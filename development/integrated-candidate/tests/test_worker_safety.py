import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fixtures_review import _candidate_count, _config, _event_count, _sha
from xiaoba_agentos.cli import execute_review, main
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.output_hygiene import detect_reasoning_leak
from xiaoba_agentos.store import EventStore


LEAKED = "<think>用户想要我算加法，先分解数字。\n结论：1517。"


class LeakedOutputReuseTest(unittest.TestCase):
    def test_leaked_answer_is_rerun_instead_of_reused(self):
        self.assertTrue(detect_reasoning_leak(LEAKED))
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("测试", 3)
            run_id = store.start_agent_run(task_id, "a")
            store.finish_agent_run(run_id, "completed", LEAKED, None, 10)
            result = Orchestrator(config, store).run_once()
            self.assertEqual("completed", result["status"], result)
            runs = [run for run in store.task_runs(task_id) if run["agent_name"] == "a"]
            self.assertEqual(2, len(runs))
            self.assertFalse(detect_reasoning_leak(runs[-1]["output"]))
            self.assertEqual(0, _event_count(store, task_id, "output_hygiene.quarantined"))

    def test_clean_answer_is_still_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("测试", 3)
            run_id = store.start_agent_run(task_id, "a")
            store.finish_agent_run(run_id, "completed", "结论：可行。依据：测试。", None, 10)
            self.assertEqual("completed", Orchestrator(config, store).run_once()["status"])
            self.assertEqual(1, len([run for run in store.task_runs(task_id) if run["agent_name"] == "a"]))


class LeaseReleaseTest(unittest.TestCase):
    def test_expired_lease_is_recovered_on_the_next_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "runtime" / "agentos.sqlite3")
            store.initialize()
            task_id = store.submit("任务", 3)
            done = store.submit("已完成", 3)
            self.assertEqual(task_id, store.lease_next(1800)["id"])
            store.complete_manually(done, "结果")
            self.assertFalse(store.has_runnable_work() and store.get_task(task_id)["status"] != "running")
            self.assertEqual(1, store.expire_running_leases())
            self.assertTrue(store.has_runnable_work())
            store.recover_expired()
            self.assertEqual("retry", store.get_task(task_id)["status"])
            self.assertEqual("completed", store.get_task(done)["status"])
            self.assertEqual(0, store.expire_running_leases())

    def test_worker_error_releases_the_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            output = io.StringIO()
            with patch("xiaoba_agentos.cli.Orchestrator") as orchestrator, patch(
                "xiaoba_agentos.cli.time.sleep"
            ), patch.object(EventStore, "expire_running_leases", return_value=1) as release, redirect_stdout(output):
                orchestrator.return_value.run_once.side_effect = RuntimeError("boom")
                main(["--config", str(config.path), "run", "--max-idle-polls", "1"])
            release.assert_called_once()
            report = next(json.loads(line) for line in output.getvalue().splitlines() if "worker_error" in line)
            self.assertEqual(1, report["leases_released"])


