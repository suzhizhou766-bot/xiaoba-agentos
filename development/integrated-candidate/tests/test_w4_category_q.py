"""Wave 4 Category Q regression tests: W4-Q1, W4-Q2, W4-Q3."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

CANDIDATE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CANDIDATE / "src"))

from xiaoba_agentos.adapters import PermanentAdapterError, build_adapter  # noqa: E402
from xiaoba_agentos.config import AgentConfig  # noqa: E402
from xiaoba_agentos.legacy_compat import (  # noqa: E402
    LEGACY_AGENT_NAMES,
    LEGACY_JUDGE_EVENT_TYPE,
    LEGACY_STANDBY_EVENT_TYPE,
    RETIRED_COMPONENTS,
    WORKBUDDY_RETIRED_CODE,
    allow_legacy_standby_cover,
    read_latest_judge_event,
    read_prior_standby_substitution,
    verify_historical_replay_readonly,
)
from xiaoba_agentos.recovery import classify_failure  # noqa: E402
from xiaoba_agentos.store import EventStore  # noqa: E402


class Wave4CategoryQTests(unittest.TestCase):
    def test_w4_q1_legacy_compat_reads_history_readonly_and_disables_pool_standby_writes(self):
        self.assertEqual({"agy_review", "dsh_review", "qoder_review"}, set(LEGACY_AGENT_NAMES))
        self.assertFalse(allow_legacy_standby_cover("pool"))
        self.assertFalse(allow_legacy_standby_cover("awaiting_host"))
        self.assertTrue(allow_legacy_standby_cover("mock"))

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "history.sqlite3"
            store = EventStore(db)
            store.initialize()
            tid = store.submit("TEST legacy replay", max_attempts=1)
            store.append_event(tid, LEGACY_JUDGE_EVENT_TYPE, {"winner": "dsh_review", "successful_agents": 2})
            store.append_event(tid, LEGACY_STANDBY_EVENT_TYPE, {"replaced": "agy_review", "standby": "qoder_review"})

            with store.read_only() as conn:
                before = conn.total_changes
                judge = read_latest_judge_event(conn, tid)
                sub = read_prior_standby_substitution(conn, tid)
                self.assertEqual(before, conn.total_changes)
            self.assertEqual("dsh_review", judge["winner"])
            self.assertEqual("qoder_review", sub["standby"])

            replay = verify_historical_replay_readonly(db)
            self.assertTrue(replay["ok"], replay)
            self.assertEqual(0, replay["writes_performed"])
            self.assertEqual(1, replay["legacy_counts"][LEGACY_JUDGE_EVENT_TYPE])
            self.assertEqual(1, replay["legacy_counts"][LEGACY_STANDBY_EVENT_TYPE])

    def test_w4_q2_retired_components_registry_and_hard_refusal_guards(self):
        for key in ("workbuddy", "headroom", "legacy_review_port_8797", "peer_auto_repair", "strategy_evolution"):
            self.assertIn(key, RETIRED_COMPONENTS)

        # WorkBuddy built-in engine remains hard-refused
        from xiaoba_agentos.pool_dispatch import (
            WORKBUDDY_RETIRED_MESSAGE,
            infer_agent_type,
            retired_engine_warnings,
        )
        wb_node = {"id": "old-wb", "name": "OldWB", "agent_type": "workbuddy", "interface": "cli", "entry": "/bin/workbuddy"}
        cb_node = {"id": "new-cb", "name": "NewCB", "agent_type": "codebuddy", "interface": "cli", "entry": "/bin/codebuddy"}
        self.assertEqual("workbuddy", infer_agent_type(wb_node))
        self.assertEqual("codebuddy", infer_agent_type(cb_node))
        self.assertEqual(1, len(retired_engine_warnings([wb_node, cb_node])))
        self.assertIn("已退役", WORKBUDDY_RETIRED_MESSAGE)
        self.assertEqual("engine_retired", WORKBUDDY_RETIRED_CODE)

        # Headroom fault remains internal (never triggers external standby substitution)
        cls = classify_failure(RuntimeError("headroom readiness unavailable"))
        self.assertEqual("internal_headroom", cls.name)
        self.assertFalse(cls.external)

    def test_w4_q3_docs_explicitly_record_schema7_six_retired_tables_kept_without_drop(self):
        arch = (CANDIDATE / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")
        ops = (CANDIDATE / "docs/OPERATIONS.md").read_text(encoding="utf-8")
        for doc_name, text in (("ARCHITECTURE.md", arch), ("OPERATIONS.md", ops)):
            self.assertIn("Schema 7", text, doc_name)
            for table in (
                "strategies",
                "strategy_audit",
                "evaluations",
                "evaluation_cases",
                "evaluation_runs",
                "promotion_gates",
            ):
                self.assertIn(table, text, f"{doc_name} missing {table}")
            self.assertIn("删表迁移", text, doc_name)


if __name__ == "__main__":
    unittest.main()
