"""底线 B19 写入边界、幂等键内容比对、未知 driver（卡 3）。

三处缺口共用一条要求：带思考过程或未执行工具调用标记的内容既进不了候选，
也进不了正式库；同一个幂等键不能静默吞掉不同内容；未知或空 driver 不能默默
回落到 AGY。

全部用例只在临时配置、临时 SQLite、临时 outbox 和临时 Vault 上运行，不调用
任何真实模型。

正文检查只用 output_hygiene 里已有的两个检测函数，所以「不误伤正常正文」
（MB-019）也在这里锁住：中文里讨论“思考标记”“工具调用”的经验必须能通过。
"""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fixtures_review import _config, _run_cli
from xiaoba_agentos.adapters import (
    AgyAdapter,
    DshAdapter,
    PermanentAdapterError,
    QoderAdapter,
    build_adapter,
)
from xiaoba_agentos.config import AgentConfig, AppConfig
from xiaoba_agentos.memory import MemoryBridge
from xiaoba_agentos.review_flow import execute_review
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.task_context import freeze_context

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIENCE_DIR = "70_复利规则/经验"

# 不合格正文：泄露的思考过程块，和未执行的工具调用标记。
LEAKY_BODY = (
    "先记录结论。\n"
    "<think>我先想想再回答，这一步不该出现在答案里。</think>\n"
    "结论如上。"
)
TOOL_MARKUP_BODY = (
    "结论如下。\n"
    '<invoke name="bash">\n'
    '<parameter name="command">rm -rf /tmp/x</parameter>\n'
    "</invoke>\n"
)
CLEAN_BODY = "两条经验：先复现，再修复，最后在隔离目录里验证。"