class SessionClosingTest(unittest.TestCase):
    def _store(self, directory):
        store = EventStore(Path(directory) / "runtime" / "agentos.sqlite3")
        store.initialize()
        return store

    def _status(self, store, session_id):
        with store.read_only() as conn:
            return conn.execute("SELECT status FROM work_sessions WHERE id=?", (session_id,)).fetchone()[0]

    def test_closing_with_nothing_pending_closes_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            session = store.start_session()
            task_id = store.submit("任务", 1, session_id=str(session["id"]))
            store.fail_manually(task_id, "失败")
            packet = store.session_review_packet(str(session["id"]))
            self.assertEqual("closed", packet["session"]["status"])
            self.assertEqual([str(session["id"])], packet["closed_sessions"])

    def test_unresolved_candidate_keeps_the_session_closing_until_reviewed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            store = EventStore(config.database_path)
            store.initialize()
            session = store.start_session()
            task_id = store.submit("任务", 1, session_id=str(session["id"]))
            staged = root / "c.md"
            staged.write_text("证据", encoding="utf-8")
            store.complete_with_memory_candidate(task_id, "结果", str(staged), _sha(staged), [], expected_attempt=0)
            self.assertEqual("closing", store.session_review_packet(str(session["id"]))["session"]["status"])
            self.assertEqual([], store.close_resolved_sessions())
            code, _payload = execute_review(config, store, task_id, "delivery", "reject", "不需要")
            self.assertEqual(0, code)
            self.assertEqual("closed", self._status(store, str(session["id"])))

    def test_sessions_stuck_in_closing_are_swept(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            session = store.start_session()
            task_id = store.submit("任务", 1, session_id=str(session["id"]))
            store.fail_manually(task_id, "失败")
            with store.connect() as conn:  # the state the old flow left behind
                conn.execute("UPDATE work_sessions SET status='closing',closing_at=? WHERE id=?",
                             (time.time(), session["id"]))
            self.assertEqual("closing", self._status(store, str(session["id"])))
            store.start_session()
            self.assertEqual("closed", self._status(store, str(session["id"])))
            self.assertTrue(store.verify_event_chain()["ok"])


class AuthoredCandidateTest(unittest.TestCase):
    def test_candidate_enters_the_review_queue_without_ever_being_runnable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            store = EventStore(config.database_path)
            store.initialize()
            source = store.submit("来源任务", 1)
            created = store.create_authored_candidate(
                "规则标题", "# 规则\n\n正文。", config.runtime_dir / "memory-outbox",
                author="host_assistant", source_task_ids=[source], idempotency_key="rule-1")
            task = store.get_task(created["task_id"])
            self.assertEqual(("completed", "host_authored"), (task["status"], task["execution_mode"]))
            candidate = store.get_memory_candidate(created["task_id"])
            self.assertEqual("pending_delivery_review", candidate["status"])
            self.assertEqual(candidate["evidence_sha256"], _sha(Path(candidate["staged_path"])))
            self.assertEqual([source], json.loads(candidate["source_task_ids_json"]))
            self.assertEqual(1, _event_count(store, created["task_id"], "memory.candidate_authored"))
            self.assertEqual(0, _event_count(store, created["task_id"], "task.leased"))
            self.assertTrue(store.verify_event_chain()["ok"])
            # It flows through the ordinary review path.
            code, payload = execute_review(config, store, created["task_id"], "delivery", "approve", "")
            self.assertEqual((0, "delivery_approved"), (code, payload["memory_status"]))
            # A retry with the same key gets the same record back, and nothing is duplicated.
            # The replay has to carry the same content; a key reused for different
            # content is refused (see test_write_boundary.py).
            again = store.create_authored_candidate("规则标题", "# 规则\n\n正文。", config.runtime_dir / "memory-outbox",
                                                    author="host_assistant", source_task_ids=[source],
                                                    idempotency_key="rule-1")
            self.assertEqual((created["task_id"], True), (again["task_id"], again["idempotent"]))
            self.assertEqual("delivery_approved", again["status"])
            self.assertEqual(1, len(list((config.runtime_dir / "memory-outbox").glob("*.md"))))
            ordinary = store.submit("普通任务", 1, idempotency_key="ordinary-key")
            with self.assertRaises(ValueError):
                store.create_authored_candidate("标题", "正文", config.runtime_dir / "memory-outbox",
                                                author="a", idempotency_key="ordinary-key")
            self.assertIsNone(store.get_memory_candidate(ordinary))

    def test_failures_leave_no_task_candidate_or_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "runtime" / "agentos.sqlite3")
            store.initialize()
            outbox = root / "runtime" / "memory-outbox"
            with self.assertRaises(KeyError):
                store.create_authored_candidate("标题", "正文", outbox, author="a", source_task_ids=["missing"])
            with patch.object(store, "_write_candidate_status_sidecar_values", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.create_authored_candidate("标题", "正文", outbox, author="a")
            for bad in ({"title": " ", "body": "正文"}, {"title": "标题", "body": ""}):
                with self.assertRaises(ValueError):
                    store.create_authored_candidate(bad["title"], bad["body"], outbox, author="a")
            with store.read_only() as conn:
                self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
                self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0])
            self.assertEqual([], list(outbox.glob("*")) if outbox.exists() else [])

    def test_command_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            EventStore(config.database_path).initialize()
            body = root / "body.md"
            body.write_text("# 规则\n\n正文。\n", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["--config", str(config.path), "memory-candidate-create",
                             "--title", "规则标题", "--body-file", str(body)])
            self.assertEqual(0, code)
            self.assertEqual("pending_delivery_review", json.loads(output.getvalue())["status"])
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["--config", str(config.path), "memory-candidate-create",
                             "--title", "规则标题", "--body-file", str(root / "missing.md")])
            self.assertEqual(1, code)


