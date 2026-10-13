"""底线 B11：发布前绑定门禁（CLI 与共用审核入口同一条路）。

`review_memory` 的终审绑定与 `mark_memory_published` 的后置绑定守卫都发生在
数据库层，而发布是「先写 Vault 和索引、再结算数据库」。候选经过两阶段批准旧
内容后又被重新登记新哈希时，外层哈希检查会通过，数据库结算才被拒——但那时
正式笔记和目录/图索引已经落盘。本文件锁定：门禁必须在任何 Vault/目录/图索引
写入之前生效，且检查到落盘、数据库结算之间没有无锁间隙。

全部用例只在临时配置、临时 SQLite 和临时 Vault 上运行；预置一篇已合法发布的
背景经验，使目录/图索引非空，「拒绝前后字节完全相同」才有可比内容。
"""

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures_review import _config, _run_cli
from xiaoba_agentos.memory import MemoryBridge
from xiaoba_agentos.review_flow import execute_review
from xiaoba_agentos.store import EventStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BODY_APPROVED = "两阶段批准时登记的正文。"
BODY_CHANGED = "批准之后被换掉的正文，内容不同、哈希也不同。"
EXPERIENCE_DIR = "70_复利规则/经验"
BINDING_ERROR = "memory publication is not bound to the final approved candidate"


