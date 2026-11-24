"""Wave 4 Category O regression tests: W4-O1, W4-O2, W4-O3."""
from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

CANDIDATE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CANDIDATE / "src"))
sys.path.insert(0, str(CANDIDATE / "tests"))

from fixtures_review import _admit_authored_candidate, _config  # noqa: E402
from xiaoba_agentos import cli  # noqa: E402
from xiaoba_agentos.config import AppConfig  # noqa: E402
from xiaoba_agentos.reuse_scores import projection_tx, snapshot_tx  # noqa: E402
from xiaoba_agentos.store import (  # noqa: E402
    EventStore,
    REVIEW_STAGE_ALIASES,
    REUSE_METRIC_GLOSSARY,
    normalize_review_stage,
)


class Wave4CategoryOTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.cfg = _config(self.root / "proj", "config/agentos.json")
        self.store = EventStore(self.cfg.database_path)
        self.store.initialize()

    def test_w4_o1_initial_stage_alias_works_and_preserves_delivery_compat(self):
        self.assertEqual("delivery", REVIEW_STAGE_ALIASES["initial"])
        self.assertEqual("delivery", normalize_review_stage("initial"))
        self.assertEqual("delivery", normalize_review_stage("delivery"))
        self.assertEqual("final", normalize_review_stage("final"))
        self.assertEqual("all", normalize_review_stage("all", allow_all=True))
        with self.assertRaises(ValueError):
            normalize_review_stage("unknown")

        created = self.store.create_authored_candidate(
            "TEST 初审别名", "测试正文", self.cfg.runtime_dir / "memory-outbox",
            author="test", experience_kind="rule",
        )
        admitted = _admit_authored_candidate(self.store, created)
        task_id = admitted["task_id"]

        # review-list --stage initial matches review-list --stage delivery
        out_initial = io.StringIO()
        with redirect_stdout(out_initial):
            rc = cli.main(["--config", str(self.cfg.path), "review-list", "--stage", "initial"])
        self.assertEqual(0, rc)
        payload_initial = json.loads(out_initial.getvalue())
        self.assertEqual("delivery", payload_initial["stage"])
        self.assertEqual("initial", payload_initial["requested_stage"])
        self.assertEqual([task_id], [item["task_id"] for item in payload_initial["items"]])

        # review --stage initial approves initial review cleanly, storing canonical 'delivery'
        out_review = io.StringIO()
        with redirect_stdout(out_review):
            rc = cli.main([
                "--config", str(self.cfg.path), "review", task_id,
                "--stage", "initial", "--decision", "approve", "--note", "初审通过",
            ])
        self.assertEqual(0, rc, out_review.getvalue())
        res = json.loads(out_review.getvalue())
        self.assertEqual("delivery", res["stage"])
        self.assertEqual("delivery_approved", res["memory_status"])

        # Old 'delivery' flag still works identically on another candidate
        created2 = self.store.create_authored_candidate(
            "TEST 旧名兼容", "测试正文二", self.cfg.runtime_dir / "memory-outbox",
            author="test", experience_kind="rule",
        )
        admitted2 = _admit_authored_candidate(self.store, created2)
        out_legacy = io.StringIO()
        with redirect_stdout(out_legacy):
            rc2 = cli.main([
                "--config", str(self.cfg.path), "review", admitted2["task_id"],
                "--stage", "delivery", "--decision", "approve", "--note", "旧参通过",
            ])
        self.assertEqual(0, rc2, out_legacy.getvalue())
        self.assertEqual("delivery_approved", json.loads(out_legacy.getvalue())["memory_status"])

    def test_w4_o2_legacy_reuse_count_and_formal_reuse_score_are_explicitly_labeled(self):
        session = self.store.start_session("Asia/Shanghai")
        created = self.store.create_authored_candidate(
            "TEST 指标消歧", "测试正文", self.cfg.runtime_dir / "memory-outbox",
            author="test", experience_kind="rule",
        )
        admitted = _admit_authored_candidate(self.store, created)
        task_id = admitted["task_id"]
        with self.store.connect() as conn:
            conn.execute("UPDATE tasks SET session_id=? WHERE id=?", (session["id"], task_id))
            conn.execute("UPDATE memory_candidates SET reuse_count=7 WHERE task_id=?", (task_id,))
            self.store._append_event_tx(conn, None, "memory.reuse_scored", {
                "source_task_id": task_id,
                "real_task_id": "rt-w4o2",
                "score": 1,
                "used_at": 1700000000.0,
            })

        queue = self.store.review_queue("initial")
        self.assertEqual(1, len(queue))
        self.assertEqual(7, queue[0]["legacy_reuse_count"])
        self.assertEqual(7, queue[0]["reuse_count"])
        self.assertEqual(1, queue[0]["formal_reuse_score"])

        packet = self.store.session_review_packet(session["id"])
        self.assertEqual(REUSE_METRIC_GLOSSARY, packet["reuse_metric_glossary"])
        row = [r for r in packet["tasks"] if r["id"] == task_id][0]
        self.assertEqual(7, row["legacy_reuse_count"])
        self.assertEqual(1, row["formal_reuse_score"])

        with self.store.read_only() as conn:
            proj = projection_tx(conn)
            self.assertEqual(1, proj[task_id]["formal_reuse_score"])
            self.assertEqual(proj[task_id]["score"], proj[task_id]["formal_reuse_score"])
            snap = snapshot_tx(conn)
            self.assertEqual("formal_reuse_score", snap["metric"])
            self.assertEqual(1, snap["scores"][task_id]["formal_reuse_score"])

    def test_w4_o3_cli_help_groups_all_registered_subcommands_into_four_sections(self):
        parser = cli.build_parser()
        sub_actions = [a for a in parser._actions if hasattr(a, "choices") and isinstance(a.choices, dict)]
        self.assertEqual(1, len(sub_actions))
        registered = set(sub_actions[0].choices.keys())

        grouped = []
        self.assertEqual(4, len(cli.CLI_COMMAND_GROUPS))
        self.assertEqual(8, len(cli.CLI_COMMAND_GROUPS[0][1]), "Daily group must contain exactly 8 commands")
        for _, entries in cli.CLI_COMMAND_GROUPS:
            for cmd_name, desc in entries:
                self.assertTrue(desc.strip(), f"missing description for {cmd_name}")
                grouped.append(cmd_name)

        self.assertEqual(len(grouped), len(set(grouped)), "duplicate command in CLI_COMMAND_GROUPS")
        self.assertEqual(registered, set(grouped), "CLI_COMMAND_GROUPS must match registered subcommands 1:1")

        help_text = parser.format_help()
        for group_title, _ in cli.CLI_COMMAND_GROUPS:
            self.assertIn(f"[{group_title}]", help_text)
        self.assertIn("--stage initial", help_text)


if __name__ == "__main__":
    unittest.main()
