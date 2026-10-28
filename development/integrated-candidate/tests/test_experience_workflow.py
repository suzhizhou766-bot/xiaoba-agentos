"""Fresh local execution, independent review and two human gates stay distinct."""
import copy
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from fixtures_review import _config, _event_count, _run_cli
from xiaoba_agentos.experience_workflow import (
    complete_audit, digest, execute_trial, policy, prepare_trial, trial_summary, match_trials,
)
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.memory import MemoryBridge


class ExperienceWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.cfg = _config(self.root)
        self.store = EventStore(self.cfg.database_path)
        self.store.initialize()
        self.outbox = self.cfg.runtime_dir / "memory-outbox"
        self.card = {"title": "局部方法试验", "problem_key": "parse-fixture",
                     "objective": "检查fixture结果", "scope": "仅临时夹具，不碰生产",
                     "acceptance": "独立断言结果及两个独立审查通过", "stop_conditions": "失败即停止采用"}

    def tearDown(self):
        self.tmp.cleanup()

    def candidate(self, kind="method", approve=True):
        c = self.store.create_authored_candidate("可复用方法", "仅测试用的候选方法", self.outbox,
                                                author="test", experience_kind=kind, problem_key="parse-fixture")
        if approve:
            self.store.review_memory(c["task_id"], "delivery", "approve", "测试中的人工初审", c["evidence_sha256"])
        return c

    def summary(self, c):
        with self.store.read_only() as conn:
            return trial_summary(conn, self.store.get_memory_candidate(c["task_id"]))

    def execution(self, passed=True):
        return {"started_at": time.time(), "finished_at": time.time(), "cases": [{"passed": passed, "returncode": 0 if passed else 1, "output": "CASE_PASS", "expect_stdout": "CASE_PASS"}], "passed": passed}

    def trial(self, c, execution=None, names=("agy_review", "dsh_review"), verdict="PASS", complete=True):
        from fixtures_validation import native_trial
        card = self.card
        if execution and execution.get("workspace"):
            card = dict(self.card, workspace=execution["workspace"],
                        cases=[{"argv":r["argv"],"expect_stdout":r["expect_stdout"]} for r in execution["cases"]])
        elif execution:
            from fixtures_validation import make_execution
            card, execution = make_execution(self.cfg.runtime_dir, execution.get("passed") is True, self.card)
        return native_trial(self.store,c,card=card,execution=execution,names=names,settle=complete,
                            output="TEST fixture review\nTRIAL_VALIDATION: "+verdict,runtime=self.cfg.runtime_dir)["task_id"]

    def final(self, c):
        return self.store.review_memory(c["task_id"], "final", "approve", "测试中的人工终审", c["evidence_sha256"])

    def test_audit_settlement_is_once_without_candidate(self):
        t = self.store.submit("技术审计", 1, snapshot={"experience_policy": {"version": 1, "kind": "audit"}})
        self.assertTrue(complete_audit(self.store, t, "结果"))
        self.assertFalse(complete_audit(self.store, t, "另一结果"))
        self.assertEqual(self.store.get_task(t)["result"], "结果")
        self.assertIsNone(self.store.get_memory_candidate(t))
        self.assertEqual(_event_count(self.store, t, "audit.settled"), 1)
        self.assertEqual(_event_count(self.store, t, "task.completed"), 1)

    def test_audit_post_commit_error_does_not_requeue_or_stage_candidate(self):
        from xiaoba_agentos.orchestrator import Orchestrator
        t = self.store.submit("独立审计", 1, snapshot={"experience_policy": {"version": 1, "kind": "audit"}})
        def committed_then_failed(*args, **kwargs):
            complete_audit(*args, **kwargs)
            raise OSError("permission maintenance failed after commit")
        with patch("xiaoba_agentos.experience_workflow.complete_audit", committed_then_failed):
            result = Orchestrator(self.cfg, self.store).run_once()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["delivery_review"]["status"], "audit_only")
        self.assertIsNone(self.store.get_memory_candidate(t))
        self.assertEqual(_event_count(self.store, t, "task.completed"), 1)

    def test_official_skill_review_defaults_to_audit_without_normal_retrieval(self):
        from xiaoba_agentos.cli import _submit_task
        self.cfg.raw["release"] = {"expected_version": "test-mock"}
        with patch("xiaoba_agentos.cli.MemoryBridge") as memory, patch("xiaoba_agentos.cli.SkillRegistry") as skills:
            t = _submit_task(self.cfg, self.store, "你是小八 AgentOS 的独立审查者之一，只读审查技能 demo 的这一次改动。")
        self.assertEqual(policy(self.store.get_task(t))["kind"], "audit")
        memory.assert_not_called()
        skills.assert_not_called()

    def test_keyword_match_returns_only_suggestions_for_approved_methods(self):
        c = self.store.create_authored_candidate("DSH局部修复", "候选正文不应返回", self.outbox, author="test", experience_kind="method")
        self.store.review_memory(c["task_id"], "delivery", "approve", "", c["evidence_sha256"])
        matches = match_trials(self.store, query="dsh")
        self.assertEqual([m["task_id"] for m in matches], [c["task_id"]])
        self.assertEqual(set(matches[0]), {"task_id", "title", "candidate_sha256"})

    def test_constraint_hold_requires_explicit_human_release_and_stays_audit(self):
        t = self.store.submit("审计", 1, snapshot={"experience_policy": {"version": 1, "kind": "audit"}})
        self.store.hold_constraint_task(t, "暂存结果", {})
        self.assertFalse(complete_audit(self.store, t, "不能释放"))
        self.assertTrue(complete_audit(self.store, t, "", release_hold=True))
        self.assertIsNone(self.store.get_memory_candidate(t))
        self.assertEqual(self.store.get_constraint_hold(t)["status"], "released")

    def test_method_cannot_skip_real_validation(self):
        c = self.candidate()
        with self.assertRaisesRegex(ValueError, "fresh execution"):
            self.final(c)
        self.assertEqual(self.summary(c)["state"], "awaiting_trial")

    def test_rule_still_needs_two_human_approvals_but_no_trial(self):
        c = self.candidate("rule", approve=False)
        with self.assertRaises(ValueError):
            self.final(c)
        self.store.review_memory(c["task_id"], "delivery", "approve", "", c["evidence_sha256"])
        self.assertEqual(self.final(c)["status"], "pending_final_review")

    def test_two_reviews_allow_final_on_original_candidate(self):
        c = self.candidate()
        task = self.trial(c)
        self.assertIsNone(self.store.get_memory_candidate(task))
        self.assertEqual(len(self.store.review_queue("all")), 1)
        self.assertEqual(self.summary(c)["state"], "ready_for_final")
        self.assertEqual(self.final(c)["status"], "pending_final_review")
        with self.store.publication_transaction(c["task_id"]) as window:
            self.assertEqual(window.evidence_sha256, c["evidence_sha256"])

    def test_one_reviewer_cannot_pass(self):
        c = self.candidate()
        self.trial(c, names=("AGY",))
        with self.assertRaises(ValueError):
            self.final(c)

    def test_running_trial_cannot_pass(self):
        c = self.candidate()
        self.trial(c, complete=False)
        self.assertEqual(self.summary(c)["state"], "trial_running")
        with self.assertRaises(ValueError):
            self.final(c)

    def test_new_failed_execution_supersedes_old_success_even_when_models_vote_pass(self):
        c = self.candidate()
        self.trial(c)
        self.trial(c, self.execution(False))
        self.assertEqual(self.summary(c)["state"], "awaiting_check")
        with self.assertRaises(ValueError):
            self.final(c)

    def test_renewed_initial_approval_invalidates_old_trial(self):
        c = self.candidate()
        self.trial(c)
        self.store.review_memory(c["task_id"], "delivery", "approve", "重新初审", c["evidence_sha256"])
        with self.assertRaises(ValueError):
            self.final(c)

    def test_old_execution_is_refused(self):
        c = self.candidate()
        with self.assertRaisesRegex(ValueError, "new local execution"):
            prepare_trial(self.store, c["task_id"], self.card, {"cases": ["old"], "started_at": 1})

    def test_candidate_bytes_changed_before_queue_leaves_no_trial_task(self):
        c = self.candidate()
        t, prompt = prepare_trial(self.store, c["task_id"], self.card, self.execution())
        Path(c["staged_path"]).write_text("changed")
        before = len(self.store.list_tasks(100))
        with self.assertRaisesRegex(ValueError, "bytes changed"):
            self.store.submit(prompt, 1, snapshot={"experience_trial": t, "experience_policy": {"version": 1, "kind": "audit"}})
        self.assertEqual(before, len(self.store.list_tasks(100)))

    def test_candidate_does_not_become_normal_published_memory(self):
        c = self.candidate()
        self.trial(c)
        self.assertFalse(MemoryBridge(self.cfg, self.store)._database_active({"source_task_id": c["task_id"]}))

    @unittest.skipUnless(Path("/usr/bin/sandbox-exec").exists(), "macOS trial harness")
    def test_cli_real_execution_and_mock_dual_review_return_to_original_candidate(self):
        # These two providers are explicitly mocks. Real providers are a
        # separate release acceptance gate, never claimed by this test.
        raw = json.loads(self.cfg.path.read_text())
        raw["release"] = {"expected_version": "test-mock", "index_path": str(self.root / "unused-index.json")}
        for agent, name in zip(raw["agents"], ("agy_review", "dsh_review")):
            agent["name"] = name
            agent["template"] = "已核对测试夹具证据，结论只针对本例。\nTRIAL_VALIDATION: PASS"
        self.cfg.path.write_text(json.dumps(raw))
        from xiaoba_agentos.config import AppConfig
        from xiaoba_agentos.orchestrator import Orchestrator
        self.cfg = AppConfig.load(self.cfg.path)
        self.store.apply_validation_extension()
        c = self.candidate()
        workspace = self.cfg.runtime_dir / "trial-workspaces" / "cli-case"
        workspace.mkdir(parents=True)
        card = dict(self.card, workspace=str(workspace), cases=[{
            "argv": [os.path.realpath(sys.executable), "-I", "-B", "-c", "assert 2+3==5; print('ASSERTIONS_PASSED')"],
            "expect_stdout": "ASSERTIONS_PASSED"}])
        path = self.root / "trial-card.json"
        path.write_text(json.dumps(card))
        code, queued = _run_cli(["--config", str(self.cfg.path), "memory-trial-run", c["task_id"], "--card", str(path)])
        self.assertEqual(code, 0, queued)
        self.assertTrue(Path(queued["execution_receipt"]).is_file())
        task_id = queued["validation_task_id"]
        snapshot = json.loads(self.store.get_task(task_id)["execution_snapshot_json"])
        self.assertEqual(snapshot["memory_hits"], [])
        self.assertEqual(snapshot["agent_memory_contexts"], {})
        self.assertEqual(snapshot["knowlp_context"], "")
        result = Orchestrator(self.cfg, self.store).run_once()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(self.store.review_queue("all")), 1)
        self.assertTrue(self.summary(c)["ready"], self.summary(c))
        self.assertTrue(self.store.verify_event_chain()["ok"])
        self.assertIsNone(self.store.get_memory_candidate(task_id))

    def test_audit_completion_closes_a_closing_session(self):
        session = self.store.start_session()
        t = self.store.submit("审计", 1, session_id=session["id"],
                              snapshot={"experience_policy": {"version": 1, "kind": "audit"}})
        self.store.session_review_packet(session["id"])
        complete_audit(self.store, t, "结果")
        with self.store.read_only() as conn:
            self.assertEqual(conn.execute("SELECT status FROM work_sessions WHERE id=?", (session["id"],)).fetchone()[0], "closed")

    @unittest.skipUnless(Path("/usr/bin/sandbox-exec").exists(), "macOS trial harness")
    def test_invalid_later_case_is_refused_before_first_case_executes(self):
        workspace = self.cfg.runtime_dir / "trial-workspaces" / "invalid-case"
        workspace.mkdir(parents=True)
        card = dict(self.card, workspace=str(workspace), cases=[{
            "argv": [os.path.realpath(sys.executable), "-I", "-B", "-c", "open('must-not-exist','w').write('bad');print('PASS')"],
            "expect_stdout": "PASS"}, {"argv": []}])
        with self.assertRaises(ValueError):
            execute_trial(self.cfg, card)
        self.assertFalse((workspace / "must-not-exist").exists())

    @unittest.skipUnless(Path("/usr/bin/sandbox-exec").exists(), "macOS trial harness")
    def test_real_test_runs_but_outside_writes_and_network_are_denied(self):
        c = self.candidate()
        workspace = self.cfg.runtime_dir / "trial-workspaces" / "中文夹具"
        workspace.mkdir(parents=True)
        outside = self.root / "must-not-be-written"
        private = self.root / "private-fixture.txt"
        private.write_text("private fixture, not a real credential")
        script = ("from pathlib import Path; import socket\n"
                  "Path('result').write_text(str(2+3))\n"
                  "try:\n Path(" + repr(str(outside)) + ").write_text('bad')\nexcept PermissionError: pass\nelse: raise AssertionError('outside write allowed')\n"
                  "try:\n Path(" + repr(str(private)) + ").read_text()\nexcept PermissionError: pass\nelse: raise AssertionError('outside user-temp read allowed')\n"
                  "try:\n socket.create_connection(('127.0.0.1',9),timeout=1)\nexcept PermissionError: pass\nelse: raise AssertionError('network allowed')\n"
                  "assert Path('result').read_text()=='5'\nprint('ACTUAL_CASE_PASS')")
        card = dict(self.card, workspace=str(workspace), cases=[{"argv": [os.path.realpath(sys.executable), "-I", "-B", "-c", script], "expect_stdout": "ACTUAL_CASE_PASS"}])
        execution = execute_trial(self.cfg, card)
        self.assertTrue(execution["passed"], execution)
        self.assertFalse(outside.exists())
        self.assertEqual((workspace / "result").read_text(), "5")
        self.trial(c, execution)
        self.assertTrue(self.summary(c)["ready"])

    @unittest.skipUnless(Path("/usr/bin/sandbox-exec").exists(), "macOS trial harness")
    def test_core_interpreter_runs_but_only_its_binary_and_stdlib_are_readable(self):
        from xiaoba_agentos.interpreter import PINNED_PYTHON
        if not PINNED_PYTHON.is_file():
            self.skipTest("pinned core interpreter not installed on this machine")
        home = PINNED_PYTHON.resolve().parents[1]
        site_packages = home / "lib/python3.14/site-packages"
        other = home / "bin/python3.14-config"
        workspace = self.cfg.runtime_dir / "trial-workspaces" / "core-interpreter"
        workspace.mkdir(parents=True)
        probes = [str(p) for p in sorted(site_packages.iterdir())[:1]] + [str(other)]
        script = ("import json, pathlib, sqlite3\n"
                  "for p in " + repr(probes) + ":\n"
                  " try:\n  path=pathlib.Path(p)\n  path.read_bytes() if path.is_file() else list(path.iterdir())\n"
                  " except PermissionError: pass\n"
                  " else: raise AssertionError('readable: '+p)\n"
                  "print('CORE_INTERPRETER_SANDBOXED')")
        card = dict(self.card, workspace=str(workspace), cases=[{"argv": [str(PINNED_PYTHON), "-I", "-B", "-c", script], "expect_stdout": "CORE_INTERPRETER_SANDBOXED"}])
        execution = execute_trial(self.cfg, card)
        self.assertTrue(execution["passed"], execution)


if __name__ == "__main__":
    unittest.main()