class PublicationGateTest(unittest.TestCase):
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
        # CLI 从磁盘重新读配置：把改动后的 raw 写回去，真实命令才看得到。
        self.config.path.write_text(json.dumps(self.config.raw, ensure_ascii=False), encoding="utf-8")
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
        self._publish_background_memory()

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------------ 夹具
    def _publish_background_memory(self):
        """预置一篇合法发布的经验，让目录与图索引非空。"""
        created = self.store.create_authored_candidate(
            "已发布背景经验", "背景正文，用来让索引非空。", self.bridge.outbox, author="test"
        )
        for stage in ("delivery", "final"):
            code, payload = execute_review(
                self.config, self.store, created["task_id"], stage, "approve", ""
            )
            self.assertEqual(0, code, payload)
        self.assertEqual("published", self.store.get_memory_candidate(created["task_id"])["status"])

    def _candidate(self):
        created = self.store.create_authored_candidate(
            "绑定测试候选", BODY_APPROVED, self.bridge.outbox, author="test"
        )
        return created["task_id"], Path(created["staged_path"]), created["evidence_sha256"]

    def _approve(self, task_id, sha256, stages):
        for stage in stages:
            self.store.review_memory(task_id, stage, "approve", "用户在裁决中心批准", sha256)

    def _re_register_with_changed_content(self, task_id, staged):
        staged.write_text(BODY_CHANGED, encoding="utf-8")
        changed = hashlib.sha256(staged.read_bytes()).hexdigest()
        self.store.stage_memory_candidate(task_id, str(staged), changed)
        return changed

    def _cli(self, argv):
        return _run_cli(["--config", str(self.config.path)] + argv)

    def _vault_bytes(self):
        return {
            str(path.relative_to(self.vault)): path.read_bytes()
            for path in self.vault.rglob("*") if path.is_file()
        }

    def _graph_bytes(self):
        graph = Path(self.config.raw["memory"]["knowlp_graph_dir"])
        if not graph.is_dir():
            return {}
        return {
            str(path.relative_to(graph)): path.read_bytes()
            for path in graph.rglob("*") if path.is_file()
        }

    def _notes(self, vault_bytes, needle):
        return [
            path for path, content in vault_bytes.items()
            if path.startswith(EXPERIENCE_DIR) and path.endswith(".md") and needle in content.decode("utf-8")
        ]

    def _events(self, task_id, event_type):
        with self.store.read_only() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM events WHERE task_id=? AND event_type=?",
                (task_id, event_type),
            ).fetchone()[0]

    def _second_writer_blocked(self):
        """真实第二个连接此刻能否拿到写锁。timeout=0：立刻给出答案。"""
        probe = sqlite3.connect(str(self.store.path), timeout=0)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
            return False
        except sqlite3.OperationalError:
            return True
        finally:
            probe.close()

    # ------------------------------------- 真实 CLI：批准不覆盖新证据就拒绝
    def test_cli_refuses_when_the_final_approval_does_not_cover_the_registered_evidence(self):
        task_id, staged, approved = self._candidate()
        self._approve(task_id, approved, ("delivery", "final"))
        changed = self._re_register_with_changed_content(task_id, staged)
        vault_before, graph_before = self._vault_bytes(), self._graph_bytes()

        code, payload = self._cli(["publish-memory", "--task-id", task_id])

        self.assertEqual(1, code)
        self.assertEqual("reconciliation_required", payload["status"])
        self.assertIn(BINDING_ERROR, json.dumps(payload, ensure_ascii=False))
        # 关键断言：不是只看数据库状态和事件数，而是 Vault 与目录/图索引逐字节相同。
        self.assertEqual(vault_before, self._vault_bytes(), "被拒的发布不得改动临时 Vault 一个字节")
        self.assertEqual(graph_before, self._graph_bytes(), "被拒的发布不得改动目录/图索引")
        row = self.store.get_memory_candidate(task_id)
        self.assertEqual(changed, row["evidence_sha256"], "外层哈希检查此时是通的")
        self.assertNotEqual("published", row["status"])
        self.assertIsNone(row["published_path"])
        self.assertEqual(0, self._events(task_id, "memory.published"))
        self.assertEqual([], self._notes(self._vault_bytes(), BODY_CHANGED))

    # ------------------------- 共用审核入口：终审批准与落盘之间被真实连接改变证据
    def test_shared_review_entry_refuses_evidence_changed_after_final_approval(self):
        task_id, staged, approved = self._candidate()
        self._approve(task_id, approved, ("delivery", "final"))
        vault_before, graph_before = self._vault_bytes(), self._graph_bytes()

        real_review = self.store.review_memory

        def review_then_change(*args, **kwargs):
            result = real_review(*args, **kwargs)
            # 真实第二个连接（store 自己开连接）：终审批准之后、落盘之前改证据。
            self._re_register_with_changed_content(task_id, staged)
            return result

        with patch.object(self.store, "review_memory", side_effect=review_then_change):
            code, payload = execute_review(
                self.config, self.store, task_id, "final", "approve", ""
            )

        self.assertEqual(1, code, payload)
        self.assertEqual("reconciliation_required", payload["status"])
        self.assertIn(BINDING_ERROR, json.dumps(payload, ensure_ascii=False))
        self.assertEqual(vault_before, self._vault_bytes(), "被拒的发布不得在 Vault 留下正式笔记")
        self.assertEqual(graph_before, self._graph_bytes(), "被拒的发布不得改动目录/图索引")
        self.assertNotEqual("published", self.store.get_memory_candidate(task_id)["status"])
        self.assertEqual(0, self._events(task_id, "memory.published"))
        self.assertEqual([], self._notes(self._vault_bytes(), BODY_CHANGED))

    # --------------------------------- 检查到落盘之间没有无锁间隙（真实两连接）
    def test_no_unlocked_gap_between_the_check_and_the_vault_write(self):
        for entry in ("cli", "shared-review"):
            with self.subTest(entry=entry):
                task_id, _, approved = self._candidate()
                self._approve(task_id, approved, ("delivery", "final"))
                observed = {}
                original_rebuild = MemoryBridge._rebuild_and_verify_index

                def probing_rebuild(bridge, vault):
                    # 笔记与目录此时已经写入，索引正在重建：写锁必须仍被持有。
                    observed["blocked"] = self._second_writer_blocked()
                    return original_rebuild(bridge, vault)

                with patch.object(MemoryBridge, "_rebuild_and_verify_index", probing_rebuild):
                    if entry == "cli":
                        code, payload = self._cli(["publish-memory", "--task-id", task_id])
                    else:
                        code, payload = execute_review(
                            self.config, self.store, task_id, "final", "approve", ""
                        )

                self.assertEqual(0, code, payload)
                self.assertTrue(
                    observed.get("blocked"),
                    "落盘与重建索引期间必须仍持有写锁，否则另一连接可以穿插改证据",
                )

    # ------------------------------- 数据库结算失败：不留半更新，重试只补结算
    def test_settlement_failure_keeps_the_approved_note_and_a_retry_settles_it(self):
        task_id, _, approved = self._candidate()
        self._approve(task_id, approved, ("delivery", "final"))

        def failing_settlement(store, conn, tid, path):
            raise sqlite3.OperationalError("disk I/O error")

        with patch.object(EventStore, "_mark_memory_published_tx", failing_settlement):
            code, payload = self._cli(["publish-memory", "--task-id", task_id])

        self.assertEqual(1, code)
        self.assertEqual("reconciliation_required", payload["status"])
        row = self.store.get_memory_candidate(task_id)
        self.assertNotEqual("published", row["status"])
        self.assertIsNone(row["published_path"])
        self.assertEqual(0, self._events(task_id, "memory.published"))
        vault_after_failure = self._vault_bytes()
        self.assertEqual(
            1, len(self._notes(vault_after_failure, BODY_APPROVED)),
            "已批准的正文已经落盘，等结算；不得是未经有效批准的内容",
        )

        code, payload = self._cli(["publish-memory", "--task-id", task_id])

        self.assertEqual(0, code, payload)
        self.assertEqual("published", self.store.get_memory_candidate(task_id)["status"])
        self.assertEqual(1, self._events(task_id, "memory.published"))
        self.assertEqual(vault_after_failure, self._vault_bytes(), "重试只补未成功的结算，不重写文件")

    # ---------------------------------------- 提交成功后报错：读回真实已提交状态
    def test_settlement_committed_then_reported_as_error_is_read_back(self):
        task_id, _, approved = self._candidate()
        self._approve(task_id, approved, ("delivery", "final"))
        original = self.store._mark_memory_published_tx

        def committed_then_raise(store, conn, tid, path):
            result = original(conn, tid, path)
            conn.commit()  # 真正提交，然后才把错误报上去
            raise sqlite3.OperationalError("connection reset after commit")

        with patch.object(EventStore, "_mark_memory_published_tx", committed_then_raise):
            code, payload = self._cli(["publish-memory", "--task-id", task_id])

        self.assertEqual(0, code, payload)
        self.assertEqual("published", payload["status"])
        self.assertEqual("published", self.store.get_memory_candidate(task_id)["status"])
        self.assertEqual(1, self._events(task_id, "memory.published"))
        self.assertEqual(
            1, len(self._notes(self._vault_bytes(), BODY_APPROVED)),
            "已合法发布的笔记不得被当成未提交而删除",
        )

    # ------------------------------------------- 文件写入失败：目录恢复、无残留
    def test_vault_write_failure_restores_the_catalog_and_leaves_no_note(self):
        task_id, _, approved = self._candidate()
        self._approve(task_id, approved, ("delivery", "final"))
        vault_before = self._vault_bytes()
        # 发布端会对 Vault 路径 resolve()，macOS 上 /var 会变成 /private/var。
        experience = (self.vault / EXPERIENCE_DIR).resolve()
        real_replace = os.replace

        def fail_note_write(src, dst):
            if str(dst).startswith(str(experience) + os.sep):
                raise OSError("disk full while writing the note")
            return real_replace(src, dst)

        with patch("xiaoba_agentos.memory.os.replace", side_effect=fail_note_write):
            code, payload = self._cli(["publish-memory", "--task-id", task_id])

        self.assertEqual(1, code)
        self.assertEqual("reconciliation_required", payload["status"])
        self.assertEqual(vault_before, self._vault_bytes(), "写笔记失败不得改动 Vault 或目录索引")
        self.assertNotEqual("published", self.store.get_memory_candidate(task_id)["status"])
        self.assertEqual(0, self._events(task_id, "memory.published"))
        self.assertEqual([], self._notes(self._vault_bytes(), BODY_APPROVED))

    # ------------------------------------------ 合法双阶段同哈希发布与幂等复验
    def test_legitimate_two_stage_publication_then_idempotent_republication(self):
        task_id, _, approved = self._candidate()
        self._approve(task_id, approved, ("delivery", "final"))

        code, payload = self._cli(["publish-memory", "--task-id", task_id])

        self.assertEqual(0, code, payload)
        self.assertEqual("published", payload["status"])
        self.assertEqual("published", self.store.get_memory_candidate(task_id)["status"])
        self.assertEqual(1, self._events(task_id, "memory.published"))
        vault_after_first = self._vault_bytes()
        self.assertEqual(1, len(self._notes(vault_after_first, BODY_APPROVED)))

        code, payload = self._cli(["publish-memory", "--task-id", task_id])

        self.assertEqual(0, code, payload)
        self.assertEqual("published", payload["status"])
        self.assertEqual(1, self._events(task_id, "memory.published"), "幂等复验不得再结算一次")
        self.assertEqual(vault_after_first, self._vault_bytes())


if __name__ == "__main__":
    unittest.main()
