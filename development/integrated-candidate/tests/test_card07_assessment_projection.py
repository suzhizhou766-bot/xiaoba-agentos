"""卡 07 C07-03：API 投影 memory.value_assessed / validation_paused 的反例。

只读临时库，不写库、不派活、不调模型。核心断言：
  - 审查员来自 value_assessed.scores 的真实节点 id，不是写死的 dsh/peer 双槽；
  - validation_paused 用 reason/failure_class/needs_info 表达，旧 dsh/peer 只作历史；
  - 读不到时 available=False，数量 0 是 available=True；坏事件被隔离。
"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ASSET = (Path(__file__).resolve().parents[1] / "src/xiaoba_agentos"
         / "review_ui_assets/workflow_query.py")
spec = importlib.util.spec_from_file_location("c07_workflow_query", ASSET)
wq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wq)


def connect(path):
    from xiaoba_agentos.store import EventStore
    store = EventStore(path); store.initialize()
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


class AssessmentProjectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "db.sqlite3"
        self.con = connect(path)
        from xiaoba_agentos.store import EventStore
        self.task = EventStore(path).submit("TEST assessment", 1)

    def tearDown(self):
        self.con.close()

    def _event(self, seq, etype, payload, task=None):
        from xiaoba_agentos.store import EventStore
        EventStore._append_event_tx(self.con, self.task, etype, payload)

    def test_no_events_is_available_with_null_fields(self):
        r = wq.assessment_projection(self.con, self.task)
        self.assertTrue(r["available"])
        self.assertIsNone(r["value_assessed"])
        self.assertIsNone(r["validation_paused"])

    def test_value_assessed_reviewers_are_dynamic_not_fixed_slots(self):
        self._event(1, "memory.value_assessed", {
            "version": 2, "candidate_sha256": "a" * 64,
            "scores": {"pool-node-Z": 8, "pool-node-A": 4}, "samples": 2,
            "average": 6, "spread": 4, "duplicate_of": [],
            "reason": "disagreement", "decision": "initial_review",
            "human_approval": False, "review_request_id": "r1"})
        r = wq.assessment_projection(self.con, self.task)
        self.assertTrue(r["available"])
        self.assertEqual([x["reviewer"] for x in r["value_assessed"]["reviewers"]],
                         ["pool-node-A", "pool-node-Z"])
        self.assertNotIn("dsh_verdict", r["value_assessed"])
        self.assertNotIn("peer_verdict", r["value_assessed"])
        self.assertEqual(r["value_assessed"]["decision"], "initial_review")
        self.assertEqual(r["value_assessed"]["version"], 2)

    def test_validation_paused_uses_reason_and_legacy_verdicts_are_secondary(self):
        self._event(1, "memory.validation_paused", {
            "round": 2, "pause_reason": "缺必填字段", "failure_class": "needs_info",
            "needs_info": True, "dsh_verdict": "NEEDS_INFO", "peer_verdict": "FAIL"})
        r = wq.assessment_projection(self.con, self.task)
        paused = r["validation_paused"]
        self.assertEqual(paused["reason"], "缺必填字段")
        self.assertEqual(paused["failure_class"], "needs_info")
        self.assertTrue(paused["needs_info"])
        self.assertEqual(paused["legacy_verdicts"],
                         {"dsh_verdict": "NEEDS_INFO", "peer_verdict": "FAIL"})

    def test_bad_value_event_is_isolated(self):
        self._event(1, "memory.value_assessed", {"scores": "not-a-dict"})
        r = wq.assessment_projection(self.con, self.task)
        self.assertTrue(r["available"])
        self.assertIsNone(r["value_assessed"])
        self.assertTrue(r["problems"])

    def test_bad_value_payload_preserves_paused_projection(self):
        self._event(1, "memory.value_assessed", [])
        self._event(2, "memory.validation_paused", {"pause_reason":"TEST paused"})
        result = wq.assessment_projection(self.con, self.task)
        self.assertTrue(result["available"])
        self.assertEqual("TEST paused",result["validation_paused"]["reason"])
        self.assertTrue(result["problems"])

    def test_bad_scores_reported_and_sqlite_failure_is_explicit(self):
        self._event(1, "memory.value_assessed", {"scores":{"good":8,"boolean":True,"big":11,"bad":"x"}})
        result = wq.assessment_projection(self.con, self.task)
        self.assertEqual([{"reviewer":"good","score":8}],result["value_assessed"]["reviewers"])
        self.assertEqual(3,len(result["problems"]))
        empty = sqlite3.connect(":memory:")
        self.addCleanup(empty.close)
        result = wq.assessment_projection(empty, "missing")
        self.assertFalse(result["available"]);self.assertTrue(result["problems"])

    def test_latest_event_wins(self):
        base = {"version": 1, "candidate_sha256": "b" * 64,
                "scores": {"n1": 9}, "samples": 1, "average": 9, "spread": 0,
                "duplicate_of": [], "reason": "average", "decision": "initial_review",
                "human_approval": False}
        self._event(1, "memory.value_assessed", base)
        self._event(2, "memory.value_assessed", dict(base, scores={"n2": 7}))
        r = wq.assessment_projection(self.con, self.task)
        self.assertEqual([x["reviewer"] for x in r["value_assessed"]["reviewers"]], ["n2"])


if __name__ == "__main__":
    unittest.main()
