"""卡46：待复验经验的恢复路径、驳回后归档、一致性检查与复用记账。

只用隔离临时目录里的第二大脑副本和数据库；不调真实模型、不碰线上。
每个用例先按现场复现，再断言修复后应有的行为。
"""
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures_review import _config, _run_cli
from xiaoba_agentos.memory import MemoryBridge
from xiaoba_agentos.review_flow import execute_review, publish_approved_candidate
from xiaoba_agentos.store import EventStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIENCE_DIR = "70_复利规则/经验"
CATALOG_REL = "70_复利规则/经验索引.md"


def _note_status(note: Path) -> str:
    text = note.read_text(encoding="utf-8")
    return re.search(r'^status:\s*"([^"]+)"', text, re.MULTILINE).group(1)


class Card46Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = _config(self.root)
        # 索引脚本随发布包在本树里，临时发布才走得完整个流程。
        self.config.raw["project_dir"] = str(PROJECT_ROOT)
        self.config.raw["memory"].update({
            "publish_enabled": True,
            "obsidian_subdir": EXPERIENCE_DIR,
            "catalog_note": CATALOG_REL,
            "knowlp_graph_dir": str(self.root / "runtime/graph"),
            "retire_after_failed_reuses": 2,
        })
        self.vault = self.root / "vault"
        self.catalog = self.vault / CATALOG_REL
        self.catalog.parent.mkdir(parents=True)
        (self.vault / "root.md").write_text(
            '---\nid: "root"\nparent: ""\nstatus: "active"\n---\n\n# Root\n\n- [[' + str(Path(CATALOG_REL).with_suffix('')) + ']]\n',
            encoding="utf-8",
        )
        self.catalog.write_text(
            '---\nid: "experiences"\nparent: "root"\nstatus: "active"\n---\n\n# 经验索引\n',
            encoding="utf-8",
        )
        self.store = EventStore(self.config.database_path)
        self.store.initialize()
        self.bridge = MemoryBridge(self.config, self.store)

    def tearDown(self):
        self.tmp.cleanup()

    # --- 夹具动作 -------------------------------------------------------
    def _candidate(self, title="恢复测试经验", body="正文：先核对再发布。"):
        created = self.store.create_authored_candidate(title, body, self.bridge.outbox, author="test")
        return created["task_id"], Path(created["staged_path"]), created["evidence_sha256"]

    def _publish(self, task_id, sha):
        for stage in ("delivery", "final"):
            code, payload = execute_review(self.config, self.store, task_id, stage, "approve", "")
            self.assertEqual(0, code, payload)
        row = self.store.get_memory_candidate(task_id)
        self.assertEqual("published", row["status"])
        return row

    def _quarantine(self, source_task_id):
        """2 号现场：复核失败把经验标成待复验，DB 与笔记都转 revalidation_required，链接被摘。"""
        from xiaoba_agentos.review_flow import reconcile_failed_reuses
        source_note = Path(self.store.get_memory_candidate(source_task_id)["published_path"])
        decider, _, _ = self._candidate(
            "触发复核失败的任务", "结论引用了 " + source_note.stem + " 的写法，但仍然失败。"
        )
        self.store.record_memory_reuse(decider, source_task_id, str(source_note))
        _, _, errors = reconcile_failed_reuses(self.config, self.store, decider)
        self.assertEqual([], errors)
        row = self.store.get_memory_candidate(source_task_id)
        self.assertEqual("revalidation_required", row["status"])
        note = Path(row["published_path"])
        self.assertEqual("revalidation_required", _note_status(note))
        self.assertNotIn(note.stem, self.catalog.read_text(encoding="utf-8"))
        return note

    def _events(self, task_id, event_type):
        with self.store.read_only() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM events WHERE task_id=? AND event_type=?",
                (task_id, event_type),
            ).fetchone()[0]

    def _vault_bytes(self):
        return {
            str(path.relative_to(self.vault)): path.read_bytes()
            for path in self.vault.rglob("*") if path.is_file()
        }