# 正常中文正文：讨论“思考标记”“工具调用”这两个概念，但不含任何字面标记。
PROSE_BODY = (
    "经验：审查后台输出时，先分两类看有没有非答案残留：一类是思考过程泄露，"
    "一类是工具调用标记和终端残留。\n\n"
    "做法：把“思考标记”“工具调用标记”“终端残留”写成清单逐项检查；"
    "口语化的自我独白要出现两处以上才算不合格，单独一句不判失败。\n"
    "适用条件：后台产出的经验候选与正式经验。\n"
    "排除条件：宿主与用户的正常对话不做这项检查，避免误伤正文。"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _outbox_files(outbox: Path) -> list:
    """只数候选正文文件；同目录的 .status.json 状态边车由发布路径自己维护。"""
    if not outbox.is_dir():
        return []
    return sorted(path.name for path in outbox.iterdir() if path.suffix == ".md")


def _agent(driver, name="edge"):
    """直接构造 AgentConfig，绕开 AppConfig.load 的配置校验来单测 build_adapter。"""
    app = AppConfig(Path("/tmp/write-boundary-config.json"), {
        "runtime_dir": "/tmp/write-boundary-runtime",
        "project_dir": "/tmp",
        "worker": {"poll_seconds": 1, "lease_seconds": 60, "max_attempts": 1,
                   "retry_base_seconds": 0, "retry_max_seconds": 0, "parallelism": 1,
                   "minimum_successful_agents": 1},
        "memory": {"knowlp_enabled": False, "obsidian_vault": "/tmp/write-boundary-vault",
                   "obsidian_subdir": "AgentOS", "publish_enabled": False},
        "integrations": {}, "cli_routes": {}, "agents": [],
        "evolution": {"minimum_samples": 1, "promotion_margin": 0.05, "latency_penalty": 0.01},
    })
    raw = {"name": name, "type": "local_cli", "enabled": True, "role": "",
           "timeout_seconds": 60}
    if driver is not None:
        raw["driver"] = driver
    return AgentConfig(name, "local_cli", True, "", timeout_seconds=60, app_config=app, raw=raw)


class CandidateCreationBoundaryTest(unittest.TestCase):
    """创建时就拒绝，且任务、文件、事件都不留。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = EventStore(self.root / "runtime" / "agentos.sqlite3")
        self.store.initialize()
        self.outbox = self.root / "runtime" / "memory-outbox"

    def _state(self):
        with self.store.read_only() as conn:
            events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return self.store.summary()["tasks"], _outbox_files(self.outbox), events

    def _assert_nothing_written(self, before):
        self.assertEqual(before, self._state())
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_reasoning_block_in_body_is_refused_without_any_write(self):
        before = self._state()
        with self.assertRaises(ValueError) as caught:
            self.store.create_authored_candidate("标题", LEAKY_BODY, self.outbox, author="test")
        self.assertIn("B19", str(caught.exception))
        self.assertIn("body", str(caught.exception))
        self._assert_nothing_written(before)

    def test_tool_call_markup_in_body_is_refused_without_any_write(self):
        before = self._state()
        with self.assertRaises(ValueError) as caught:
            self.store.create_authored_candidate("标题", TOOL_MARKUP_BODY, self.outbox, author="test")
        self.assertIn("B19", str(caught.exception))
        self.assertIn("工具调用", str(caught.exception))
        self._assert_nothing_written(before)

    def test_markup_in_title_is_refused_without_any_write(self):
        before = self._state()
        with self.assertRaises(ValueError) as caught:
            self.store.create_authored_candidate("标题 <think>内部推理</think>", CLEAN_BODY,
                                                 self.outbox, author="test")
        self.assertIn("title", str(caught.exception))
        self._assert_nothing_written(before)

    def test_normal_chinese_discussion_of_these_words_is_accepted(self):
        created = self.store.create_authored_candidate("思考标记与工具调用的检查清单", PROSE_BODY,
                                                       self.outbox, author="test")
        candidate = self.store.get_memory_candidate(created["task_id"])
        self.assertEqual("pending_delivery_review", candidate["status"])
        self.assertEqual(1, len(_outbox_files(self.outbox)))
        self.assertIn("思考标记", Path(candidate["staged_path"]).read_text(encoding="utf-8"))

    def test_cli_entry_reports_blocked_and_writes_nothing(self):
        """memory-candidate-create 走同一条拒绝路径：blocked、退出码 1、库里什么都没有。"""
        config = _config(self.root)
        body_file = self.root / "body.md"
        body_file.write_text(LEAKY_BODY, encoding="utf-8")
        before = self._state()

        code, payload = _run_cli([
            "--config", str(config.path), "memory-candidate-create",
            "--title", "越界候选", "--body-file", str(body_file), "--author", "test",
        ])

        self.assertEqual(1, code)
        self.assertEqual("blocked", payload["status"])
        self.assertIn("B19", payload["error"])
        self._assert_nothing_written(before)


class IdempotencyKeyTest(unittest.TestCase):
    """MD2-09：同键同内容返回原记录，同键不同内容抛错，原记录不变。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = EventStore(self.root / "runtime" / "agentos.sqlite3")
        self.store.initialize()
        self.outbox = self.root / "runtime" / "memory-outbox"

    def _task_rows(self):
        with self.store.read_only() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT id,prompt,result,execution_snapshot_json FROM tasks ORDER BY created_at"
            )]

    def test_same_key_and_same_content_returns_the_first_record(self):
        created = self.store.create_authored_candidate(
            "同键经验", CLEAN_BODY, self.outbox, author="test", idempotency_key="key-same"
        )
        rows = self._task_rows()
        replay = self.store.create_authored_candidate(
            "同键经验", CLEAN_BODY, self.outbox, author="test", idempotency_key="key-same"
        )
        self.assertTrue(replay["idempotent"])
        self.assertEqual(created["task_id"], replay["task_id"])
        self.assertEqual(created["evidence_sha256"], replay["evidence_sha256"])
        self.assertEqual(rows, self._task_rows())
        self.assertEqual(1, len(_outbox_files(self.outbox)))

    def test_same_key_with_different_body_is_refused_and_first_record_is_unchanged(self):
        created = self.store.create_authored_candidate(
            "同键经验", CLEAN_BODY, self.outbox, author="test", idempotency_key="key-diff"
        )
        before_tasks = self._task_rows()
        before_candidate = self.store.get_memory_candidate(created["task_id"])
        before_files = _outbox_files(self.outbox)

        with self.assertRaises(ValueError) as caught:
            self.store.create_authored_candidate(
                "同键经验", "换了一份完全不同的正文。", self.outbox,
                author="test", idempotency_key="key-diff",
            )
        self.assertIn("idempotency key", str(caught.exception))
        self.assertIn("body", str(caught.exception))
        self.assertEqual(before_tasks, self._task_rows())
        self.assertEqual(before_candidate, self.store.get_memory_candidate(created["task_id"]))
        self.assertEqual(before_files, _outbox_files(self.outbox))
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_same_key_with_different_metadata_is_refused(self):
        self.store.create_authored_candidate(
            "同键经验", CLEAN_BODY, self.outbox, author="test", idempotency_key="key-meta"
        )
        with self.assertRaises(ValueError) as caught:
            self.store.create_authored_candidate(
                "同键经验", CLEAN_BODY, self.outbox, author="someone-else",
                idempotency_key="key-meta",
            )
        self.assertIn("metadata", str(caught.exception))

    def test_submit_retry_survives_changed_memory_and_config(self):
        # 回复丢失后的重试：记忆命中、技能、配置和版本指纹在两次之间变了，
        # 调用方提交的内容没变，仍是同一次提交，要拿回原任务而不是报错。
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStore(Path(tmp) / "agentos.sqlite3")
            store.initialize()
            first = {"prompt": "题目", "task_context": freeze_context("上下文"), "memory_hits": [],
                     "agents": [{"name": "a"}], "source_manifest_sha256": "a" * 64}
            task_id = store.submit("题目", 1, snapshot=first, idempotency_key="retry-1")
            later = {**first, "task_context": {**first["task_context"], "captured_at": "2099-01-01T00:00:00+00:00"},
                     "memory_hits": [{"path": "新经验.md"}], "agents": [{"name": "b"}],
                     "source_manifest_sha256": "b" * 64}
            self.assertEqual(task_id, store.submit("题目", 1, snapshot=later, idempotency_key="retry-1"))
            with self.assertRaisesRegex(ValueError, "different content"):
                store.submit("题目", 1, snapshot={**first, "task_context": freeze_context("别的上下文")}, idempotency_key="retry-1")

    def test_submit_same_key_same_content_returns_the_first_task(self):
        first = self.store.submit("普通任务", 1, idempotency_key="submit-same")
        again = self.store.submit("普通任务", 1, idempotency_key="submit-same")
        self.assertEqual(first, again)
        self.assertEqual(1, len(self._task_rows()))

    def test_submit_same_key_different_content_is_refused(self):
        first = self.store.submit("普通任务", 1, idempotency_key="submit-diff")
        before = self._task_rows()
        with self.assertRaises(ValueError) as caught:
            self.store.submit("换掉的任务内容", 1, idempotency_key="submit-diff")
        self.assertIn("idempotency key", str(caught.exception))
        self.assertEqual(before, self._task_rows())
        self.assertEqual(first, before[0]["id"])
        with self.assertRaises(ValueError):
            # 正文相同但调用方给的其它输入不同，同样不能被当成重试吞掉。
            self.store.submit("普通任务", 1, snapshot={"prompt": "普通任务", "needs_web": True},
                              idempotency_key="submit-diff")


