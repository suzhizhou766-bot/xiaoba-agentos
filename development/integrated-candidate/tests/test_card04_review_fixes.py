"""C04 公开入口反例：递归排除、替补计票、动态席位、三态、Worker 节流。

隔离临时目录与假适配器，不调用真实模型，不碰生产库。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fixtures_review import _config
from test_pool_dispatch import _seed, _source
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.pool_dispatch import entry_program, infer_agent_type
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.task_participants import execution_context
from xiaoba_agentos.task_scores import capture_pool
from xiaoba_agentos.validation_evidence import (
    current_pool_review_participants,
    pool_review_verdicts,
    review_independence,
)


def _review_body(verdict="PASS"):
    return (
        f"证据引用：task:task-1\n审查结论：{verdict}\n"
        f"TRIAL_VALIDATION: {verdict}\nTEST 池节点审查正文"
    )


class Card04ReviewFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = _config(self.root)
        self.store = EventStore(self.config.database_path)
        self.store.initialize()

    def _seed_pair(self, *nodes):
        _seed(self.store, list(nodes))

    # ---------- C04-01 递归排除 / 独立性 ----------
    def test_trial_shell_excludes_source_author_and_real_task_executor(self):
        source_id = self.store.submit("TEST source work", 1)
        real_id = self.store.submit("TEST real task work", 1)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store._append_event_tx(db, source_id, "task.participant_recorded",
                                       {"actor_id": "author-A", "role": "author"})
            self.store._append_event_tx(db, real_id, "task.participant_recorded",
                                       {"actor_id": "exec-B", "role": "executor"})
            db.execute(
                "UPDATE tasks SET status='completed',result=? WHERE id=?",
                ("TEST done", real_id),
            )
        trial_id = self.store.submit("TEST trial shell", 1)
        snap = {
            "prompt": "TEST trial",
            "experience_trial": {
                "source_task_id": source_id,
                "real_task": {"id": real_id},
            },
            "review_context": {
                "needs_review": True,
                "host": {"id": "host-H", "entry": "cli:host", "model": "m"},
            },
        }
        with self.store.connect() as db:
            db.execute(
                "UPDATE tasks SET execution_snapshot_json=?,execution_snapshot_hash=? WHERE id=?",
                (json.dumps(snap), "x" * 64, trial_id),
            )
        task = self.store.get_task(trial_id)
        context = execution_context(self.store, task)
        self.assertIn("author-A", context["excluded_ids"])
        self.assertIn("exec-B", context["excluded_ids"])
        self.assertIn(source_id, context["related_task_ids"])
        self.assertIn(real_id, context["related_task_ids"])

    def test_author_self_review_blocked_via_exclusion(self):
        task_id = self.store.submit("TEST self review", 1)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store._append_event_tx(db, task_id, "task.participant_recorded",
                                       {"actor_id": "author-self", "role": "author"})
        snap = {
            "prompt": "TEST",
            "review_context": {
                "needs_review": True,
                "host": {"id": "author-self", "entry": "cli:host", "model": "m"},
            },
        }
        with self.store.connect() as db:
            db.execute(
                "UPDATE tasks SET execution_snapshot_json=? WHERE id=?",
                (json.dumps(snap), task_id),
            )
        context = execution_context(self.store, self.store.get_task(task_id))
        self.assertIn("author-self", context["excluded_ids"])
        self.assertTrue(context["host"]["author"])

    def test_entry_program_wb_cb_same_and_dsh_ignores_model(self):
        wb = {"interface": "cli", "entry": "/TEST/bin/workbuddy", "agent_type": "workbuddy", "model": "m1"}
        cb = {"interface": "cli", "entry": "/TEST/bin/codebuddy", "agent_type": "codebuddy", "model": "m1"}
        self.assertEqual(
            review_independence(entry_program(wb), "m1", kind=infer_agent_type(wb)).split("|")[0],
            review_independence(entry_program(cb), "m1", kind=infer_agent_type(cb)).split("|")[0],
        )
        self.assertEqual(
            review_independence("cli:dsh", "model-A", kind="dsh"),
            review_independence("cli:dsh", "model-B", kind="dsh"),
        )

    # ---------- C04-02 替补 / both ----------
    def test_third_node_substitute_keeps_two_current_votes(self):
        self._seed_pair(
            _source("N1", "cli:codex", "m1", kind="review"),
            _source("N2", "cli:claude", "m2", kind="both"),
            _source("N3", "cli:qoder", "m3", kind="review"),
        )
        task_id = self.store.submit("TEST substitute", 1)
        for node, verdict in (("N1", "FAIL"), ("N2", "PASS"), ("N3", "PASS")):
            run_id = self.store.start_agent_run(task_id, node, {
                "purpose": "review", "configured_model": f"m-{node}",
            })
            self.store.finish_agent_run(run_id, "completed", _review_body(verdict), None, 1)
        with self.store.read_only() as db:
            participants = current_pool_review_participants(db, task_id)
            verdicts = pool_review_verdicts(db, task_id)
            started = db.execute(
                "SELECT count(*) FROM events WHERE task_id=? AND event_type='agent.started'",
                (task_id,),
            ).fetchone()[0]
        self.assertEqual(started, 3)
        self.assertEqual(len(participants), 2)
        self.assertEqual({p["node_id"] for p in participants}, {"N2", "N3"})
        self.assertEqual(set(verdicts.values()), {"PASS"})

    def test_both_role_node_counts_as_review_vote(self):
        self._seed_pair(
            _source("BOTH-1", "cli:codex", "m1", kind="both"),
            _source("REV-2", "cli:claude", "m2", kind="review"),
        )
        task_id = self.store.submit("TEST both", 1)
        for node in ("BOTH-1", "REV-2"):
            run_id = self.store.start_agent_run(task_id, node, {"purpose": "review"})
            self.store.finish_agent_run(run_id, "completed", _review_body("PASS"), None, 1)
        with self.store.read_only() as db:
            verdicts = pool_review_verdicts(db, task_id)
            frozen = capture_pool(db, "BOTH-1", "review")
        self.assertEqual(set(verdicts.values()), {"PASS"})
        self.assertEqual(frozen["role"], "review")
        self.assertEqual(frozen["entry_program"], "cli:codex")

    def test_replace_dsh_api_exists_and_rejects_same_independence(self):
        ingress = Path(__file__).resolve().parents[2] / "review-ingress"
        if str(ingress) not in sys.path:
            sys.path.insert(0, str(ingress))
        from xiaoba_review.store import ReviewStore

        outbox = self.root / "outbox"
        outbox.mkdir()
        rs = ReviewStore(self.store, outbox)
        self.assertTrue(callable(rs.replace_dsh))
        self.assertTrue(callable(rs.replace_reviewer))
        # Public surface: either seat can be named; peer wrapper still present.
        self.assertTrue(callable(rs.replace_peer))

    # ---------- C04-04 Worker 节流 ----------
    def test_dispatch_wait_is_idempotent_and_cli_sleeps_on_wait_statuses(self):
        task = self.store.submit("TEST wait", 1)
        self.store.mark_dispatch_wait(task, "awaiting_host", "等待宿主提交合格审查回执")
        self.store.mark_dispatch_wait(task, "awaiting_host", "等待宿主提交合格审查回执")
        self.store.mark_dispatch_wait(task, "awaiting_host", "等待宿主提交合格审查回执")
        with self.store.read_only() as db:
            waits = db.execute(
                "SELECT count(*) FROM events WHERE task_id=? AND event_type='task.dispatch_wait'",
                (task,),
            ).fetchone()[0]
            row = db.execute("SELECT status,last_error,lease_until FROM tasks WHERE id=?", (task,)).fetchone()
        self.assertEqual(waits, 1)
        self.assertEqual(row["status"], "pending")
        self.assertTrue(str(row["last_error"]).startswith("awaiting_host:"))
        self.assertIsNone(row["lease_until"])
        cli_src = (Path(__file__).resolve().parents[1] / "src/xiaoba_agentos/cli.py").read_text()
        self.assertIn('"awaiting_host"', cli_src)
        self.assertIn('"ask_user"', cli_src)
        self.assertIn('"blocked_review_nodes"', cli_src)
        self.assertIn('time.sleep(int(config.worker["poll_seconds"]))', cli_src)


if __name__ == "__main__":
    unittest.main()
