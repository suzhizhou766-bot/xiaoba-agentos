"""底线 B11：两道批准各自绑定它批过的那一份内容。

现有测试只走外层哈希检查（送审哈希对不上登记的证据就拒），从没到达
`review_memory()` 的初审绑定分支和 `mark_memory_published()` 的终审绑定分支。
这里的夹具先批准、再换掉候选内容并重新登记证据哈希，让外层检查通过，
于是还能拦住请求的只剩这两处批准绑定。
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fixtures_review import _config
from xiaoba_agentos.memory import MemoryBridge
from xiaoba_agentos.review_flow import execute_review
from xiaoba_agentos.store import EventStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BODY_APPROVED = "初审批准时登记的正文。"
BODY_CHANGED = "批准之后被换掉的正文，内容不同、哈希也不同。"
FINAL_BINDING_ERROR = "final review is not bound to the approved delivery candidate"
PUBLICATION_BINDING_ERROR = "memory publication is not bound to the final approved candidate"
EXPERIENCE_DIR = "70_复利规则/经验"


def _approve(store, task_id, sha256, stages):
    for stage in stages:
        store.review_memory(task_id, stage, "approve", "用户在裁决中心批准", sha256)


def _re_register_with_changed_content(store, task_id, staged):
    """换掉候选内容，并按新内容重新登记证据哈希（外层哈希检查因此会通过）。"""
    staged.write_text(BODY_CHANGED, encoding="utf-8")
    changed = hashlib.sha256(staged.read_bytes()).hexdigest()
    store.stage_memory_candidate(task_id, str(staged), changed)
    return changed


class ApprovalBindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.config = _config(self.root)
        # 索引构建脚本随发布包存在，这里指向本树，使临时发布能走完整流程。
        self.config.raw["project_dir"] = str(PROJECT_ROOT)
        self.config.raw["memory"].update({
            "publish_enabled": True,
            "obsidian_subdir": EXPERIENCE_DIR,
            "catalog_note": EXPERIENCE_DIR + "索引.md",
            "knowlp_graph_dir": str(self.root / "runtime/graph"),
        })
        catalog = self.vault / (EXPERIENCE_DIR + "索引.md")
        catalog.parent.mkdir(parents=True)
        (self.vault / "root.md").write_text(
            '---\nid: "root"\nparent: ""\nstatus: "active"\n---\n\n'
            "# Root\n\n- [[" + EXPERIENCE_DIR + "索引]]\n",
            encoding="utf-8",
        )
        catalog.write_text(
            '---\nid: "experiences"\nparent: "root"\nstatus: "active"\n---\n\n# 经验索引\n',
            encoding="utf-8",
        )
        self.store = EventStore(self.config.database_path)
        self.store.initialize()
        self.bridge = MemoryBridge(self.config, self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def _candidate(self):
        created = self.store.create_authored_candidate(
            "绑定测试候选", BODY_APPROVED, self.bridge.outbox, author="test"
        )
        return created["task_id"], Path(created["staged_path"]), created["evidence_sha256"]

    def _reviews(self, task_id):
        with self.store.read_only() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT stage,decision,candidate_sha256 FROM memory_reviews WHERE task_id=?"
                " ORDER BY created_at,id", (task_id,)
            )]

    def _events(self, task_id, event_type):
        with self.store.read_only() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM events WHERE task_id=? AND event_type=?",
                (task_id, event_type),
            ).fetchone()[0]

    def _sidecar(self, staged):
        path = Path(str(staged) + ".status.json")
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def _vault_files(self):
        return {
            str(path.relative_to(self.vault)): path.read_bytes()
            for path in self.vault.rglob("*") if path.is_file()
        }

    def test_final_review_refuses_content_the_delivery_review_never_approved(self):
        """初审批准后换内容并重登记证据：终审只能被初审绑定拒掉，且不留半条终审记录。"""
        task_id, staged, approved = self._candidate()
        _approve(self.store, task_id, approved, ("delivery",))
        changed = _re_register_with_changed_content(self.store, task_id, staged)

        row = self.store.get_memory_candidate(task_id)
        # 外层哈希检查此时是通的：送审哈希 == 重新登记的证据哈希。
        self.assertEqual(row["evidence_sha256"], changed)
        self.assertEqual(changed, hashlib.sha256(staged.read_bytes()).hexdigest())
        self.assertEqual("delivery_approved", row["status"])

        with self.assertRaisesRegex(ValueError, FINAL_BINDING_ERROR):
            self.store.review_memory(task_id, "final", "approve", "终审批准", changed)

        after = self.store.get_memory_candidate(task_id)
        self.assertEqual("delivery_approved", after["status"], "被拒的终审不得改状态")
        self.assertEqual(
            [("delivery", "approve")],
            [(item["stage"], item["decision"]) for item in self._reviews(task_id)],
            "终审被拒不得留下半条终审记录",
        )
        self.assertEqual(1, self._events(task_id, "memory.reviewed"), "只允许初审那一条审核事件")
        self.assertEqual(0, self._events(task_id, "memory.published"))
        self.assertNotIn(self._sidecar(staged)["status"], {"pending_final_review", "published"})
        receipt = self.store.memory_review_receipt(task_id)
        self.assertEqual(0, receipt["final_approved_count"])
        self.assertFalse(receipt["fully_approved"])
        self.assertFalse(receipt["publish_ready"])
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_execute_review_final_approve_is_blocked_by_the_delivery_binding(self):
        """真实审核入口（裁决中心与 CLI 同一条路）也被初审绑定拦住：不发布、不落笔记。"""
        task_id, staged, approved = self._candidate()
        _approve(self.store, task_id, approved, ("delivery",))
        _re_register_with_changed_content(self.store, task_id, staged)
        vault_before = self._vault_files()

        code, payload = execute_review(self.config, self.store, task_id, "final", "approve", "")

        self.assertEqual(2, code, payload)
        self.assertEqual(FINAL_BINDING_ERROR, payload["error"])
        self.assertEqual("delivery_approved", self.store.get_memory_candidate(task_id)["status"])
        self.assertEqual(1, self._events(task_id, "memory.reviewed"), "终审那一条审核事件不得出现")
        self.assertEqual(0, self._events(task_id, "memory.published"))
        self.assertEqual(vault_before, self._vault_files(), "被拒的终审不得在临时 Vault 留下正式笔记")

    def test_publication_refuses_content_the_final_review_never_approved(self):
        """双阶段批准后换证据：发布只能被终审绑定拒掉，不产生已发布状态或发布事件。"""
        task_id, staged, approved = self._candidate()
        _approve(self.store, task_id, approved, ("delivery", "final"))
        changed = _re_register_with_changed_content(self.store, task_id, staged)
        vault_before = self._vault_files()

        row = self.store.get_memory_candidate(task_id)
        self.assertEqual("pending_final_review", row["status"])
        self.assertEqual(row["evidence_sha256"], changed)

        note = self.vault / EXPERIENCE_DIR / f"绑定测试候选-{task_id[:8]}.md"
        with self.assertRaisesRegex(ValueError, PUBLICATION_BINDING_ERROR):
            self.store.mark_memory_published(task_id, str(note))

        after = self.store.get_memory_candidate(task_id)
        self.assertEqual("pending_final_review", after["status"], "绑定不符不得写成已发布")
        self.assertIsNone(after["published_path"])
        self.assertIsNone(after["published_at"])
        self.assertEqual(0, self._events(task_id, "memory.published"))
        self.assertEqual(
            [("delivery", "approve"), ("final", "approve")],
            [(item["stage"], item["decision"]) for item in self._reviews(task_id)],
            "发布被拒不得追加或改写审核记录",
        )
        self.assertFalse(note.exists(), "发布被拒不得留下正式笔记")
        self.assertEqual(vault_before, self._vault_files(), "发布被拒不得改动临时 Vault")
        self.assertNotEqual("published", self._sidecar(staged)["status"])
        receipt = self.store.memory_review_receipt(task_id)
        self.assertEqual(1, receipt["final_approved_count"])
        self.assertFalse(receipt["fully_approved"], "哈希已换，终审批准不再覆盖这一份内容")
        self.assertFalse(receipt["active_retrieval"])
        self.assertTrue(self.store.verify_event_chain()["ok"])


if __name__ == "__main__":
    unittest.main()
