"""已发布 / 已退役候选拒绝重新登记：隔离回归测试。

覆盖四条候选登记路径：
  * stage_memory_candidate
  * complete_with_memory_candidate（已完成任务的重复 / 补登记分支）
  * release_constraint_hold（释放扣留时顺带登记候选）
  * create_authored_candidate 的幂等入口（只允许"返回既有同一请求的结果"）

两种终态 × 同哈希 / 异哈希都必须失败，且失败必须发生在任何数据库行、
侧文件或事件写入之前：状态、证据路径、来源、事件数、审核记录、任务行
和暂存文件字节都保持不变。失败信息要明确说出"终态不可重新登记"。

终态判定与写入共享同一个事务边界：用第二条真实 SQLite 连接证明
检查→写入的间隙不能被一次发布 / 退役穿插，命令行入口也必须在写任何
文件之前就拒绝。非终态重新登记与幂等重放不在本次改动范围，本文件只
用合法流程和幂等不退化来守住它们的边界。
"""

import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures_review import _config, _run_cli
from xiaoba_agentos.memory import MemoryBridge
from xiaoba_agentos.store import MEMORY_TERMINAL_STATES, EventStore


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_terminal_update(status: str) -> tuple:
    """另一条连接发布 / 退役时要写的列：与 mark_memory_published / mark_memory_retired 相同。"""
    now = time.time()
    if status == "published":
        return ("published", now, now, now)
    return ("retired", now, now, now)


class _SnapshotMixin:
    """两个用例类共用的取证：候选整行、审核链、事件链、任务行、扣留行、文件字节。"""

    def _snapshot(self, task_id: str, staged: Path) -> dict:
        sidecar = Path(str(staged) + ".status.json")
        with self.store.read_only() as conn:
            candidate = conn.execute(
                "SELECT * FROM memory_candidates WHERE task_id=?", (task_id,)
            ).fetchone()
            reviews = conn.execute(
                "SELECT COUNT(*) FROM memory_reviews WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            task_events = conn.execute(
                "SELECT COUNT(*) FROM events WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            chain = conn.execute("SELECT COUNT(*),MAX(sequence) FROM events").fetchone()
            task = conn.execute(
                "SELECT status,result,updated_at FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            hold = conn.execute(
                "SELECT * FROM constraint_holds WHERE task_id=?", (task_id,)
            ).fetchone()
        return {
            "candidate": dict(candidate) if candidate is not None else None,
            "reviews": reviews,
            "task_events": task_events,
            "all_events": chain[0],
            "last_sequence": chain[1],
            "task": dict(task) if task is not None else None,
            "hold": dict(hold) if hold is not None else None,
            "sidecar": sidecar.read_bytes() if sidecar.exists() else None,
            "staged_bytes": staged.read_bytes(),
            "staged_mtime": staged.stat().st_mtime_ns,
        }

    def _event_count(self, task_id: str, event_type: str) -> int:
        with self.store.read_only() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM events WHERE task_id=? AND event_type=?", (task_id, event_type)
            ).fetchone()[0]