class HoldReleasePostCommitTest(unittest.TestCase):
    def test_failure_inside_connect_after_commit_is_reported_as_released(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("受约束任务", 2)
            store.hold_constraint_task(task_id, "被扣留的结果", {"reason": "test"})
            original = EventStore.release_constraint_hold

            def fail_after_commit(self, *args, **kwargs):
                real = self._secure_permissions
                calls = [0]

                def permissions():
                    calls[0] += 1
                    if calls[0] == 3:  # before open, after open, then finally after commit
                        raise PermissionError("postcommit chmod denied")
                    return real()

                with patch.object(self, "_secure_permissions", side_effect=permissions):
                    return original(self, *args, **kwargs)

            output = io.StringIO()
            with patch.object(EventStore, "release_constraint_hold", fail_after_commit), redirect_stdout(output):
                code = main(["--config", str(config.path), "constraint-hold-release", task_id])
            self.assertEqual(0, code, output.getvalue())
            self.assertEqual("released", json.loads(output.getvalue())["status"])
            self.assertEqual("released", store.get_constraint_hold(task_id)["status"])
            self.assertEqual(1, _candidate_count(store, task_id))
            self.assertEqual(1, _event_count(store, task_id, "task.completed"))


class ReviewedDefectsTest(unittest.TestCase):
    """Regression tests for defects found by the independent review of this candidate."""

    def _store(self, root):
        store = EventStore(root / "runtime" / "agentos.sqlite3")
        store.initialize()
        staged = root / "runtime" / "memory-outbox" / "c.md"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("证据", encoding="utf-8")
        return store, staged, _sha(staged)

    def test_rolled_back_completion_restores_the_status_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            store, staged, evidence = self._store(Path(directory))
            sidecar = staged.with_suffix(".md.status.json")
            task_id = store.submit("任务", 2)
            # No sidecar before: a rollback must not leave one claiming the candidate is queued.
            with patch.object(store, "_append_event_tx", side_effect=RuntimeError("late failure")):
                with self.assertRaises(RuntimeError):
                    store.complete_with_memory_candidate(task_id, "结果", str(staged), evidence, [], expected_attempt=0)
            self.assertFalse(sidecar.exists())
            self.assertIsNone(store.get_memory_candidate(task_id))
            # An earlier sidecar is put back unchanged.
            sidecar.write_text('{"status": "earlier"}', encoding="utf-8")
            with patch.object(store, "_append_event_tx", side_effect=RuntimeError("late failure")):
                with self.assertRaises(RuntimeError):
                    store.complete_with_memory_candidate(task_id, "结果", str(staged), evidence, [], expected_attempt=0)
            self.assertEqual('{"status": "earlier"}', sidecar.read_text(encoding="utf-8"))

    def test_sidecar_is_kept_when_the_failure_came_after_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            store, staged, evidence = self._store(Path(directory))
            task_id = store.submit("任务", 2)
            real = store._secure_permissions
            calls = [0]

            def permissions():
                calls[0] += 1
                if calls[0] == 3:
                    raise PermissionError("postcommit chmod denied")
                return real()

            with patch.object(store, "_secure_permissions", side_effect=permissions):
                with self.assertRaises(PermissionError):
                    store.complete_with_memory_candidate(task_id, "结果", str(staged), evidence, [], expected_attempt=0)
            self.assertEqual("completed", store.get_task(task_id)["status"])
            sidecar = json.loads(staged.with_suffix(".md.status.json").read_text(encoding="utf-8"))
            self.assertEqual(("pending_delivery_review", evidence), (sidecar["status"], sidecar["evidence_sha256"]))

    def test_rolled_back_hold_release_restores_the_status_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            store, staged, evidence = self._store(Path(directory))
            task_id = store.submit("受约束任务", 2)
            store.hold_constraint_task(task_id, "被扣留的结果", {"reason": "test"})
            original = store._append_event_tx
            seen = []

            def fail_on_staged(conn, tid, event_type, payload):
                if event_type == "memory.candidate_staged":
                    raise RuntimeError("late failure")
                seen.append(event_type)
                return original(conn, tid, event_type, payload)

            with patch.object(store, "_append_event_tx", side_effect=fail_on_staged):
                with self.assertRaises(RuntimeError):
                    store.release_constraint_hold(task_id, str(staged), evidence)
            self.assertFalse(staged.with_suffix(".md.status.json").exists())
            self.assertEqual("pending", store.get_constraint_hold(task_id)["status"])

    def test_store_refuses_a_candidate_whose_file_does_not_match_its_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            store, staged, evidence = self._store(Path(directory))
            task_id = store.submit("任务", 2)
            for path, digest in ((str(staged), "0" * 64), (str(staged.with_name("missing.md")), evidence)):
                with self.assertRaises(ValueError):
                    store.complete_with_memory_candidate(task_id, "结果", path, digest, [], expected_attempt=0)
            self.assertNotEqual("completed", store.get_task(task_id)["status"])
            store.hold_constraint_task(task_id, "结果", {"reason": "test"})
            with self.assertRaises(ValueError):
                store.release_constraint_hold(task_id, str(staged), "0" * 64)
            self.assertEqual("pending", store.get_constraint_hold(task_id)["status"])

    def test_completed_task_without_a_candidate_is_repaired_not_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            store, staged, evidence = self._store(Path(directory))
            task_id = store.submit("任务", 2)
            store.complete_manually(task_id, "结果")  # the old two-step path, interrupted before staging
            self.assertIsNone(store.get_memory_candidate(task_id))
            self.assertFalse(store.complete_with_memory_candidate(task_id, "结果", str(staged), evidence, [], expected_attempt=0))
            self.assertEqual("pending_delivery_review", store.get_memory_candidate(task_id)["status"])
            self.assertEqual(1, _event_count(store, task_id, "task.completed"))
            self.assertEqual(1, _event_count(store, task_id, "memory.candidate_staged"))
            self.assertFalse(store.complete_with_memory_candidate(task_id, "结果", str(staged), evidence, [], expected_attempt=0))
            self.assertEqual(1, _event_count(store, task_id, "memory.candidate_staged"))

    def test_failed_staging_leaves_no_orphan_files(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("测试", 1)  # single attempt: the task ends up failed
            with patch.object(EventStore, "_append_event_tx", side_effect=RuntimeError("late failure")):
                with self.assertRaises(RuntimeError):
                    Orchestrator(config, store).run_once()
            with patch.object(EventStore, "complete_with_memory_candidate", side_effect=OSError("disk full")):
                store.expire_running_leases()
                result = Orchestrator(config, store).run_once()
            self.assertIn(result["status"], {"failed", "retry"})
            outbox = config.runtime_dir / "memory-outbox"
            self.assertEqual([], sorted(p.name for p in outbox.glob(f"{task_id}*")))

    def test_authored_candidate_write_failure_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "runtime" / "agentos.sqlite3")
            store.initialize()
            outbox = root / "runtime" / "memory-outbox"
            with patch("xiaoba_agentos.store.os.chmod", side_effect=OSError("denied")):
                with self.assertRaises(OSError):
                    store.create_authored_candidate("标题", "正文", outbox, author="a")
            self.assertEqual([], list(outbox.glob("*")))

    def test_review_does_not_close_a_session_that_still_has_runnable_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            store = EventStore(config.database_path)
            store.initialize()
            session = store.start_session()
            reviewed = store.submit("已完成", 1, session_id=str(session["id"]))
            staged = root / "c.md"
            staged.write_text("证据", encoding="utf-8")
            store.complete_with_memory_candidate(reviewed, "结果", str(staged), _sha(staged), [], expected_attempt=0)
            store.submit("仍在排队", 1, session_id=str(session["id"]))
            store.session_review_packet(str(session["id"]))
            execute_review(config, store, reviewed, "delivery", "reject", "")
            with store.read_only() as conn:
                status = conn.execute("SELECT status FROM work_sessions WHERE id=?", (session["id"],)).fetchone()[0]
            self.assertEqual("closing", status)

    def test_preflight_cadence_has_its_own_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            from fixtures_review import _raw_config
            raw = _raw_config(root)
            raw["worker"].update({"log_heartbeat_polls": 100, "preflight_heartbeat_polls": 2})
            path = root / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with patch("xiaoba_agentos.cli.Orchestrator") as orchestrator, patch(
                "xiaoba_agentos.cli.time.sleep"
            ), redirect_stdout(io.StringIO()):
                orchestrator.return_value.run_once.return_value = {"status": "idle"}
                main(["--config", str(path), "run", "--max-idle-polls", "4"])
            flags = [call.kwargs["preflight_when_idle"] for call in orchestrator.return_value.run_once.call_args_list]
            self.assertEqual([True, False, True, False], flags)

    def test_blocked_intake_can_be_resubmitted_with_the_same_message_id(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            store = EventStore(config.database_path)
            store.initialize()
            args = ["--config", str(config.path), "intake", "请多后台分析", "--explicit-agentos",
                    "--requires-action", "--submit", "--message-id", "same-id"]
            skills = patch("xiaoba_agentos.cli.SkillRegistry")
            skills.start().return_value.select_context.return_value = {"selected": [], "context": "", "context_sha256": "t"}
            self.addCleanup(skills.stop)
            version = patch.object(AppConfig, "expected_release_version", "test")
            version.start()
            self.addCleanup(version.stop)
            out = io.StringIO()
            with patch("xiaoba_agentos.cli.execution_blockers", return_value=["agy_review: 桌面未运行"]), redirect_stdout(out):
                self.assertEqual(1, main(args))
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(0, main(args))
            self.assertEqual("pending", json.loads(out.getvalue())["status"])


if __name__ == "__main__":
    unittest.main()
