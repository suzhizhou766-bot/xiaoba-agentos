"""Real portal approval guard must reach the native gate on the same lock."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from fixtures_review import _config
from xiaoba_agentos import cli
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.experience_workflow import trial_summary
from xiaoba_agentos.store import EventStore

ASSETS = Path(__file__).resolve().parents[1] / "src/xiaoba_agentos/review_ui_assets"


def module(name):
    spec = importlib.util.spec_from_file_location("trial_portal_" + name, ASSETS / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class TrialReviewCenterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        _config(self.root)
        self.path = self.root / "config.json"
        raw = json.loads(self.path.read_text())
        raw["runtime_dir"] = str(self.root / ".runtime")
        raw["memory"]["knowlp_graph_dir"] = str(self.root / "graph")
        self.path.write_text(json.dumps(raw))
        self.cfg = AppConfig.load(self.path)
        self.store = EventStore(self.cfg.database_path)
        self.store.initialize()
        self.center = module("server").Center(root=self.root, config_path=self.path)
        self.center.workflow_views = self.views

    def tearDown(self):
        self.tmp.cleanup()

    def views(self, task_id=None):
        with self.store.read_only() as conn:
            return {row["task_id"]: trial_summary(conn, row)
                    for row in conn.execute("SELECT * FROM memory_candidates")
                    if task_id is None or row["task_id"] == task_id}

    def candidate(self, kind):
        return self.store.create_authored_candidate("页面候选", "测试正文", self.cfg.runtime_dir / "memory-outbox",
                                                    author="test", experience_kind=kind)

    def test_method_card_shows_waiting_validation_and_cannot_approve_final(self):
        c = self.candidate("method")
        self.store.review_memory(c["task_id"], "delivery", "approve", "测试初审", c["evidence_sha256"])
        detail = self.center.detail(c["task_id"])
        self.assertEqual(detail["status_label"], "待隔离验证")
        self.assertTrue(detail["hash_matches"])
        self.assertNotIn("approve", detail["allowed_decisions"])
        self.assertEqual(detail["body"], Path(c["staged_path"]).read_text())

    def test_legacy_card_is_explicit_and_preserves_prior_manual_path(self):
        c = self.candidate(None)
        detail = self.center.detail(c["task_id"])
        self.assertEqual(detail["experience_validation"]["state"], "legacy_manual")
        self.assertIn("approve", detail["allowed_decisions"])

    def test_browser_atomic_guard_accepts_delivery_and_native_gate_refuses_unvalidated_final(self):
        c = self.candidate("method")
        action = module("review_action")
        original = cli.EventStore
        try:
            detail = self.center.detail(c["task_id"])
            result = action.execute({"task_id": c["task_id"], "stage": "delivery", "decision": "approve",
                                     "note": "测试人工初审", "expected": detail["expected"]}, self.path,
                                    Path(__file__).resolve().parents[1] / "src")
            self.assertEqual(result["exit_code"], 0, result)
            detail = self.center.detail(c["task_id"])
            result = action.execute({"task_id": c["task_id"], "stage": "final", "decision": "approve",
                                     "note": "测试未验证时的拒绝", "expected": detail["expected"]}, self.path,
                                    Path(__file__).resolve().parents[1] / "src")
            self.assertNotEqual(result["exit_code"], 0, result)
            with self.store.read_only() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM memory_reviews WHERE task_id=? AND stage='final'", (c["task_id"],)).fetchone()[0], 0)
        finally:
            cli.EventStore = original