class TerminalCandidateRegistrationTest(_SnapshotMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = EventStore(self.root / "agentos.sqlite3")
        self.store.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------------ 夹具
    def _terminal_candidate(self, status: str) -> dict:
        """把一条候选推到 published 或 retired，返回登记所需的全部坐标。"""
        assert status in MEMORY_TERMINAL_STATES
        task_id = self.store.submit("source", 1)
        self.store.complete(task_id, "result")
        staged = self.root / f"{status}.md"
        staged.write_text("# terminal evidence\n", encoding="utf-8")
        evidence = _sha(staged)
        self.store.stage_memory_candidate(task_id, str(staged), evidence, ["source-a"])
        self.store.review_memory(task_id, "delivery", "approve", "ok", evidence)
        self.store.review_memory(task_id, "final", "approve", "ok", evidence)
        self.store.mark_memory_published(task_id, str(self.root / f"vault/{status}.md"))
        if status == "retired":
            self.store.mark_memory_retired(task_id, str(self.root / f"archive/{status}.md"), "test")
        return {"task_id": task_id, "staged": staged, "evidence": evidence}

    def _held_terminal_candidate(self, status: str) -> dict:
        """终态候选外加一条仍处于 pending 的扣留（需要防御的旧 / 异常状态）。"""
        fixture = self._terminal_candidate(status)
        self.store.hold_constraint_task(fixture["task_id"], "1517", {"status": "unresolved_partial"})
        return fixture

    def _rewritten_evidence(self) -> dict:
        staged = self.root / "rewritten.md"
        staged.write_text("# rewritten evidence\n", encoding="utf-8")
        return {"staged": staged, "evidence": _sha(staged)}

    def _attempt(self, status: str, same_hash: bool, call) -> None:
        """跑一次重新登记尝试，断言失败且什么都没动。"""
        fixture = self._terminal_candidate(status)
        task_id, staged = fixture["task_id"], fixture["staged"]
        if same_hash:
            path, digest = str(staged), fixture["evidence"]
        else:
            rewritten = self._rewritten_evidence()
            path, digest = str(rewritten["staged"]), rewritten["evidence"]
        before = self._snapshot(task_id, staged)
        with self.assertRaises(ValueError) as caught:
            call(task_id, path, digest)
        message = str(caught.exception)
        self.assertIn("cannot be re-registered", message)
        self.assertIn(f"'{status}'", message)
        self.assertEqual(before, self._snapshot(task_id, staged))
        self.assertTrue(self.store.verify_event_chain()["ok"])

    # ------------------------------------------------- 四条登记路径的拒绝
    def test_stage_memory_candidate_refuses_terminal_reregistration(self):
        for status in sorted(MEMORY_TERMINAL_STATES):
            for same_hash in (True, False):
                with self.subTest(status=status, same_hash=same_hash):
                    self._attempt(
                        status, same_hash,
                        lambda task_id, path, digest: self.store.stage_memory_candidate(
                            task_id, path, digest, ["new-source"]
                        ),
                    )

    def test_complete_with_memory_candidate_refuses_terminal_reregistration(self):
        for status in sorted(MEMORY_TERMINAL_STATES):
            for same_hash in (True, False):
                with self.subTest(status=status, same_hash=same_hash):
                    self._attempt(
                        status, same_hash,
                        lambda task_id, path, digest: self.store.complete_with_memory_candidate(
                            task_id, "另一个结果", path, digest, ["new-source"]
                        ),
                    )

    def test_release_constraint_hold_refuses_terminal_reregistration(self):
        for status in sorted(MEMORY_TERMINAL_STATES):
            for same_hash in (True, False):
                with self.subTest(status=status, same_hash=same_hash):
                    fixture = self._held_terminal_candidate(status)
                    task_id, staged, evidence = (
                        fixture["task_id"], fixture["staged"], fixture["evidence"]
                    )
                    if same_hash:
                        path, digest = str(staged), evidence
                    else:
                        rewritten = self._rewritten_evidence()
                        path, digest = str(rewritten["staged"]), rewritten["evidence"]
                    before = self._snapshot(task_id, staged)
                    with self.assertRaises(ValueError) as caught:
                        self.store.release_constraint_hold(task_id, path, digest)
                    self.assertIn("cannot be re-registered", str(caught.exception))
                    self.assertIn(f"'{status}'", str(caught.exception))
                    self.assertEqual(before, self._snapshot(task_id, staged))
                    self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_idempotent_replay_returns_the_same_record_and_writes_nothing(self):
        """幂等重放不是重新登记：原样返回既有记录，不动任何一行。"""
        outbox = self.root / "outbox"
        created = self.store.create_authored_candidate(
            "标题", "正文", outbox, author="test", idempotency_key="k-1"
        )
        task_id = created["task_id"]
        staged = Path(created["staged_path"])
        evidence = created["evidence_sha256"]
        self.store.review_memory(task_id, "delivery", "approve", "ok", evidence)
        self.store.review_memory(task_id, "final", "approve", "ok", evidence)
        self.store.mark_memory_published(task_id, str(self.root / "vault/authored.md"))
        before = self._snapshot(task_id, staged)

        replay = self.store.create_authored_candidate(
            "标题", "正文", outbox, author="test", idempotency_key="k-1"
        )
        self.assertTrue(replay["idempotent"])
        self.assertEqual(task_id, replay["task_id"])
        self.assertEqual("published", replay["status"])
        self.assertEqual(before, self._snapshot(task_id, staged))

    # ------------------------------------------- 检查与写入共享一个事务边界
    def _refused_with_lock_held(self, call) -> dict:
        """在守卫执行的瞬间用第二条真实连接探测：事务开着吗？还能写吗？"""
        original = EventStore._reject_terminal_memory_reregistration
        seen = {}

        def guard(store_self, conn, task_id):
            seen["in_transaction"] = conn.in_transaction
            peer = sqlite3.connect(str(store_self.path), timeout=0.1)
            try:
                peer.execute("BEGIN IMMEDIATE")  # 另一条连接此刻想拿写锁
                peer.execute("ROLLBACK")
                seen["peer_blocked"] = False
            except sqlite3.OperationalError as exc:
                seen["peer_blocked"] = "locked" in str(exc)
            finally:
                peer.close()
            return original(store_self, conn, task_id)  # 真身负责拒绝

        with patch.object(EventStore, "_reject_terminal_memory_reregistration", guard):
            with self.assertRaises(ValueError) as caught:
                call()
        self.assertIn("cannot be re-registered", str(caught.exception))
        return seen

    def test_stage_check_and_write_share_one_transaction_boundary(self):
        for status in sorted(MEMORY_TERMINAL_STATES):
            for same_hash in (True, False):
                with self.subTest(status=status, same_hash=same_hash):
                    fixture = self._terminal_candidate(status)
                    task_id = fixture["task_id"]
                    if same_hash:
                        path, digest = str(fixture["staged"]), fixture["evidence"]
                    else:
                        rewritten = self._rewritten_evidence()
                        path, digest = str(rewritten["staged"]), rewritten["evidence"]
                    seen = self._refused_with_lock_held(
                        lambda: self.store.stage_memory_candidate(task_id, path, digest, ["x"])
                    )
                    self.assertTrue(seen["in_transaction"])
                    self.assertTrue(seen["peer_blocked"])

    def test_release_check_and_write_share_one_transaction_boundary(self):
        for status in sorted(MEMORY_TERMINAL_STATES):
            with self.subTest(status=status):
                fixture = self._held_terminal_candidate(status)
                task_id = fixture["task_id"]
                seen = self._refused_with_lock_held(
                    lambda: self.store.release_constraint_hold(
                        task_id, str(fixture["staged"]), fixture["evidence"]
                    )
                )
                self.assertTrue(seen["in_transaction"])
                self.assertTrue(seen["peer_blocked"])

    def test_a_peer_writer_cannot_slip_between_the_check_and_the_write(self):
        """真实的两连接竞争：B 先拿写锁并提交终态，A 的事务只能排在后面。

        修复前的路径在守卫时没有事务：守卫读到的是旧状态（待终审），
        随后的 UPSERT 会在 B 提交之后照写，把已发布候选的证据哈希换掉。
        现在同一件事必须以拒绝收场。
        """
        for status in sorted(MEMORY_TERMINAL_STATES):
            with self.subTest(status=status):
                fixture = self._terminal_candidate(status)
                task_id, staged, evidence = (
                    fixture["task_id"], fixture["staged"], fixture["evidence"]
                )
                rewritten = self._rewritten_evidence()
                before = self._snapshot(task_id, staged)

                ready = threading.Event()
                errors = []

                def peer():
                    conn = sqlite3.connect(str(self.store.path), timeout=10)
                    try:
                        conn.execute("BEGIN IMMEDIATE")  # 先占住写锁
                        ready.set()
                        time.sleep(0.2)  # 让主线程的 BEGIN IMMEDIATE 排到锁后面
                        status_now, published_at, last_validated_at, updated_at = (
                            _canonical_terminal_update(status)
                        )
                        published = (
                            str(self.root / f"vault/{status}.md") if status == "published"
                            else str(self.root / f"archive/{status}.md")
                        )
                        conn.execute(
                            """UPDATE memory_candidates SET status=?,published_path=?,
                               published_at=COALESCE(published_at,?),last_validated_at=?,
                               updated_at=? WHERE task_id=?""",
                            (status_now, published, published_at, last_validated_at, updated_at, task_id),
                        )
                        conn.commit()
                    except Exception as exc:  # pragma: no cover - 只为诊断
                        errors.append(exc)
                    finally:
                        conn.close()

                thread = threading.Thread(target=peer)
                thread.start()
                try:
                    self.assertTrue(ready.wait(10))
                    with self.assertRaises(ValueError) as caught:
                        self.store.stage_memory_candidate(
                            task_id, str(rewritten["staged"]), rewritten["evidence"], ["x"]
                        )
                    self.assertIn("cannot be re-registered", str(caught.exception))
                    self.assertIn(f"'{status}'", str(caught.exception))
                finally:
                    thread.join(10)
                self.assertEqual([], errors)
                after = self._snapshot(task_id, staged)
                row = after["candidate"]
                # B 的提交照做，A 的拒绝不动任何东西：状态是 B 写的终态，
                # 证据哈希仍是原来那一份，文件、侧文件、审核与事件一字未增。
                self.assertEqual(status, row["status"])
                self.assertEqual(evidence, row["evidence_sha256"])
                self.assertEqual(before["reviews"], after["reviews"])
                self.assertEqual(before["task_events"], after["task_events"])
                self.assertEqual(before["all_events"], after["all_events"])
                self.assertEqual(before["last_sequence"], after["last_sequence"])
                self.assertEqual(before["task"], after["task"])
                self.assertEqual(before["hold"], after["hold"])
                self.assertEqual(before["sidecar"], after["sidecar"])
                self.assertEqual(before["staged_bytes"], after["staged_bytes"])
                self.assertEqual(before["staged_mtime"], after["staged_mtime"])
                self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_the_lock_probe_itself_writes_outside_the_transaction(self):
        """探测用例的自检：离开事务后，同样的写锁可以正常拿到。"""
        conn = sqlite3.connect(str(self.store.path), timeout=10)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        finally:
            conn.close()

    # ------------------------------------------------------ 未改动行为的边界
    def test_missing_candidate_repair_on_a_completed_task_still_works(self):
        """补登记分支没有既有候选，终态检查不介入。"""
        task_id = self.store.submit("repair", 1)
        self.store.complete(task_id, "result")
        staged = self.root / "repair.md"
        staged.write_text("# repaired evidence\n", encoding="utf-8")
        evidence = _sha(staged)
        self.assertFalse(
            self.store.complete_with_memory_candidate(task_id, "result", str(staged), evidence, [])
        )
        self.assertEqual("pending_delivery_review", self.store.get_memory_candidate(task_id)["status"])
        self.assertEqual(1, self._event_count(task_id, "memory.candidate_staged"))

    def test_exactly_once_settlement_still_returns_false_for_a_repeat(self):
        """恰好一次结算：同一任务重复 complete 只结算一次，不改任何行。"""
        task_id = self.store.submit("settle", 1)
        staged = self.root / "settle.md"
        staged.write_text("# settled evidence\n", encoding="utf-8")
        evidence = _sha(staged)
        self.assertTrue(
            self.store.complete_with_memory_candidate(task_id, "结果", str(staged), evidence, [])
        )
        before = self._snapshot(task_id, staged)
        self.assertFalse(
            self.store.complete_with_memory_candidate(task_id, "另一个结果", str(staged), evidence, [])
        )
        self.assertEqual(before, self._snapshot(task_id, staged))
        self.assertEqual(1, self._event_count(task_id, "memory.candidate_staged"))

    def test_non_terminal_flow_still_works_and_stays_reviewable(self):
        """非终态维持现状：重新登记被接受，合法流程仍能走完到终态。

        数据库状态列不随重新登记变化（ON CONFLICT 不更新 status），侧文件
        记录的是工具使用的待初审状态；两者的错位是已知现状，属于下一版
        修订入口要处理的问题，这里不把它固化成产品必须满足的行为。
        """
        task_id = self.store.submit("source", 1)
        self.store.complete(task_id, "result")
        staged = self.root / "draft.md"
        staged.write_text("# first draft\n", encoding="utf-8")
        first = _sha(staged)
        self.store.stage_memory_candidate(task_id, str(staged), first, ["source-a"])
        self.store.review_memory(task_id, "delivery", "revise", "调整", first)
        self.assertEqual("revision_requested", self.store.get_memory_candidate(task_id)["status"])

        staged.write_text("# revised draft\n", encoding="utf-8")
        second = _sha(staged)
        self.store.stage_memory_candidate(task_id, str(staged), second, ["source-b"])
        row = self.store.get_memory_candidate(task_id)
        self.assertEqual(second, row["evidence_sha256"])
        self.assertEqual("revision_requested", row["status"])
        self.assertIn("source-b", row["source_task_ids_json"])
        sidecar = json.loads(Path(str(staged) + ".status.json").read_text(encoding="utf-8"))
        self.assertEqual(second, sidecar["evidence_sha256"])
        self.assertEqual("pending_delivery_review", sidecar["status"])

        # 合法流程继续：重新裁决 → 终审 → 发布，终态落地的是重新登记后的证据。
        self.store.review_memory(task_id, "delivery", "approve", "通过", second)
        self.store.review_memory(task_id, "final", "approve", "通过", second)
        published = self.store.mark_memory_published(task_id, str(self.root / "vault/draft.md"))
        self.assertEqual("published", published["status"])
        self.assertEqual(second, published["evidence_sha256"])
        self.assertEqual(second, _sha(staged))
        self.assertTrue(self.store.verify_event_chain()["ok"])


class ConstraintReleaseCliTest(_SnapshotMixin, unittest.TestCase):
    """真实命令行：终态候选必须在写任何文件之前就被拒绝。"""

    _FROZEN_STAMP = "2026-10-11T09:00:00+0800"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = _config(self.root)
        self.store = EventStore(self.config.database_path)
        self.store.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def _frozen_clock(self):
        """固定 front-matter 时间戳：这样"同哈希"场景在字节层面可证。"""
        return patch("xiaoba_agentos.memory.time.strftime", return_value=self._FROZEN_STAMP)

    def _published_hold(self, status: str, hold_result: str) -> dict:
        """终态候选 + 仍 pending 的扣留：命令行必须防住的异常状态。"""
        task_id = self.store.submit("任务提示", 1)
        self.store.hold_constraint_task(task_id, hold_result, {"status": "unresolved_partial"})
        bridge = MemoryBridge(self.config)
        staged = bridge.stage_experience(task_id, "任务提示", hold_result, [])
        evidence = _sha(staged)
        self.store.stage_memory_candidate(task_id, str(staged), evidence, ["source-a"])
        self.store.review_memory(task_id, "delivery", "approve", "ok", evidence)
        self.store.review_memory(task_id, "final", "approve", "ok", evidence)
        self.store.mark_memory_published(task_id, str(self.root / f"vault/{status}.md"))
        if status == "retired":
            self.store.mark_memory_retired(task_id, str(self.root / f"archive/{status}.md"), "test")
        return {"task_id": task_id, "staged": staged, "evidence": evidence, "hold_result": hold_result}

    def test_cli_refuses_a_terminal_candidate_before_writing_any_evidence(self):
        for status in sorted(MEMORY_TERMINAL_STATES):
            for same_hash in (True, False):
                with self.subTest(status=status, same_hash=same_hash):
                    hold_result = "被扣留的结果" if same_hash else "被扣留的另一个结果"
                    with self._frozen_clock():
                        fixture = self._published_hold(status, hold_result)
                        task_id, staged, evidence = (
                            fixture["task_id"], fixture["staged"], fixture["evidence"]
                        )
                        before = self._snapshot(task_id, staged)
                        code, payload = _run_cli(
                            ["--config", str(self.config.path), "constraint-hold-release", task_id]
                        )
                    self.assertEqual(1, code)
                    self.assertEqual("blocked", payload["status"])
                    self.assertIn("cannot be re-registered", payload["error"])
                    self.assertIn(f"'{status}'", payload["error"])
                    self.assertFalse(payload["retry_safe"])
                    # 暂存文件一个字节都没动：不是改完再改回来，而是从未被写过。
                    self.assertEqual(evidence, _sha(staged))
                    self.assertEqual(before, self._snapshot(task_id, staged))
                    self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_cli_release_of_a_non_terminal_candidate_is_unchanged(self):
        """非终态候选照常放行：扣留释放、证据更新、候选留在待初审。"""
        task_id = self.store.submit("任务提示", 1)
        staged = self.root / "draft.md"
        staged.write_text("# first draft\n", encoding="utf-8")
        first = _sha(staged)
        self.store.stage_memory_candidate(task_id, str(staged), first, ["source-a"])
        self.store.hold_constraint_task(task_id, "被扣留的结果", {"status": "unresolved_partial"})
        before = self._snapshot(task_id, staged)
        self.assertEqual("pending_delivery_review", before["candidate"]["status"])
        staged_events = self._event_count(task_id, "memory.candidate_staged")

        code, payload = _run_cli(
            ["--config", str(self.config.path), "constraint-hold-release", task_id]
        )
        self.assertEqual(0, code)
        self.assertEqual("released", payload["status"])
        candidate = self.store.get_memory_candidate(task_id)
        self.assertEqual("pending_delivery_review", candidate["status"])
        self.assertEqual(payload["candidate_sha256"], candidate["evidence_sha256"])
        self.assertEqual(
            candidate["evidence_sha256"],
            _sha(Path(candidate["staged_path"])),
        )
        self.assertEqual("released", self.store.get_constraint_hold(task_id)["status"])
        self.assertEqual(
            staged_events + 1, self._event_count(task_id, "memory.candidate_staged")
        )
        self.assertTrue(self.store.verify_event_chain()["ok"])


if __name__ == "__main__":
    unittest.main()
