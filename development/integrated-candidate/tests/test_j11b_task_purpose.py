"""Freeze controlled task purpose at submission, using only isolated stores."""
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures_review import _config, _run_cli
from xiaoba_agentos.experience_workflow import complete_audit
from xiaoba_agentos.cli import _submit_task, build_parser
from xiaoba_agentos.store import EventStore


REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "development" / "integrated-candidate" / "scripts"


def _load_script(name):
    spec = importlib.util.spec_from_file_location("TEST_" + name, SCRIPTS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class J11bTaskPurposeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = _config(self.root)
        config_data = json.loads(self.config.path.read_text(encoding="utf-8"))
        config_data["release"] = {"expected_version": "v1.6.16-test"}
        self.config.path.write_text(json.dumps(config_data), encoding="utf-8")
        from xiaoba_agentos.config import AppConfig
        self.config = AppConfig.load(self.config.path)
        self.store = EventStore(self.config.database_path)
        self.store.initialize()

    def snapshot(self, task_id):
        task = self.store.get_task(task_id)
        snapshot = json.loads(task["execution_snapshot_json"])
        canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(hashlib.sha256(canonical.encode()).hexdigest(), task["execution_snapshot_hash"])
        return snapshot

    def test_release_smoke_cli_option_is_registered(self):
        args = build_parser().parse_args([
            "--config", str(self.config.path), "submit", "TEST post-switch smoke",
            "--task-purpose", "release_smoke",
        ])
        self.assertEqual("release_smoke", args.task_purpose)

    def test_release_smoke_cli_dispatch_freezes_purpose(self):
        code, result = _run_cli([
            "--config", str(self.config.path), "submit", "TEST post-switch smoke",
            "--task-purpose", "release_smoke",
        ])
        self.assertEqual(0, code, result)
        snapshot = self.snapshot(result["task_id"])
        self.assertEqual("release_smoke", snapshot["task_purpose"])
        self.assertEqual("audit", snapshot["experience_policy"]["kind"])

    def test_release_smoke_freezes_audit_only_purpose(self):
        task = _submit_task(
            self.config, self.store, "TEST post-switch smoke",
            task_purpose="release_smoke",
        )
        snapshot = self.snapshot(task)
        self.assertEqual("release_smoke", snapshot["task_purpose"])
        self.assertEqual("audit", snapshot["experience_policy"]["kind"])

    def test_release_smoke_audit_settlement_never_creates_candidate(self):
        task = _submit_task(
            self.config, self.store, "TEST post-switch smoke",
            task_purpose="release_smoke",
        )

        self.assertTrue(complete_audit(self.store, task, "TEST audit result"))
        self.assertEqual("completed", self.store.get_task(task)["status"])
        self.assertIsNone(self.store.get_memory_candidate(task))
        with self.store.read_only() as db:
            self.assertEqual(1, db.execute(
                "SELECT count(*) FROM events WHERE task_id=? AND event_type='audit.settled'", (task,)
            ).fetchone()[0])
            self.assertEqual(0, db.execute(
                "SELECT count(*) FROM events WHERE task_id=? AND event_type IN ('memory.candidate_staged','memory.value_unscored','memory.value_assessed')",
                (task,),
            ).fetchone()[0])

    def test_release_smoke_candidate_settlement_fails_closed(self):
        task = _submit_task(
            self.config, self.store, "TEST forged smoke candidate",
            task_purpose="release_smoke",
        )
        staged = self.config.runtime_dir / "memory-outbox" / (task + ".md")
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("TEST must never become a smoke candidate", encoding="utf-8")
        digest = hashlib.sha256(staged.read_bytes()).hexdigest()

        with self.assertRaisesRegex(ValueError, "audit task must use audit-only settlement"):
            self.store.complete_with_memory_candidate(
                task, "TEST result", str(staged), digest, [], expected_attempt=0
            )
        self.assertEqual("pending", self.store.get_task(task)["status"])
        self.assertIsNone(self.store.get_memory_candidate(task))

    def test_release_acceptance_freezes_audit_only_purpose(self):
        task = _submit_task(
            self.config, self.store, "TEST acceptance",
            release_acceptance=True, no_work_task=True,
        )
        snapshot = self.snapshot(task)
        self.assertEqual("release_acceptance", snapshot["task_purpose"])
        self.assertEqual("audit", snapshot["experience_policy"]["kind"])
        self.assertIs(True, snapshot["review_context"]["release_acceptance"])

    def test_experience_trial_freezes_audit_only_purpose(self):
        with patch.object(self.store, "submit", return_value="TEST-validation-task") as submit:
            task = _submit_task(
                self.config, self.store, "TEST trial review",
                experience_trial={"source_task_id": "TEST-source"},
            )
        self.assertEqual("TEST-validation-task", task)
        snapshot = submit.call_args.kwargs["snapshot"]
        self.assertEqual("experience_trial", snapshot["task_purpose"])
        self.assertEqual("audit", snapshot["experience_policy"]["kind"])
        self.assertEqual("TEST-source", snapshot["experience_trial"]["source_task_id"])

    def test_controlled_purpose_rejects_candidate_and_no_review_modes(self):
        with self.assertRaisesRegex(ValueError, "must use audit"):
            _submit_task(
                self.config, self.store, "TEST invalid smoke",
                experience_kind="method", task_purpose="release_smoke",
            )
        with self.assertRaisesRegex(ValueError, "require review"):
            _submit_task(
                self.config, self.store, "TEST unreviewed smoke",
                task_purpose="release_smoke", needs_review=False,
            )
        with self.assertRaisesRegex(ValueError, "requires --release-acceptance"):
            _submit_task(
                self.config, self.store, "TEST forged acceptance marker",
                task_purpose="release_acceptance",
            )

    def test_default_method_task_keeps_default_candidate_policy(self):
        empty_context = {"hits": [], "contexts": {}, "receipt": None}
        empty_skills = {
            "selected": [], "context": "",
            "context_sha256": hashlib.sha256(b"").hexdigest(),
        }
        with patch("xiaoba_agentos.cli.MemoryBridge") as memory, \
                patch("xiaoba_agentos.cli.SkillRegistry") as skills:
            memory.return_value.agent_contexts.return_value = empty_context
            memory.return_value.render_hits.return_value = ""
            skills.return_value.select_context.return_value = empty_skills
            task = _submit_task(self.config, self.store, "TEST ordinary user task")
        snapshot = self.snapshot(task)
        self.assertEqual("user_task", snapshot["task_purpose"])
        self.assertEqual("method", snapshot["experience_policy"]["kind"])

    def test_release_acceptance_wrapper_forces_audit_and_rejects_conflicts(self):
        wrapper = _load_script("submit_release_acceptance.py")
        argv = wrapper.arguments(["--config", "TEST-config", "TEST acceptance", "--no-work-task"])
        self.assertIn("--release-acceptance", argv)
        self.assertEqual("audit", argv[argv.index("--memory-kind") + 1])
        with self.assertRaises(SystemExit):
            wrapper.arguments(["--config", "TEST-config", "TEST acceptance", "--memory-kind", "method"])

    def test_release_smoke_wrapper_forces_audit_purpose(self):
        wrapper = _load_script("submit_release_smoke.py")
        argv = wrapper.arguments(["--config", "TEST-config", "TEST smoke"])
        self.assertEqual("audit", argv[argv.index("--memory-kind") + 1])
        self.assertEqual("release_smoke", argv[argv.index("--task-purpose") + 1])
        with self.assertRaises(SystemExit):
            wrapper.arguments(["--config", "TEST-config", "TEST smoke", "--memory-kind", "method"])
        with self.assertRaises(SystemExit):
            wrapper.arguments(["--config", "TEST-config", "TEST smoke", "--no-review"])


if __name__ == "__main__":
    unittest.main()
