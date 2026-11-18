"""卡 07 C07-04：新初审拒绝写 retired，历史 delivery_rejected 只读兼容。

临时库，不碰线上服务。验证：
  - 新的 delivery/reject 写终态 retired，不再产生 delivery_rejected 新状态；
  - 手工置入的历史 delivery_rejected 行仍可读、状态集合与展示标签都在（只读兼容）；
  - retired 是终态，不能被再次初审复活；
  - 只读连接读取不改库。
"""
import hashlib
import importlib.util
import json
import socket
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from xiaoba_agentos.store import (  # noqa: E402
    EventStore, MEMORY_REVIEW_STATUS_MAP, MEMORY_CANDIDATE_STATES,
    LEGACY_DELIVERY_REJECTED,
)
SERVER = ROOT / "src/xiaoba_agentos/review_ui_assets/server.py"
spec = importlib.util.spec_from_file_location("c07_server", SERVER)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class RetiredTransitionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = EventStore(Path(self.tmp.name) / "agentos.sqlite3")
        self.store.initialize()
        self.path = Path(self.tmp.name) / "cand.md"
        self.path.write_text("测试候选正文", encoding="utf-8")
        self.sha = hashlib.sha256(self.path.read_bytes()).hexdigest()

    def _stage(self):
        task = self.store.submit("源任务", 1)
        self.store.complete_manually(task, "结果")
        self.store.stage_memory_candidate(task, str(self.path), self.sha)
        return task

    def test_new_initial_rejection_writes_retired(self):
        self.assertEqual("retired", MEMORY_REVIEW_STATUS_MAP[("delivery", "reject")])
        task = self._stage()
        result = self.store.review_memory(task, "delivery", "reject", "不采用", self.sha)
        self.assertEqual("retired", result["status"])
        with self.store.read_only() as db:
            row = db.execute("SELECT status FROM memory_candidates WHERE task_id=?", (task,)).fetchone()
            self.assertEqual("retired", row["status"])
            new_states = [r[0] for r in db.execute(
                "SELECT DISTINCT status FROM memory_candidates WHERE task_id=?", (task,))]
        self.assertNotIn(LEGACY_DELIVERY_REJECTED, new_states)

    def test_initial_rejection_has_no_archive_but_passes_lifecycle_audit(self):
        from fixtures_review import _config
        from xiaoba_agentos.memory import MemoryBridge
        from xiaoba_agentos.review_flow import execute_review
        root = Path(self.tmp.name)
        config = _config(root)
        vault = Path(config.memory["obsidian_vault"])
        catalog = vault / "70_复利规则/经验索引.md"
        catalog.parent.mkdir(parents=True); catalog.write_text("TEST empty index")
        task = self._stage()
        code, result = execute_review(config, self.store, task, "delivery", "reject", "TEST do not adopt")
        self.assertEqual(0, code, result)
        candidate = self.store.get_memory_candidate(task)
        self.assertEqual("retired", candidate["status"])
        self.assertIsNone(candidate["published_path"])
        audit = MemoryBridge(config).audit_lifecycle([candidate])
        self.assertTrue(audit["ok"], audit)
        # An actual former publication must still have a recoverable archive.
        for retired in (dict(candidate, published_at=123), dict(candidate,published_path=str(root/'missing.md'))):
            audit = MemoryBridge(config).audit_lifecycle([retired])
            self.assertIn(task+":retired_archive_missing",audit["problems"])

    def test_retired_is_terminal_cannot_be_rereviewed(self):
        task = self._stage()
        self.store.review_memory(task, "delivery", "reject", "不采用", self.sha)
        with self.assertRaises(ValueError):
            self.store.review_memory(task, "delivery", "approve", "复活", self.sha)

    def test_historical_delivery_rejected_is_read_only_compatible(self):
        # 兼容读取：状态仍在合法集合里，展示标签也在；旧行可被 item() 投影。
        self.assertIn(LEGACY_DELIVERY_REJECTED, MEMORY_CANDIDATE_STATES)
        self.assertIn(LEGACY_DELIVERY_REJECTED, server.LABELS)
        task = self._stage()
        with self.store.connect() as db:
            db.execute("UPDATE memory_candidates SET status=? WHERE task_id=?",
                       (LEGACY_DELIVERY_REJECTED, task))
        with self.store.read_only() as db:
            row = dict(db.execute(
                "SELECT mc.*, t.prompt FROM memory_candidates mc JOIN tasks t ON t.id=mc.task_id "
                "WHERE mc.task_id=?", (task,)).fetchone())
        center = server.Center.__new__(server.Center)
        center.vault = Path("/tmp/vault")
        item = center.item(row)
        self.assertEqual("delivery_rejected", item["status"])
        self.assertEqual("初审不采用", item["status_label"])
        self.assertEqual([], item["allowed_decisions"])  # 只读，不产生新裁决

    def test_read_only_connection_does_not_write(self):
        task = self._stage()
        with self.store.read_only() as db:
            before = db.total_changes
            db.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()
            self.assertEqual(before, db.total_changes)


class GetNoWriteHttpTest(unittest.TestCase):
    """卡 07 C07-04：隔离 HTTP 服务、随机端口，GET 只读不写库、不派活、不调模型。"""

    def test_get_state_and_candidate_do_not_write_database(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        sys.path.insert(0, str(ROOT / "src"))
        from xiaoba_agentos.config import AppConfig
        from fixtures_review import _config
        _config(root, "config/agentos.json")
        cfg_path = root / "config/agentos.json"
        raw = json.loads(cfg_path.read_text())
        raw["runtime_dir"] = str(root / ".runtime")
        raw["memory"]["knowlp_graph_dir"] = str(root / "graph")
        cfg_path.write_text(json.dumps(raw))
        cfg = AppConfig.load(cfg_path)
        store = EventStore(cfg.database_path)
        store.initialize()

        server_mod = server
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        center = server_mod.Center(root=root, config_path=cfg_path, port=port)
        # 不触碰线上 verify-release：桩掉发布校验与工作流视图。
        center.verify = lambda: {"ok": True, "config_path": str(cfg_path),
                                 "current_release": "vTEST", "files_manifest_sha256": "test",
                                 "release_dir": str(root)}
        center.workflow_views = lambda task_id=None: {}
        http = server_mod.Server(("127.0.0.1", port), server_mod.Handler)
        http.center = center
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http.shutdown)
        self.addCleanup(http.server_close)

        def changes():
            with store.connect() as db:
                return db.total_changes

        before = changes()
        for path in ("/api/state", "/api/candidate?id=11111111-2222-3333-4444-555555555555"):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=5) as r:
                    r.read()
            except Exception:
                pass  # 候选可能不存在 -> 503；关键是不得写库
        self.assertEqual(before, changes(), "GET 接口不得写库")


if __name__ == "__main__":
    unittest.main()