class RevalidationRecoveryTest(Card46Fixture):
    def test_approving_a_quarantined_memory_restores_it_instead_of_conflicting(self):
        task_id, _, sha = self._candidate()
        self._publish(task_id, sha)
        note = self._quarantine(task_id)

        code, payload = execute_review(self.config, self.store, task_id, "final", "approve", "")
        self.assertEqual(0, code, payload)
        row = self.store.get_memory_candidate(task_id)
        self.assertEqual("published", row["status"])
        self.assertEqual("active", _note_status(note), "恢复后笔记状态应回到 active")
        self.assertEqual(1, self.catalog.read_text(encoding="utf-8").count(note.stem),
                         "恢复后索引里应恰好一条链接（只发布一次）")
        index = json.loads((self.config.runtime_dir / "graph/meta_index.json").read_text(encoding="utf-8"))
        self.assertTrue(any(item["name"] == note.stem for item in index), "检索索引应能命中恢复的笔记")
        notes = list((self.vault / EXPERIENCE_DIR).glob(f"*-{task_id[:8]}.md"))
        self.assertEqual(1, len(notes), "恢复只发布一次，不产生第二篇笔记")

    def test_recovery_refuses_a_note_edited_after_quarantine(self):
        task_id, _, sha = self._candidate()
        self._publish(task_id, sha)
        note = self._quarantine(task_id)
        note.write_text(note.read_text(encoding="utf-8") + "\n被手改的一行。\n", encoding="utf-8")
        before = self._vault_bytes()

        code, payload = execute_review(self.config, self.store, task_id, "final", "approve", "")
        self.assertNotEqual(0, code, payload)
        self.assertIn("不一致", json.dumps(payload, ensure_ascii=False),
                      "拒绝时要说清是正文字对不上，而不是笼统的冲突")
        self.assertEqual(before, self._vault_bytes(), "被改过的笔记与索引都必须原样不动")
        self.assertNotEqual("published", self.store.get_memory_candidate(task_id)["status"])

    def test_publish_command_retries_a_stuck_publication_failed_candidate(self):
        """2 号现场：批准后撞冲突、停在“发布失败待处理”，正规重试命令要走恢复路径。"""
        task_id, _, sha = self._candidate()
        self._publish(task_id, sha)
        note = self._quarantine(task_id)
        # 批准（进入待发布），再按修复前的现场把状态落成“发布失败待处理”。
        self.store.review_memory(task_id, "final", "approve", "重新批准", sha)
        self.store.mark_memory_publication_failed(task_id, "conflicting published memory already exists")
        self.assertEqual("publication_failed", self.store.get_memory_candidate(task_id)["status"])
        self.assertEqual("revalidation_required", _note_status(note))

        publication, row = publish_approved_candidate(self.config, self.store, task_id, sha)
        self.assertEqual("published", row["status"])
        self.assertEqual(1, publication.get("recovered", 0))
        self.assertEqual("active", _note_status(note))
        self.assertEqual(1, self.catalog.read_text(encoding="utf-8").count(note.stem))

    def test_recovery_that_crashes_midway_resumes_on_the_next_attempt(self):
        task_id, _, sha = self._candidate()
        self._publish(task_id, sha)
        note = self._quarantine(task_id)
        catalog_before = self.catalog.read_bytes()

        from xiaoba_agentos.memory import MemoryBridge
        with patch.object(MemoryBridge, "_rebuild_and_verify_index", side_effect=RuntimeError("索引构建崩了")):
            code, payload = execute_review(self.config, self.store, task_id, "final", "approve", "")
        self.assertNotEqual(0, code, payload)
        self.assertEqual(catalog_before, self.catalog.read_bytes(), "中断后索引笔记要回到恢复前")
        self.assertEqual("revalidation_required", _note_status(note), "中断后不得留下半恢复状态")
        self.assertNotIn(note.stem, catalog_before.decode("utf-8"))

        publication, row = publish_approved_candidate(self.config, self.store, task_id, sha)
        self.assertEqual("published", row["status"], "同一条命令再跑一次能续上")
        self.assertEqual("active", _note_status(note))
        self.assertEqual(1, self.catalog.read_text(encoding="utf-8").count(note.stem))

    def test_cli_publish_memory_retries_a_stuck_publication_failed_candidate(self):
        """卡里的正规恢复命令 agentos publish-memory --task-id <id> 也走恢复路径。"""
        task_id, _, sha = self._candidate()
        self._publish(task_id, sha)
        note = self._quarantine(task_id)
        self.store.review_memory(task_id, "final", "approve", "重新批准", sha)
        self.store.mark_memory_publication_failed(
            task_id, "conflicting published memory already exists"
        )
        self.assertEqual("publication_failed", self.store.get_memory_candidate(task_id)["status"])

        with patch("xiaoba_agentos.cli._load", return_value=(self.config, self.store)):
            code, payload = _run_cli(["publish-memory", "--task-id", task_id])
        self.assertEqual(0, code, payload)
        self.assertEqual(1, payload.get("publication", {}).get("recovered", 0))
        self.assertEqual("published", self.store.get_memory_candidate(task_id)["status"])
        self.assertEqual("active", _note_status(note))
        self.assertEqual(1, self.catalog.read_text(encoding="utf-8").count(note.stem))