class PublicationBoundaryTest(unittest.TestCase):
    """绕过创建直接把不合格内容放进暂存区，发布仍要拒绝，且不落笔记。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.config = _config(self.root)
        self.config.raw["project_dir"] = str(PROJECT_ROOT)
        self.config.raw["memory"].update({
            "publish_enabled": True,
            "obsidian_subdir": EXPERIENCE_DIR,
            "catalog_note": EXPERIENCE_DIR + "索引.md",
            "knowlp_graph_dir": str(self.root / "runtime/graph"),
        })
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

    # ------------------------------------------------------------------ 夹具
    def _vault_files(self):
        return sorted(
            str(path.relative_to(self.vault)) for path in self.vault.rglob("*") if path.is_file()
        )

    def _events(self, task_id, event_type):
        with self.store.read_only() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM events WHERE task_id=? AND event_type=? ORDER BY sequence",
                (task_id, event_type),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def _stage_bypassing_creation(self, title, body):
        """先正常登记，再把暂存文件换成不合格正文并重算哈希——等于绕过创建检查。"""
        created = self.store.create_authored_candidate(title, CLEAN_BODY, self.bridge.outbox, author="test")
        staged = Path(created["staged_path"])
        staged.write_text(
            "---\n"
            f"task_id: {created['task_id']}\n"
            "status: pending_review\n"
            'type: "experience-candidate"\n'
            "authority: false\n"
            'author: "test"\n'
            f"title: {json.dumps(title, ensure_ascii=False)}\n"
            "---\n\n" + body + "\n",
            encoding="utf-8",
        )
        changed = _sha(staged)
        self.store.stage_memory_candidate(created["task_id"], str(staged), changed)
        return created["task_id"], staged, changed

    # ------------------------------------------------------- 直接调发布路径
    def test_direct_publication_refuses_staged_leak_and_writes_no_note(self):
        task_id, staged, changed = self._stage_bypassing_creation("越界候选", LEAKY_BODY)
        for stage in ("delivery", "final"):
            self.store.review_memory(task_id, stage, "approve", "用户在裁决中心批准", changed)
        vault_before = self._vault_files()

        with self.assertRaises(RuntimeError) as caught:
            self.bridge.publish_candidate(task_id)

        self.assertIn("B19", str(caught.exception))
        self.assertEqual(vault_before, self._vault_files(), "被拒的发布不得在第二大脑留下任何文件")
        self.assertEqual("pending_final_review", self.store.get_memory_candidate(task_id)["status"])
        self.assertEqual([], self._events(task_id, "memory.published"))
        self.assertTrue(staged.is_file())

    # ------------------------------------------------- 共用审核入口如实记录
    def test_review_entry_records_publication_failure_and_keeps_vault_clean(self):
        task_id, staged, _ = self._stage_bypassing_creation("越界候选", TOOL_MARKUP_BODY)
        vault_before = self._vault_files()

        code, delivery = execute_review(self.config, self.store, task_id, "delivery", "approve", "")
        self.assertEqual((0, "delivery_approved"), (code, delivery["memory_status"]))
        code, payload = execute_review(self.config, self.store, task_id, "final", "approve", "")

        self.assertEqual(1, code, payload)
        self.assertEqual(("reconciliation_required", "memory_publication"),
                         (payload["status"], payload["operation"]))
        self.assertIn("B19", json.dumps(payload["errors"], ensure_ascii=False))
        candidate = self.store.get_memory_candidate(task_id)
        self.assertEqual("publication_failed", candidate["status"])
        self.assertIsNone(candidate["published_path"])
        self.assertEqual(vault_before, self._vault_files(), "不合格正文不得写进第二大脑")
        self.assertTrue(staged.is_file(), "暂存文件保留，等用户补材料而不是被删掉")
        failures = self._events(task_id, "memory.publication_failed")
        self.assertEqual(1, len(failures))
        self.assertIn("B19", failures[0]["error"])
        self.assertIn("工具调用", failures[0]["error"])

    # ------------------------------------------------------- 正常正文不误伤
    def test_normal_chinese_discussion_publishes(self):
        created = self.store.create_authored_candidate(
            "思考标记与工具调用的检查清单", PROSE_BODY, self.bridge.outbox, author="test"
        )
        for stage in ("delivery", "final"):
            code, payload = execute_review(self.config, self.store, created["task_id"], stage, "approve", "")
            self.assertEqual(0, code, payload)
        candidate = self.store.get_memory_candidate(created["task_id"])
        self.assertEqual("published", candidate["status"])
        notes = [path for path in (self.vault / EXPERIENCE_DIR).glob("*.md") if path.name != "索引.md"]
        self.assertEqual(1, len(notes))
        self.assertIn("思考标记", notes[0].read_text(encoding="utf-8"))


class UnknownDriverTest(unittest.TestCase):
    """未知或空 driver 不得回落到 AGY。"""

    def test_unknown_driver_raises_permanent_error(self):
        with self.assertRaises(PermanentAdapterError) as caught:
            build_adapter(_agent("mystery-cli", "mystery"))
        self.assertIn("mystery-cli", str(caught.exception))

    def test_empty_driver_raises_permanent_error(self):
        with self.assertRaises(PermanentAdapterError):
            build_adapter(_agent(""))

    def test_missing_driver_key_raises_permanent_error(self):
        with self.assertRaises(PermanentAdapterError):
            build_adapter(_agent(None))

    def test_known_drivers_still_build_their_own_adapter(self):
        self.assertIsInstance(build_adapter(_agent("agy", "agy-one")), AgyAdapter)
        self.assertIsInstance(build_adapter(_agent("dsh", "dsh-one")), DshAdapter)
        self.assertIsInstance(build_adapter(_agent("qoder", "qoder-one")), QoderAdapter)


if __name__ == "__main__":
    unittest.main()
