"""C04 P1 regressions: real trial receipts, public CLI, isolated pool adapters."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures_review import _admit_authored_candidate
from fixtures_validation import make_execution
from test_card37 import _isolated_config
from test_pool_dispatch import _seed, _source
from xiaoba_agentos.cli import _submit_task
from xiaoba_agentos.experience_workflow import (
    prepare_trial, reserve_validation_attempt, save_execution_receipt,
    verify_final_qualification,
)
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.validation_evidence import task_evidence


class TrialReviewResource:
    def __init__(self, source, workspace=None, **kwargs):
        self.source = source

    def run_task(self, task_id, step_code, instruction, materials, **kwargs):
        return {
            "status": "completed",
            "body": "TEST independent review\n审查结论：PASS\n评分：8\n"
                    "证据引用：task:" + task_id + "\nTRIAL_VALIDATION: PASS\n",
            "execution_evidence": {"provider": "TEST fixture", "route_source": "TEST"},
        }


class Card04HostAndRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = _isolated_config(self.root, [])
        self.store = EventStore(self.config.database_path)
        self.store.initialize()
        self.store.apply_validation_extension()
        self.enterContext(patch("xiaoba_agentos.orchestrator.release_binding_status", return_value={"ok": True}))
        self.enterContext(patch("xiaoba_agentos.adapters._pool_cli_factory", return_value=TrialReviewResource))

    def cli(self, *arguments):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env["PYTHONPATH"] = os.pathsep.join([
            str(Path(__file__).resolve().parents[1] / "src"),
            str(Path(__file__).resolve().parents[2] / "review-ingress"),
            str(Path(__file__).resolve().parent),
        ])
        result = subprocess.run(
            [sys.executable, "-B", "-m", "xiaoba_agentos.cli", "--config", str(self.config.path), *arguments],
            text=True, capture_output=True, timeout=20, env=env,
        )
        self.assertTrue(result.stdout.strip(), result.stderr)
        return result.returncode, json.loads(result.stdout)

    def events(self, task_id, kind):
        with self.store.read_only() as db:
            return db.execute("SELECT * FROM events WHERE task_id=? AND event_type=?", (task_id, kind)).fetchall()

    def trial(self, *, fallback=False):
        candidate = self.store.create_authored_candidate(
            "TEST method", "TEST reusable method", self.config.runtime_dir / "memory-outbox",
            author="TEST author", experience_kind="method", problem_key="fixture-only",
        )
        candidate = _admit_authored_candidate(self.store, candidate)
        self.store.review_memory(candidate["task_id"], "delivery", "approve", "TEST human decision", candidate["evidence_sha256"])
        card, execution = make_execution(self.config.runtime_dir)
        attempt = reserve_validation_attempt(self.store, candidate["task_id"], card)
        save_execution_receipt(self.config, candidate["task_id"], card, execution)
        trial, prompt = prepare_trial(self.store, candidate["task_id"], card, execution, attempt_id=attempt)
        source = _source("TEST reviewer", "cli:TEST reviewer", "TEST model", tested=not fallback)
        _seed(self.store, [source])
        task_id = _submit_task(
            self.config, self.store, prompt, experience_kind="audit", experience_trial=trial,
            host={"id": "TEST host", "entry": "cli:TEST host", "model": "TEST host model"},
        )
        return candidate, card, task_id

    def receipt(self, task_id, verdict):
        return {
            "receipt_id": "TEST host receipt " + task_id,
            "task_id": task_id,
            "snapshot_sha256": self.store.get_task(task_id)["execution_snapshot_hash"],
            "reviewer_id": "TEST host", "verdict": verdict,
            "findings": "TEST independently checked the real execution receipt",
            "evidence_refs": ["task:" + task_id], "review_type": "revalidation", "score": 8,
            "applicability": "TEST isolated fixture", "duplicate_of": None,
            "started_at": time.time() - 1, "finished_at": time.time(),
        }

    def complete(self, task_id, verdict, *, fallback=False):
        path = self.root / ("host-" + task_id + ".json")
        path.write_text(json.dumps(self.receipt(task_id, verdict)))
        args = ["task-host-complete", task_id, "--receipt", str(path)]
        if fallback:
            args.append("--user-chose-host")
        code, value = self.cli(*args)
        self.assertEqual(0, code, value)
        return args, value

    def test_pool_plus_host_advances_real_trial_for_all_three_verdicts(self):
        for verdict, status, state, used in (
            ("PASS", "passed", "ready_for_final", 1),
            ("FAIL", "failed", "pending_round_2", 1),
            ("NEEDS_INFO", "paused", "paused", 0),
        ):
            with self.subTest(verdict=verdict):
                candidate, card, task_id = self.trial()
                self.assertEqual("awaiting_host", Orchestrator(self.config, self.store).run_once()["status"])
                self.assertEqual([], self.events(task_id, "judge.decided"))
                arguments, _ = self.complete(task_id, verdict)
                record = self.store.get_validation_records(candidate["task_id"])[0]
                lineage = self.store.get_validation_lineage(candidate["task_id"])
                self.assertEqual(status, record["status"])
                self.assertEqual("PASS", record["dsh_verdict"])
                self.assertEqual(verdict, record["peer_verdict"])
                self.assertEqual(state, lineage["lineage_status"])
                self.assertEqual(used, lineage["rounds_used"])
                self.assertEqual("completed", self.store.get_task(task_id)["status"])
                code, replay = self.cli(*arguments)
                self.assertEqual((0, True), (code, replay.get("replayed")), replay)
                self.assertEqual(1, len(self.events(task_id, "audit.settled")))
                self.assertEqual(1, len(self.events(candidate["task_id"], "memory.trial_review_settled")))
                if verdict == "FAIL":
                    self.assertTrue(reserve_validation_attempt(self.store, candidate["task_id"], card))

    def test_host_fallback_settles_receipt_but_pauses_without_independent_review(self):
        candidate, _, task_id = self.trial(fallback=True)
        arguments, value = self.complete(task_id, "PASS", fallback=True)
        self.assertIn("无独立审查", value["label"])
        record = self.store.get_validation_records(candidate["task_id"])[0]
        lineage = self.store.get_validation_lineage(candidate["task_id"])
        self.assertEqual("paused", record["status"])
        self.assertEqual(0, lineage["rounds_used"])
        self.assertEqual("paused", lineage["lineage_status"])
        self.assertIn("无独立审查", record["pause_reason"])
        # fallback 无池票：dsh 列不得伪装 PASS。
        self.assertIn(record["dsh_verdict"], (None, ""))
        self.assertEqual("PASS", record["peer_verdict"])
        self.assertEqual("completed", self.store.get_task(task_id)["status"])
        with self.store.read_only() as db:
            self.assertFalse(verify_final_qualification(db, candidate["task_id"])["qualified"])
        code, replay = self.cli(*arguments)
        self.assertEqual((0, True), (code, replay.get("replayed")))
        self.assertEqual(1, len(self.events(candidate["task_id"], "memory.validation_paused")))

    def test_host_trial_evidence_rejects_tampered_accepted_receipt(self):
        candidate, _, task_id = self.trial()
        Orchestrator(self.config, self.store).run_once()
        self.complete(task_id, "PASS")
        with self.store.connect() as db:
            event = db.execute("SELECT sequence,payload_json FROM events WHERE task_id=? AND event_type='host.receipt_accepted'", (task_id,)).fetchone()
            data = json.loads(event["payload_json"])
            data["receipt"]["verdict"] = "FAIL"
            db.execute("UPDATE events SET payload_json=? WHERE sequence=?", (json.dumps(data), event["sequence"]))
            approved = dict(db.execute("SELECT * FROM memory_reviews WHERE task_id=? AND stage='delivery'", (candidate["task_id"],)).fetchone())
            record = dict(db.execute("SELECT * FROM experience_validation_records WHERE candidate_id=?", (candidate["task_id"],)).fetchone())
            with self.assertRaisesRegex(ValueError, "宿主回执"):
                task_evidence(db, candidate, approved, record)

    def test_host_trial_evidence_rejects_same_source_or_missing_bound_pool_run(self):
        candidate, _, task_id = self.trial()
        Orchestrator(self.config, self.store).run_once()
        self.complete(task_id, "PASS")
        with self.store.connect() as db:
            approved = dict(db.execute("SELECT * FROM memory_reviews WHERE task_id=? AND stage='delivery'", (candidate["task_id"],)).fetchone())
            record = dict(db.execute("SELECT * FROM experience_validation_records WHERE candidate_id=?", (candidate["task_id"],)).fetchone())
            event = db.execute("SELECT sequence,payload_json FROM events WHERE task_id=? AND event_type='agent.started'", (task_id,)).fetchone()
            data = json.loads(event["payload_json"])
            data["execution"]["pool_score"]["entry_program"] = "cli:TEST host"
            data["execution"]["pool_score"]["model"] = "TEST host model"
            db.execute("UPDATE events SET payload_json=? WHERE sequence=?", (json.dumps(data), event["sequence"]))
            with self.assertRaisesRegex(ValueError, "来源不独立"):
                task_evidence(db, candidate, approved, record)
            db.execute("UPDATE events SET payload_json=? WHERE sequence=?", (event["payload_json"], event["sequence"]))
            db.execute("UPDATE agent_runs SET status='failed' WHERE task_id=?", (task_id,))
            with self.assertRaisesRegex(ValueError, "池票不合格"):
                task_evidence(db, candidate, approved, record)

    def test_public_resume_accepts_fresh_pool_substitute_and_worker_runs_it(self):
        good = _source("TEST good", "cli:TEST good", "good")
        bad = _source("TEST bad", "cli:TEST bad", "bad")
        _seed(self.store, [good, bad])
        task_id = _submit_task(self.config, self.store, "TEST frozen review", experience_kind="audit")
        self.store.lease_next(60)
        run_id = self.store.start_agent_run(task_id, "TEST good", {"purpose": "review"})
        self.store.finish_agent_run(run_id, "completed", "TEST existing review\n审查结论：PASS\nTRIAL_VALIDATION: PASS", None, 1)
        frozen = self.store.freeze_task(task_id, failure_class="external_upstream", failures=[{"agent": "TEST bad", "error": "TEST unavailable"}])
        bad["node"]["enabled"] = False
        good["node"]["test"]["finished_at"] = time.time() + 1
        replacement = _source("TEST replacement", "cli:TEST replacement", "replacement")
        replacement["node"]["test"]["finished_at"] = frozen["frozen_at"] - 1
        _seed(self.store, [good, bad, replacement])
        arguments = ["task-resume", task_id, "--freeze-hash", frozen["freeze_hash"]]
        code, value = self.cli(*arguments)
        self.assertEqual(1, code, value)  # Existing successful seat and stale replacement are insufficient.
        replacement["node"]["test"]["finished_at"] = time.time() + 1
        _seed(self.store, [replacement])
        code, value = self.cli(*arguments)
        self.assertEqual(0, code, value)
        self.assertEqual("retry", value["status"])
        self.assertEqual("completed", Orchestrator(self.config, self.store).run_once()["status"])
        runs = self.store.task_runs(task_id)
        self.assertEqual(1, len([run for run in runs if run["agent_name"] == "TEST good"]))
        self.assertEqual(1, len([run for run in runs if run["agent_name"] == "TEST replacement"]))
        self.assertEqual([], [run for run in runs if run["agent_name"] == "TEST bad"])
        self.assertTrue(self.store.verify_event_chain()["ok"])


if __name__ == "__main__":
    unittest.main()
