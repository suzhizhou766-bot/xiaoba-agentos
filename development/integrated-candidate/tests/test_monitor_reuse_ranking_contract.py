"""Monitor reuse ranking follows the core read-only ledger projection."""
import sys
import tempfile
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1]
REPO_ROOT = CORE.parents[1]
MONITOR = REPO_ROOT / "monitor"
sys.path[:0] = [str(CORE / "src"), str(MONITOR)]

from monitor_core.modules.overview import permanent_total_ranking
from xiaoba_agentos.experience_workflow import digest
from xiaoba_agentos.reuse_scores import snapshot_tx
from xiaoba_agentos.store import EventStore


class MonitorReuseRankingContractTests(unittest.TestCase):
    def append_contribution(self, store, root, name, *, omit_application=False,
                            application_changes=None, score_changes=None):
        artifact = {"path": str(root / "artifact.md"), "sha256": "b" * 64, "version": "v1"}
        application = {
            "application_id": "application-" + name,
            "candidate_id": "source-" + name,
            "candidate_sha256": "a" * 64,
            "formal_reuse": True,
            "real_task": {"id": "task-" + name, "workspace": str(root)},
            "artifact": artifact,
            "execution_evidence": [artifact],
        }
        application.update(application_changes or {})
        with store.connect(refresh_views=False) as conn:
            if not omit_application:
                store._append_event_tx(conn, None, "memory.application_recorded", application)
            delivery_id = store._append_event_tx(conn, None, "task.delivery_recorded", {
                "decision": "accept",
                "real_task_id": "task-" + name,
                "message_id": "message-" + name,
                "input_sha256": "c" * 64,
                "artifact": artifact,
                "application_ids": [application["application_id"]],
            })
            score = {
                "source_task_id": "source-" + name,
                "source_sha256": "a" * 64,
                "real_task_id": "task-" + name,
                "application_ids": [application["application_id"]],
                "delivery_event_id": delivery_id,
                "score": 1,
                "used_at": 1000.0,
            }
            score["settlement_key"] = digest([
                score["real_task_id"], score["source_task_id"], score["application_ids"], delivery_id,
            ])
            score.update(score_changes or {})
            store._append_event_tx(conn, None, "memory.reuse_scored", score)

    def build_store(self, root, name, *, omit_application=False, application_changes=None,
                    score_changes=None, include_control=False):
        store = EventStore(root / (name + ".sqlite3"))
        store.initialize()
        self.append_contribution(store, root, name, omit_application=omit_application,
                                 application_changes=application_changes, score_changes=score_changes)
        if include_control:
            self.append_contribution(store, root, "trusted")
        return store

    def assert_projection_matches(self, store, *, valid, expected_source):
        with store.read_only() as conn:
            before = conn.total_changes
            core = snapshot_tx(conn)
            monitor = permanent_total_ranking(conn)
            self.assertEqual(before, conn.total_changes, "ledger projections must not write")
        self.assertEqual(core["available"], monitor["available"])
        if valid:
            self.assertTrue(core["ok"])
            self.assertEqual([], core["problems"])
            self.assertEqual([{"source_task_id": expected_source, "score": 1, "used": 1}], monitor["items"])
            self.assertEqual([], monitor["problems"])
        else:
            self.assertTrue(core["available"])
            self.assertFalse(core["ok"])
            self.assertEqual([{"source_task_id": expected_source, "score": 1, "used": 1}], monitor["items"])
            self.assertEqual([expected_source], list(core["scores"]))
            self.assertTrue(core["problems"])
            self.assertEqual(1, len(monitor["problems"]))
            self.assertIn("memory.reuse_scored:", monitor["problems"][0])
            self.assertEqual([str(problem) for problem in core["problems"]], monitor["problems"])

    def test_valid_binding_counts_once_and_six_binding_errors_are_isolated(self):
        cases = (
            ("missing_application", {"omit_application": True, "include_control": True}),
            ("wrong_candidate", {"application_changes": {"candidate_id": "different-source"}, "include_control": True}),
            ("wrong_version", {"application_changes": {"candidate_sha256": "d" * 64}, "include_control": True}),
            ("wrong_task", {"application_changes": {"real_task": {"id": "different-task", "workspace": "/tmp"}}, "include_control": True}),
            ("wrong_artifact", {"application_changes": {"artifact": {"path": "other", "sha256": "e" * 64}}, "include_control": True}),
            ("wrong_settlement_key", {"score_changes": {"settlement_key": "f" * 64}, "include_control": True}),
        )
        with tempfile.TemporaryDirectory(prefix="monitor-reuse-ranking-") as tmp:
            root = Path(tmp)
            baseline = self.build_store(root, "baseline")
            self.assert_projection_matches(baseline, valid=True, expected_source="source-baseline")

            for name, options in cases:
                with self.subTest(case=name):
                    store = self.build_store(root, name, **options)
                    self.assertTrue(store.verify_event_chain()["ok"])
                    self.assert_projection_matches(store, valid=False, expected_source="source-trusted")


if __name__ == "__main__":
    unittest.main()