class RejectArchivesTest(Card46Fixture):
    def test_rejecting_a_quarantined_memory_archives_the_note(self):
        task_id, _, sha = self._candidate()
        self._publish(task_id, sha)
        note = self._quarantine(task_id)

        code, payload = execute_review(self.config, self.store, task_id, "final", "reject", "不再采用")
        self.assertEqual(0, code, payload)
        self.assertEqual("final_rejected", self.store.get_memory_candidate(task_id)["status"])
        self.assertFalse(note.exists(), "驳回后笔记必须离开活动目录")
        archive = self.config.runtime_dir / "memory-retired" / f"{task_id}.md"
        self.assertTrue(archive.is_file(), "驳回后要留一份归档")
        self.assertNotIn(note.stem, self.catalog.read_text(encoding="utf-8"), "索引里不得再留链接")

        # 归档后自检不得再把这条经验报成问题；重复驳回必须幂等，不多留归档、不重复建链接。
        self.assertTrue(self.bridge.audit_lifecycle([self.store.get_memory_candidate(task_id)])["ok"])
        again_code, again = execute_review(self.config, self.store, task_id, "final", "reject", "再点一次")
        self.assertEqual(0, again_code, again)
        self.assertEqual([], list((self.config.runtime_dir / "memory-retired").glob("*.md"))[1:])
        self.assertNotIn(note.stem, self.catalog.read_text(encoding="utf-8"))

    def test_consistency_check_reports_a_final_rejected_note_still_in_the_active_tree(self):
        task_id, _, sha = self._candidate()
        self._publish(task_id, sha)
        note = self._quarantine(task_id)
        # 旧数据现场：库是 final_rejected，笔记却还留在活动目录。
        with self.store.connect() as conn:
            conn.execute("UPDATE memory_candidates SET status='final_rejected' WHERE task_id=?", (task_id,))

        report = self.bridge.audit_lifecycle([self.store.get_memory_candidate(task_id)])
        self.assertFalse(report["ok"], report)
        self.assertTrue(any(task_id in item and "final_rejected" in item for item in report["problems"]),
                        report["problems"])
        self.assertTrue(note.exists(), "只读自检不得顺手搬走笔记")


class ReuseAccountingTest(Card46Fixture):
    def _source(self):
        task_id, _, sha = self._candidate("可被复用的经验", "复用边界要写清楚。")
        row = self._publish(task_id, sha)
        return task_id, Path(row["published_path"])

    def _reusing(self, body):
        reusing, _, _ = self._candidate("复用该经验的任务", body)
        self.store.review_memory(reusing, "delivery", "approve", "初审通过",
                                 self.store.get_memory_candidate(reusing)["evidence_sha256"])
        return reusing

    def test_a_retrieval_hit_not_cited_by_the_conclusion_does_not_downgrade_the_source(self):
        source, note = self._source()
        reusing = self._reusing("这次任务的结论没有提到任何历史经验。")
        self.store.record_memory_reuse(reusing, source, str(note))
        code, payload = execute_review(self.config, self.store, reusing, "final", "reject", "结论不成立")
        self.assertEqual(0, code, payload)
        after = self.store.get_memory_candidate(source)
        self.assertEqual("published", after["status"], "只是检索命中，不应当把来源经验降级")
        self.assertEqual(0, after["failed_reuse_count"])
        self.assertEqual("active", _note_status(note))

    def test_a_reuse_cited_by_the_conclusion_still_downgrades_the_source(self):
        source, note = self._source()
        reusing = self._reusing("本次结论引用了 " + note.stem + " 的做法，但结论仍不成立。")
        self.store.record_memory_reuse(reusing, source, str(note))
        code, payload = execute_review(self.config, self.store, reusing, "final", "reject", "结论不成立")
        self.assertEqual(0, code, payload)
        after = self.store.get_memory_candidate(source)
        self.assertEqual("revalidation_required", after["status"], "真被引用且任务失败时照旧降级")
        self.assertEqual(1, after["failed_reuse_count"])

    def test_an_uncited_hit_is_not_counted_as_a_successful_use_on_approval(self):
        """任务成功/批准时，只检索命中同样不算一次使用。"""
        source, note = self._source()
        reusing = self._reusing("这次任务的结论没有提到任何历史经验。")
        self.store.record_memory_reuse(reusing, source, str(note))
        code, payload = execute_review(
            self.config, self.store, reusing, "final", "approve", "结论成立"
        )
        self.assertEqual(0, code, payload)
        after = self.store.get_memory_candidate(source)
        self.assertEqual(0, after["successful_reuse_count"], "只命中不该加成功复用分")
        self.assertEqual(
            ["pending"],
            [item["outcome"] for item in self.store.memory_reuses(reusing)],
            "只命中应保持 pending，不被结算成 validated",
        )
        self.assertEqual("published", after["status"])

    def test_a_cited_reuse_still_counts_and_reactivates_on_approval(self):
        """真被结论引用、任务成功时，照旧记一次成功复用并复活待复验的来源。"""
        from xiaoba_agentos.review_flow import reconcile_failed_reuses

        source, note = self._source()
        failed, _, _ = self._candidate("先把来源降级", "结论引用了 " + note.stem + " 但失败。")
        self.store.record_memory_reuse(failed, source, str(note))
        _, _, errors = reconcile_failed_reuses(self.config, self.store, failed)
        self.assertEqual([], errors)
        self.assertEqual(
            "revalidation_required", self.store.get_memory_candidate(source)["status"]
        )

        reusing = self._reusing("本次结论引用了 " + note.stem + " 的做法，且结论成立。")
        self.store.record_memory_reuse(reusing, source, str(note))
        code, payload = execute_review(
            self.config, self.store, reusing, "final", "approve", "结论成立"
        )
        self.assertEqual(0, code, payload)
        after = self.store.get_memory_candidate(source)
        self.assertEqual(1, after["successful_reuse_count"])
        self.assertEqual("published", after["status"], "成功复用要把待复验经验恢复")
        self.assertEqual("active", _note_status(note))


if __name__ == "__main__":
    unittest.main()
