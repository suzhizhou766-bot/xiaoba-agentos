"""卡 44：发布验收接受宿主补票、审查票按 run_id 复用、来源任务参与者校验。

只用隔离临时目录、SQLite 夹具和假适配器，不调用真实模型，不碰线上库。
"""
import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_card37 import _isolated_config
from test_pool_dispatch import _page_source, _patch_pool_cli, _seed
from xiaoba_agentos.cli import _submit_task
from xiaoba_agentos.host_completion import complete_host
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.pool_dispatch import declared_work_tasks
from xiaoba_agentos.store import EventStore

ROOT = Path(__file__).resolve().parents[1]


def _load_build_release():
    spec = importlib.util.spec_from_file_location("card44_build_release", ROOT / "scripts" / "build_release.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


POOL_BODY = "证据引用：task:task-1\n审查结论：PASS\nFINAL_ACCEPTANCE: PASS\nTEST 池节点审查正文"


class _AcceptanceDatabaseFixture(unittest.TestCase):
    """构包取证用的验收库夹具：和 test_release 同一形状，多一列 agent_runs.id。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "agentos.json").write_text(
            json.dumps({"worker": {"minimum_successful_agents": 2}, "agents": []}), encoding="utf-8")
        (self.root / "SOURCE_MANIFEST.json").write_text("{}\n", encoding="utf-8")
        self.manifest_sha = hashlib.sha256((self.root / "SOURCE_MANIFEST.json").read_bytes()).hexdigest()
        self.build_release = _load_build_release()

    def snapshot(self, *, declare=True):
        context = {"work_task_id": "work-1" if declare is True else None,
                   "host": {"id": "host-a", "entry": "cli:host", "model": "review-model"},
                   "needs_review": True}
        if declare == "none":
            context["no_work_task"] = True
        return {"prompt": "TEST 发布验收", "source_manifest_sha256": self.manifest_sha,
                "review_context": context, "needs_review": True}

    def host_receipt(self, snapshot_sha, verdict="PASS"):
        return {"receipt_id": "host-receipt-1", "task_id": "task-1", "snapshot_sha256": snapshot_sha,
                "reviewer_id": "host-a", "verdict": verdict, "findings": "TEST 宿主实际审查正文",
                "evidence_refs": ["task:task-1"], "review_type": "new_experience", "score": 8,
                "applicability": "TEST fixtures only", "duplicate_of": None,
                "started_at": 1793500000.0, "finished_at": 1793500010.0}

    def database(self, *, declare=True, host_settled=True, host_verdict="PASS",
                 host_role=None, receipt_snapshot=None, pool_run_ids=("run-pool-1",),
                 host_kind=None, pool_body=None):
        snapshot = self.snapshot(declare=declare)
        snapshot_sha = hashlib.sha256(_canonical(snapshot).encode()).hexdigest()
        pool_body = POOL_BODY if pool_body is None else pool_body
        result = (
            "宿主审查结论：" + ("PASS" if host_verdict == "PASS" else "FAIL（未通过）")
            + "\n\nTEST 宿主实际审查正文\n\n池节点审查结论：\nsouth-node: " + pool_body
        )
        receipt = self.host_receipt(receipt_snapshot or snapshot_sha, verdict=host_verdict)
        receipt_sha = hashlib.sha256(_canonical(receipt).encode()).hexdigest()
        pool_run_ids = list(pool_run_ids)
        provenance = {"kind": host_kind or ("host_review" if host_settled else "agentos_multi_adjudicated"),
                      "status": "completed", "verdict": host_verdict,
                      "result_sha256": hashlib.sha256(result.encode()).hexdigest(),
                      "receipt_sha256": receipt_sha, "host_id": "host-a",
                      "pool_run_ids": pool_run_ids, "label": None}
        if not host_settled:
            provenance = {"kind": "agentos_multi_adjudicated", "minimum_successful_agents": 2}
            result = "本回答：小八 AgentOS 双 Agent 裁判\n\n" + pool_body
            provenance["result_sha256"] = hashlib.sha256(result.encode()).hexdigest()
        path = self.root / "acceptance.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE tasks (
                id TEXT, status TEXT, execution_mode TEXT, result TEXT, provenance_json TEXT,
                execution_snapshot_json TEXT, execution_snapshot_hash TEXT, prompt TEXT
            );
            CREATE TABLE agent_runs (
                id TEXT, task_id TEXT, agent_name TEXT, status TEXT, output TEXT, created_at REAL,
                failure_class TEXT
            );
            CREATE TABLE memory_candidates (
                task_id TEXT, status TEXT, evidence_sha256 TEXT
            );
            CREATE TABLE events (
                task_id TEXT, event_type TEXT, payload_json TEXT, sequence INTEGER
            );
            CREATE TABLE ri_sources (
                id TEXT PRIMARY KEY, data TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
            ("task-1", "completed", "production", result, json.dumps(provenance),
             json.dumps(snapshot), snapshot_sha, snapshot["prompt"]),
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
            ("work-1", "completed", "production", "TEST 工作结果", "{}",
             json.dumps({"prompt": "TEST 工作", "needs_review": False,
                         "review_context": {"needs_review": False}}), "work-sha", "TEST 工作"),
        )
        connection.execute(
            "INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?)",
            ("run-pool-1", "task-1", "south-node", "completed", pool_body, 1.0, None),
        )
        if not host_settled:
            # 老路径要两个池节点的结论；宿主补票路径只要一份池节点票加宿主那一票。
            connection.execute(
                "INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?)",
                ("run-pool-2", "task-1", "east-node", "completed", pool_body, 2.0, None),
            )
        connection.execute(
            "INSERT INTO memory_candidates VALUES (?,?,?)",
            ("task-1", "pending_delivery_review", "a" * 64),
        )
        connection.execute(
            "INSERT INTO events VALUES (?,?,?,?)",
            ("task-1", "judge.decided",
             json.dumps({"winner": "south-node", "successful_agents": 2}), 1),
        )
        if host_settled:
            connection.execute(
                "INSERT INTO events VALUES (?,?,?,?)",
                ("task-1", "host.receipt_accepted",
                 json.dumps({"receipt_sha256": receipt_sha, "receipt": receipt,
                             "user_chose_host": False, "label": None,
                             "pool_run_ids": pool_run_ids}), 2),
            )
        if host_role == "author":
            connection.execute(
                "INSERT INTO events VALUES (?,?,?,?)",
                ("work-1", "task.participant_recorded",
                 json.dumps({"actor_id": "host-a", "role": "author"}), 3),
            )
        if host_role == "executor":
            connection.execute(
                "INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?)",
                ("run-work-1", "work-1", "host-a", "completed", "TEST 宿主工作产出", 0.5, None),
            )
        for node_id, entry, model in (("south-node", "cli:codex", "alpha"), ("east-node", "cli:other", "beta")):
            if node_id == "east-node" and host_settled:
                continue
            connection.execute(
                "INSERT INTO ri_sources VALUES (?,?)",
                (node_id, json.dumps({
                    "reviewer_id": node_id, "kind": "review",
                    "source_ref": "agent-entry:" + node_id,
                    "node": {"enabled": True, "removed": False, "interface": "cli",
                             "entry": entry, "model": model, "agent_type": "other",
                             "test": {"status": "passed"}},
                }, ensure_ascii=False)),
            )
        connection.commit()
        connection.close()
        return path


class ReleaseAcceptanceHostVoteTests(_AcceptanceDatabaseFixture):
    def validate(self, path):
        return self.build_release.validate_acceptance_database(path, "task-1", self.root)

    def test_one_pool_vote_and_one_host_vote_are_accepted_with_both_sources_named(self):
        result = self.validate(self.database())
        sources = [item["source"] for item in result["conclusions"]]
        self.assertEqual(["pool", "host"], sources)
        host = result["conclusions"][1]
        self.assertEqual("host-a", host["host_id"])
        self.assertEqual("review-model", host["model"])
        self.assertTrue(host["receipt_sha256"])
        self.assertEqual(["host-a"], result["execution"]["checked_idle_ids"])
        self.assertEqual([], result["execution"]["author_ids"])
        self.assertEqual([], result["execution"]["executor_ids"])

    def check_card56_conflict(self, host):
        with self.assertRaisesRegex(RuntimeError, "自相矛盾"):
            self.validate(self.database(host_settled=host,
                pool_body="审查结论: FAIL\n---\nFINAL_ACCEPTANCE: PASS\nTEST body"))

    def test_card56_host_build_rejects_header_body_conflict(self):
        self.check_card56_conflict(True)

    def test_card56_pool_build_rejects_header_body_conflict(self):
        self.check_card56_conflict(False)

    def check_card56_strict_final(self, host):
        with self.assertRaisesRegex(RuntimeError, "FINAL_ACCEPTANCE"):
            self.validate(self.database(host_settled=host,
                pool_body="审查结论: PASS\n---\nTEST body"))

    def test_card56_host_header_alone_never_replaces_strict_final(self):
        self.check_card56_strict_final(True)

    def test_card56_pool_header_alone_never_replaces_strict_final(self):
        self.check_card56_strict_final(False)

    def test_host_fail_verdict_is_not_counted_as_a_pass(self):
        with self.assertRaisesRegex(RuntimeError, "宿主结论是 FAIL"):
            self.validate(self.database(host_verdict="FAIL"))

    def test_host_recorded_as_author_on_the_declared_work_task_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "宿主是作者或执行者"):
            self.validate(self.database(host_role="author"))

    def test_host_recorded_as_executor_on_the_declared_work_task_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "宿主是作者或执行者"):
            self.validate(self.database(host_role="executor"))

    def test_host_receipt_bound_to_another_snapshot_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "冻结题面"):
            self.validate(self.database(receipt_snapshot="b" * 64))

    def test_task_without_a_declared_source_work_task_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "没有声明来源工作任务"):
            self.validate(self.database(declare=False))

    def test_explicit_no_source_work_task_is_accepted(self):
        result = self.validate(self.database(declare="none"))
        self.assertEqual([], result["execution"]["author_ids"])

    def test_two_pool_nodes_still_pass_without_a_host_vote(self):
        result = self.validate(self.database(host_settled=False))
        self.assertEqual(["pool", "pool"], [item["source"] for item in result["conclusions"]])

    def test_host_settlement_with_no_pool_vote_is_refused(self):
        # “两票都是宿主”在构包这一层的形态：结算记录里没有任何池节点结论。
        with self.assertRaisesRegex(RuntimeError, "至少要有一份池节点结论"):
            self.validate(self.database(pool_run_ids=()))

    def test_pool_vote_that_says_fail_is_refused_even_with_a_host_pass(self):
        # 卡 49 第二轮 CodeBuddy：池节点写明 FAIL 时，宿主 PASS 不能把它凑成通过。
        body = "证据引用：task:task-1\n审查结论：FAIL\nFINAL_ACCEPTANCE: FAIL\nTEST 池节点发现阻断缺陷"
        with self.assertRaisesRegex(RuntimeError, "不是 FINAL_ACCEPTANCE: PASS"):
            self.validate(self.database(pool_body=body))

    def test_pool_vote_that_needs_info_is_refused_even_with_a_host_pass(self):
        body = "证据引用：task:task-1\n审查结论：NEEDS_INFO\nTEST 池节点缺材料"
        with self.assertRaisesRegex(RuntimeError, "不是 FINAL_ACCEPTANCE: PASS"):
            self.validate(self.database(pool_body=body))

    def test_pool_vote_with_pass_and_fail_lines_is_refused(self):
        body = POOL_BODY + "\nFINAL_ACCEPTANCE: FAIL"
        with self.assertRaisesRegex(RuntimeError, "不是 FINAL_ACCEPTANCE: PASS"):
            self.validate(self.database(pool_body=body))

    def test_pool_vote_conclusions_follow_the_written_verdict(self):
        votes = self.build_release.pool_vote_conclusions(
            [{"id": "r1", "agent_name": "a", "output": POOL_BODY},
             {"id": "r2", "agent_name": "b", "output": "审查结论：FAIL\nFINAL_ACCEPTANCE: FAIL"}],
            "p" * 64, "m" * 64)
        self.assertEqual([True, False], [item["passed"] for item in votes])

    def test_a_non_review_host_settlement_is_not_an_acceptance_vote(self):
        # 宿主直接干完活（host_manual）没有审查结论，不是验收的一票。
        with self.assertRaisesRegex(RuntimeError, "不是从宿主审查结算记录来的"):
            self.validate(self.database(host_kind="host_manual"))


class PoolVoteReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = _isolated_config(self.root, [])
        self.store = EventStore(self.config.database_path)
        self.store.initialize()
        self.enterContext(patch("xiaoba_agentos.orchestrator.release_binding_status", return_value={"ok": True}))
        self.enterContext(_patch_pool_cli())

    def seed(self, count=1):
        _seed(self.store, [_page_source(str(i), "cli:fake" + str(i), str(i), agent_type="other")
                           for i in range(count)])

    def host(self):
        return {"id": "host", "entry": "cli:host", "model": "test"}

    def receipt(self, task_id):
        task = self.store.get_task(task_id)
        return {"receipt_id": "host-" + task_id, "task_id": task_id,
                "snapshot_sha256": task["execution_snapshot_hash"], "reviewer_id": "host",
                "verdict": "PASS", "findings": "TEST 宿主实际结论",
                "evidence_refs": ["task:" + task_id], "review_type": "new_experience",
                "score": 8, "applicability": "TEST fixtures only", "duplicate_of": None,
                "started_at": time.time() - 1, "finished_at": time.time()}

    def run_ids(self, task_id):
        with self.store.read_only() as db:
            return [row["id"] for row in db.execute(
                "SELECT id FROM agent_runs WHERE task_id=? ORDER BY created_at", (task_id,))]

    def probe_run(self, task_id, node_id, text):
        """节点“测试运行”若写 agent_runs 的形状：同一节点、同一任务、purpose=test。"""
        run = self.store.start_agent_run(task_id, node_id, {"purpose": "test", "step_code": "TEST-01"})
        self.store.finish_agent_run(run, "completed", text, None, 1)
        return run

    def test_test_run_during_the_host_wait_does_not_void_the_review_vote(self):
        self.seed()
        task = _submit_task(self.config, self.store, "TEST 审查", experience_kind="audit", host=self.host())
        self.assertEqual("awaiting_host", Orchestrator(self.config, self.store).run_once()["status"])
        vote = self.run_ids(task)[0]
        self.probe_run(task, "0", "challenge-only-probe-answer")
        before = self.store.agent_invocation_count(task)
        for _ in range(2):
            self.assertEqual("awaiting_host", Orchestrator(self.config, self.store).run_once()["status"])
        self.assertEqual(before, self.store.agent_invocation_count(task), "测试运行后不该重新派活")
        self.assertEqual(1, self.store.get_task(task)["attempts"])
        complete_host(self.config, self.store, task, self.receipt(task))
        self.assertEqual("completed", self.store.get_task(task)["status"])
        provenance = json.loads(self.store.get_task(task)["provenance_json"])
        self.assertEqual([vote], provenance["pool_run_ids"], "复用的是那次审查运行的 run_id")
        self.assertNotIn("challenge-only-probe-answer", self.store.get_task(task)["result"])

    def test_card56_header_pass_body_fail_is_refused(self):
        self.seed()
        task = _submit_task(self.config, self.store, "TEST 审查", experience_kind="audit", host=self.host())
        with patch("test_pool_dispatch._FakePoolResource.run_task", return_value={
                "status": "completed", "body": "FINAL_ACCEPTANCE: FAIL\nTEST negative body",
                "metadata": {"审查结论": "PASS"}, "evidence_refs": ["TEST evidence"]}):
            self.assertEqual("awaiting_host", Orchestrator(self.config, self.store).run_once()["status"])
        with self.assertRaisesRegex(ValueError, "自相矛盾"):
            complete_host(self.config, self.store, task, self.receipt(task))

    def test_submit_declares_the_source_work_task_or_explicitly_none(self):
        self.seed()
        work = _submit_task(self.config, self.store, "TEST 工作", needs_review=False)
        declared = _submit_task(self.config, self.store, "TEST 验收甲", work_task_id=work,
                                host=self.host())
        snapshot = json.loads(self.store.get_task(declared)["execution_snapshot_json"])
        self.assertEqual(([work], False), declared_work_tasks(snapshot))
        none = _submit_task(self.config, self.store, "TEST 验收乙", no_work_task=True,
                            host=self.host())
        snapshot = json.loads(self.store.get_task(none)["execution_snapshot_json"])
        self.assertEqual(([], True), declared_work_tasks(snapshot))
        with self.assertRaisesRegex(ValueError, "只能声明一个"):
            _submit_task(self.config, self.store, "TEST 验收丙", work_task_id=work,
                         no_work_task=True, host=self.host())

    def test_pool_vote_without_review_receipt_fields_is_not_a_vote(self):
        self.seed()
        task = _submit_task(self.config, self.store, "TEST 审查", experience_kind="audit", host=self.host())
        self.assertEqual("awaiting_host", Orchestrator(self.config, self.store).run_once()["status"])
        run = self.store.start_agent_run(task, "0", {"purpose": "review", "step_code": "REVIEW-01"})
        self.store.finish_agent_run(run, "completed", "TEST 没有结论字段的正文", None, 1)
        with self.assertRaisesRegex(ValueError, "审查回执"):
            complete_host(self.config, self.store, task, self.receipt(task))
        self.assertNotEqual("completed", self.store.get_task(task)["status"])
