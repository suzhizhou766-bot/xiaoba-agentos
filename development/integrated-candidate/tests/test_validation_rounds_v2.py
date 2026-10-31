"""卡 4：经验核验四项修复的验收测试（真实断言钉住修复前后的行为差异）。

覆盖：
1. 三态判定 extract_trial_verdict（PASS / FAIL / NEEDS_INFO，且必须独占一行且唯一）。
2. 轮次决策 decide_round：按 agent 名分 dsh / peer 两列，各取最不利；NEEDS_INFO 暂停不消耗轮次；
   第 2 轮一 PASS 一 FAIL 记“分歧”（awaiting_diff_materials），绝不退役。
3. 门禁 verify_final_qualification 与 submit_validation_diff_materials 接受分歧记录。
4. resume_paused_validation 把 NEEDS_INFO 暂停恢复到原轮次。
5. ingress_evidence 的混合回执：第二轮一 PASS 一 FAIL 算分歧，其余未全通过仍拒绝。
6. 应用绑定：终审按“本轮试验卡绑定的那次应用”检查，不按最新一条。

所有执行都是本地 Seatbelt 临时试验，审查者与审查入口全部是 TEST 合成身份。
修复前的旧行为写在各测试的“修复前：”注释里。
"""
import copy
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fixtures_review import _config, _event_count
from fixtures_validation import make_execution, native_trial, bound_diff
from xiaoba_agentos.experience_loop import (
    application_packet, final_binding, record_application, record_delivery,
)
from xiaoba_agentos.experience_workflow import (
    advance_validation_round, complete_audit, extract_trial_verdict, prepare_trial,
    reserve_validation_attempt, resume_paused_validation, save_execution_receipt,
    trial_summary,
    verify_final_qualification,
)
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.workflow import verified_snapshot

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "review-ingress"))
import xiaoba_agentos  # noqa: E402,F401
import xiaoba_review  # noqa: E402,F401
from xiaoba_review.store import DecisionInbox, ReviewStore  # noqa: E402


def review_output(verdict):
    """审查员的完整输出：一行正文 + 恰好一行判定标记。"""
    return "TEST independent review\nTRIAL_VALIDATION: " + verdict + "\n"


def trial_card(**extra):
    card = {"problem_key": "parse-fixture"}
    card.update(extra)
    return card


def mixed_trial(store, cand, outputs, passed=True, settle=True, card=None):
    """按审查员名字给不同输出的试验；流程与 fixtures_validation.native_trial 一致。

    native_trial 的 output 参数对所有名字用同一份输出，无法造出 dsh/peer 结论不同的一轮，
    所以这里照抄同一套流程（reserve -> 执行回执 -> prepare -> submit -> 逐名 start/finish
    -> judge.decided -> complete_audit），只是输出按名字取。
    names 必须含一个带 "dsh" 的名字（dsh 列）和一个不带的名字（peer 列）。
    """
    names = tuple(outputs)
    runtime = Path(cand["staged_path"]).parent.parent
    card, execution = make_execution(runtime, passed, trial_card(**(card or {})))
    attempt = reserve_validation_attempt(store, cand["task_id"], card) if store.is_validation_extension_installed() else None
    cfg = SimpleNamespace(runtime_dir=runtime)
    save_execution_receipt(cfg, cand["task_id"], card, execution)
    trial, prompt = prepare_trial(store, cand["task_id"], card, execution, attempt_id=attempt)
    tid = store.submit(prompt, 1, snapshot={"experience_policy": {"version": 1, "kind": "audit"},
        "experience_trial": trial, "agents": [{"name": n, "enabled": True} for n in names]})
    for name in names:
        run = store.start_agent_run(tid, name)
        store.finish_agent_run(run, "completed", outputs[name], None, 1)
    store.append_event(tid, "judge.decided", {"successful_agents": len(names), "winner": names[0]})
    if settle:
        complete_audit(store, tid, "TEST independent audit receipt")
    return {"task_id": tid, "plan_digest": trial["card_sha256"], "execution_digest": trial["execution_sha256"],
            "execution": execution, "trial": trial}


class ValidationRoundsV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.cfg = _config(self.root)
        self.store = EventStore(self.cfg.database_path); self.store.initialize(); self.store.apply_validation_extension()
        # review-ingress 夹具：两个明确的 TEST 合成来源，一 dsh 一 peer。
        self.outbox = self.cfg.runtime_dir / "memory-outbox"
        self.rs = ReviewStore(self.store, self.outbox); self.inbox = DecisionInbox()
        for name, kind in (("TEST-dsh", "dsh"), ("TEST-peer", "peer")):
            self.rs.register_source({"reviewer_id": name, "kind": kind, "source_ref": "test://" + name,
                                     "provenance": "TEST synthetic independent source"})
        # experience_loop 夹具：两个真实存在的工作目录，各自有真实产物与执行证据。
        self.project = self.root / "real-task"; self.project.mkdir()
        self.other = self.root / "real-task-2"; self.other.mkdir()
        self.artifact = self.file(self.project, "product.txt", "TEST delivered product v1"); self.artifact["version"] = "v1"
        self.evidence = self.file(self.project, "execution.txt", "TEST actual application assertions and execution log")
        self.delivery_record = self.file(self.project, "delivery.md", "TEST user acceptance and task delivery record")
        self.artifact_b = self.file(self.other, "product-b.txt", "TEST delivered product v2"); self.artifact_b["version"] = "v2"
        self.evidence_b = self.file(self.other, "execution-b.txt", "TEST second application execution log")

    # ---------- 共同夹具（照抄既有测试的封装） ----------
    def file(self, directory, name, content):
        path = directory / name
        path.write_text(content)
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def candidate(self):
        c = self.store.create_authored_candidate("TEST parse-fixture method", "TEST reusable method steps",
            self.outbox, author="test", experience_kind="method", problem_key="parse-fixture")
        self.store.review_memory(c["task_id"], "delivery", "approve", "TEST human initial review", c["evidence_sha256"])
        return c

    def gate(self, c):
        with self.store.read_only() as db:
            return verify_final_qualification(db, c["task_id"])

    def records(self, c):
        return self.store.get_validation_records(c["task_id"])

    def lineage(self, c):
        return self.store.get_validation_lineage(c["task_id"])

    def status(self, c):
        return self.store.get_memory_candidate(c["task_id"])["status"]

    def resume(self, c):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return resume_paused_validation(self.store, db, c["task_id"])

    def events(self, c, kind):
        return _event_count(self.store, c["task_id"], kind)

    def application(self, c, real_task_id="host:TEST-task-1", workspace=None, artifact=None,
                    evidence=None, task_type="TEST parsing"):
        with self.store.read_only() as conn:
            review = conn.execute("SELECT id FROM memory_reviews WHERE task_id=? ORDER BY rowid DESC LIMIT 1", (c["task_id"],)).fetchone()[0]
        return {"candidate_id": c["task_id"], "candidate_sha256": c["evidence_sha256"], "delivery_review_id": review,
            "real_task": {"id": real_task_id, "problem_key": "parse-fixture", "task_type": task_type,
                "description": "TEST similar real task, not the candidate-authoring task",
                "workspace": str(workspace or self.project), "authorization_ref": "TEST user request 1"},
            "conditions_checked": {"applicability": "TEST same input format", "exclusions": "TEST no excluded conditions",
                "environment": "TEST local fixture environment"},
            "artifact": artifact or self.artifact, "application_location": "TEST parser step 2",
            "steps_applied": "TEST applied the candidate normalization steps", "deviations": "TEST no deviations",
            "execution_evidence": [evidence or self.evidence]}

    def delivery(self, **changes):
        return dict({"origin": "user", "user_message": "交付", "message_id": "TEST-message-1",
            "source_ref": "TEST explicit human event", "real_task_id": "host:TEST-task-1", "artifact": self.artifact,
            "delivery_record": self.delivery_record}, **changes)

    def request(self, c, task=None):
        evidence = [{"ref": "test://execution", "sha256": task["execution_digest"] if task else "a" * 64}]
        return self.rs.create(c["task_id"], "TEST-dsh", "TEST-peer", "TEST真实执行审查", evidence)

    def receipts(self, req, verdicts=None, verdict="PASS"):
        """照抄既有入口夹具，多一个 verdicts 参数按角色分别写结论。"""
        receipts = {}
        for assignment in req["assignments"]:
            card = assignment["card"]
            data = {k: card[k] for k in ("review_request_id", "assignment_id", "reviewer_role", "plan_revision",
                                         "reviewer_id", "source_ref", "candidate_sha256", "input_digest", "card_digest")}
            role_verdict = (verdicts or {}).get(assignment["role"], verdict)
            data.update(receipt_id=assignment["id"] + "-first", verdict=role_verdict,
                        findings="TEST证据独立核查", evidence_refs=card["evidence_refs"],
                        started_at=time.time(), finished_at=time.time())
            received = self.rs.receive(data); self.rs.attest(received["id"], received["digest"], "TEST来源核对")
            receipts[assignment["role"]] = data
        self.rs.deliver(self.inbox)
        return receipts

    # ---------- 规格 1：三态判定的解析边界 ----------
    def test_extract_trial_verdict_accepts_three_states_only_when_unique_line(self):
        """规格 1：恰好一行 TRIAL_VALIDATION 才认三态，多行/混写/缺行/泄漏仍 FAIL。"""
        self.assertEqual(extract_trial_verdict(review_output("PASS")), "PASS")
        self.assertEqual(extract_trial_verdict(review_output("FAIL")), "FAIL")
        # 修复前：旧正则只认 (PASS|FAIL)，NEEDS_INFO 直接落成 FAIL，需补材料的审查被判死。
        self.assertEqual(extract_trial_verdict(review_output("NEEDS_INFO")), "NEEDS_INFO")
        # 多行/混写：两个标记谁也不认。
        self.assertEqual(extract_trial_verdict("TEST review\nTRIAL_VALIDATION: PASS\nTRIAL_VALIDATION: NEEDS_INFO\n"), "FAIL")
        # 行内混写（正文提到标记）不算独占一行。
        self.assertEqual(extract_trial_verdict("TEST review TRIAL_VALIDATION: NEEDS_INFO 但证据不足\n"), "FAIL")
        # 缺行。
        self.assertEqual(extract_trial_verdict("TEST independent review only\n"), "FAIL")
        # 泄漏思考标记 / 非正常标记仍 FAIL，即便后面跟着干净的判定行。
        self.assertEqual(extract_trial_verdict("<thinking>TEST</thinking>\nTRIAL_VALIDATION: NEEDS_INFO\n"), "FAIL")
        self.assertEqual(extract_trial_verdict('<invoke name="x">\nTRIAL_VALIDATION: PASS\n'), "FAIL")
        # 非字符串。
        self.assertEqual(extract_trial_verdict(None), "FAIL")

    # ---------- 验收 1：NEEDS_INFO 暂停不消耗轮次，恢复回原轮次 ----------
    def test_needs_info_first_round_pauses_without_consuming_round(self):
        """第 1 轮有一人 NEEDS_INFO → 暂停、轮次不变；恢复后在同一轮重判通过。"""
        c = self.candidate()
        mixed_trial(self.store, c, {"dsh_review": review_output("NEEDS_INFO"), "agy_review": review_output("PASS")})
        lineage = self.lineage(c)
        self.assertEqual(lineage["rounds_used"], 0)            # 轮次没被消耗
        self.assertEqual(lineage["lineage_status"], "paused")
        record = self.records(c)[0]
        self.assertEqual(record["round"], 1)
        self.assertEqual(record["status"], "paused")
        self.assertEqual(record["failure_class"], "needs_info")
        self.assertTrue(record["pause_reason"].startswith("needs_info"))
        # 原审查任务已结束：清掉 task_id，恢复后才能在同一轮重新发起。
        self.assertIsNone(record["task_id"])
        self.assertEqual(self.status(c), "delivery_approved")   # 候选状态不动
        self.assertFalse(self.gate(c)["qualified"])
        self.assertEqual(self.gate(c)["state"], "validation_paused")
        self.assertEqual(self.events(c, "memory.validation_paused"), 1)
        # 修复前：NEEDS_INFO 被当成 FAIL → 第 1 轮直接判 failed、消耗轮次进第二轮，没有暂停/恢复。
        resumed = self.resume(c)
        self.assertEqual(resumed["resumed_round"], 1)
        self.assertEqual(resumed["action"], "retry_interrupted_execution")
        native_trial(self.store, c, card=trial_card())                              # 同一轮重跑，两个审查员都 PASS
        self.assertEqual(self.lineage(c)["rounds_used"], 1)
        result = self.gate(c)
        self.assertTrue(result["qualified"], result)
        self.assertEqual(result["rounds_used"], 1)               # 回原轮次，没走到第二轮
        self.assertEqual(self.status(c), "pending_final_review")

    # ---------- 验收 2：第 1 轮 PASS/FAIL 分列且进第二轮 ----------
    def test_round1_split_pass_fail_records_separate_columns_and_enters_round2(self):
        """第 1 轮 dsh PASS + peer FAIL → 两列各写各的、进第二轮、不进待终审。"""
        c = self.candidate()
        mixed_trial(self.store, c, {"dsh_review": review_output("PASS"), "agy_review": review_output("FAIL")})
        record = self.records(c)[0]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["dsh_verdict"], "PASS")
        self.assertEqual(record["peer_verdict"], "FAIL")
        # 修复前：同一个 verdict 同时写进两列（都写 FAIL），分不清谁说了什么。
        self.assertEqual(self.lineage(c)["rounds_used"], 1)
        self.assertEqual(self.lineage(c)["lineage_status"], "pending_round_2")
        self.assertEqual(self.status(c), "revalidation_required")
        result = self.gate(c)
        self.assertFalse(result["qualified"])
        self.assertNotEqual(result["state"], "ready_for_final")
        self.assertNotEqual(result["state"], "diff_materials_required")
        self.assertEqual(result["state"], "revalidation_required")

    # ---------- 验收 3：第 1 轮 FAIL + NEEDS_INFO 进第二轮 ----------
    def test_round1_fail_and_needs_info_enters_round2(self):
        """第 1 轮 FAIL 优先于 NEEDS_INFO：照常进第二轮，不暂停不退役。"""
        c = self.candidate()
        mixed_trial(self.store, c, {"dsh_review": review_output("FAIL"), "agy_review": review_output("NEEDS_INFO")})
        record = self.records(c)[0]
        self.assertEqual(record["status"], "failed")
        self.assertEqual((record["dsh_verdict"], record["peer_verdict"]), ("FAIL", "NEEDS_INFO"))
        self.assertEqual(self.lineage(c)["rounds_used"], 1)
        self.assertEqual(self.lineage(c)["lineage_status"], "pending_round_2")
        self.assertEqual(self.status(c), "revalidation_required")
        self.assertEqual(self.events(c, "memory.validation_paused"), 0)
        self.assertEqual(self.events(c, "memory.retired"), 0)
        # 修复前：NEEDS_INFO 落成 FAIL，两列同为 FAIL；结论一样是进第二轮，但列信息丢失。

    # ---------- 验收 4：第 2 轮 FAIL + NEEDS_INFO 暂停待材料 ----------
    def test_round2_fail_and_needs_info_pauses_for_materials(self):
        """第 2 轮同时有 FAIL 与 NEEDS_INFO → 暂停待补材料，不退役；恢复回第 2 轮。"""
        c = self.candidate()
        native_trial(self.store, c, False, card=trial_card())                        # 第 1 轮失败
        mixed_trial(self.store, c, {"dsh_review": review_output("FAIL"), "agy_review": review_output("NEEDS_INFO")})
        lineage = self.lineage(c)
        self.assertEqual(lineage["rounds_used"], 1)                # 仍是已用 1 轮
        self.assertEqual(lineage["lineage_status"], "paused")
        self.assertEqual(lineage["current_round"], 2)
        record = self.records(c)[1]
        self.assertEqual(record["round"], 2)
        self.assertEqual(record["status"], "paused")
        self.assertEqual(record["failure_class"], "needs_info")
        self.assertNotEqual(self.status(c), "retired")
        self.assertEqual(self.events(c, "memory.retired"), 0)
        self.assertEqual(self.gate(c)["state"], "validation_paused")
        # 修复前：第 2 轮任何非全 PASS 都直接 retired，NEEDS_INFO 根本没有“补材料再判”的出口。
        resumed = self.resume(c)
        self.assertEqual(resumed["resumed_round"], 2)
        self.assertEqual(resumed["action"], "retry_interrupted_execution")

    # ---------- 验收 5：第 2 轮分歧 → 待差异材料 → 交齐后待终审（全程链） ----------
    def test_round2_split_pass_fail_with_passed_execution_awaits_diff_materials(self):
        """第 2 轮一 PASS 一 FAIL 且真实执行通过 → 记分歧、要差异材料、交齐后待终审不退役。"""
        c = self.candidate()
        native_trial(self.store, c, False, card=trial_card())                        # 第 1 轮失败
        mixed_trial(self.store, c, {"dsh_review": review_output("PASS"), "agy_review": review_output("FAIL")})
        record = self.records(c)[1]
        self.assertEqual(record["round"], 2)
        self.assertEqual(record["status"], "failed")               # 分歧仍写 failed（不加新状态）
        self.assertEqual(record["failure_class"], "review_disagreement")
        self.assertEqual(record["dsh_verdict"], "PASS")
        self.assertEqual(record["peer_verdict"], "FAIL")
        self.assertEqual(self.lineage(c)["lineage_status"], "awaiting_diff_materials")
        self.assertEqual(self.lineage(c)["rounds_used"], 2)
        self.assertEqual(self.events(c, "memory.validation_disagreement"), 1)
        self.assertEqual(self.events(c, "memory.retired"), 0)      # 分歧绝不按退役写事件
        self.assertNotEqual(self.status(c), "retired")
        # 偏差记录：卡片规格写“候选置 pending_final_review”，实现先把候选留在
        # revalidation_required，交齐差异材料时才升到 pending_final_review（见下文断言）。
        self.assertEqual(self.status(c), "revalidation_required")
        result = self.gate(c)
        self.assertFalse(result["qualified"])
        self.assertEqual(result["state"], "diff_materials_required")
        # 修复前：旧 diff_is_bound 只认 records[2].status=='passed'，旧 submit_diff_materials 对
        # 非 passed 记录直接报错 → 分歧记录既进不了待终审、也补不了材料，只能退役。
        materials = bound_diff(self.store, c["task_id"])
        submitted = self.store.submit_validation_diff_materials(c["task_id"], materials)
        self.assertTrue(submitted["disagreement"])
        self.assertEqual(submitted["outcome"], "disagreement")
        result = self.gate(c)
        self.assertTrue(result["qualified"], result)
        self.assertEqual(result["state"], "ready_for_final")
        self.assertTrue(result["disagreement"])
        self.assertEqual((result["dsh_verdict"], result["peer_verdict"]), ("PASS", "FAIL"))
        # 交齐材料后候选才进待终审：用户仍可拒绝，不是退役。
        self.assertEqual(self.status(c), "pending_final_review")
        self.assertEqual(self.events(c, "memory.retired"), 0)
        # 裁决页说“各审查员原文见下”，所以每轮都要带上任务卡、执行回执和审查员原文。
        with self.store.read_only() as db:
            trials = trial_summary(db, c["task_id"])["trials"]
        second = trials[1]
        self.assertTrue(second["disagreement"])
        self.assertIsInstance(second["card"], dict)
        self.assertIsInstance(second["execution"], dict)
        self.assertEqual({r["agent"] for r in second["reviews"]}, {"dsh_review", "agy_review"})
        self.assertTrue(all("TRIAL_VALIDATION" in r["output"] for r in second["reviews"]))

    # ---------- 验收 6：第 2 轮 PASS/FAIL 但真实执行没过 → 退役，不算分歧 ----------
    def test_round2_split_with_failed_execution_retires_without_disagreement(self):
        """审查结论有分歧但真实执行没过 → 退役，不记分歧（分歧不能替代真实执行）。"""
        c = self.candidate()
        native_trial(self.store, c, False, card=trial_card())                        # 第 1 轮失败
        mixed_trial(self.store, c, {"dsh_review": review_output("PASS"), "agy_review": review_output("FAIL")},
                    passed=False)
        record = self.records(c)[1]
        self.assertEqual(record["status"], "failed")
        self.assertNotEqual(record["failure_class"], "review_disagreement")
        self.assertEqual(self.status(c), "retired")
        self.assertEqual(self.events(c, "memory.validation_disagreement"), 0)
        self.assertEqual(self.events(c, "memory.retired"), 1)
        self.assertFalse(self.gate(c)["qualified"])
        # 退役候选不再进终审门禁：protect_final_decision 分支返回 state='final_decision'。
        self.assertEqual(self.gate(c)["state"], "final_decision")
        with self.assertRaises(ValueError):
            self.store.submit_validation_diff_materials(c["task_id"], bound_diff(self.store, c["task_id"]))
        # 修复前：第 2 轮失败一律 retired；结论相同，但旧实现根本没有“分歧”这个概念。

    # ---------- 验收 7：两轮都失败 → 退役 ----------
    def test_round2_all_fail_retires(self):
        """两轮都失败 → 退役（全 FAIL 不是分歧）。"""
        c = self.candidate()
        native_trial(self.store, c, False, card=trial_card())
        native_trial(self.store, c, False, card=trial_card())
        self.assertEqual(self.records(c)[1]["status"], "failed")
        self.assertNotEqual(self.records(c)[1]["failure_class"], "review_disagreement")
        self.assertEqual(self.lineage(c)["lineage_status"], "retired")
        self.assertEqual(self.status(c), "retired")
        self.assertEqual(self.events(c, "memory.retired"), 1)
        self.assertEqual(self.events(c, "memory.validation_disagreement"), 0)
        self.assertFalse(self.gate(c)["qualified"])
        # 退役候选不再进终审门禁：protect_final_decision 分支返回 state='final_decision'。
        self.assertEqual(self.gate(c)["state"], "final_decision")

    # ---------- 验收 8：原生全 PASS 但外部回执分歧 → 按分歧处理 ----------
    def test_round2_native_all_pass_but_external_receipts_disagree_counts_as_disagreement(self):
        """原生双审全 PASS、外部回执一 PASS 一 FAIL → 仍是分歧，不是“全部通过”。"""
        c = self.candidate()
        native_trial(self.store, c, False, card=trial_card())                        # 第 1 轮失败
        task = native_trial(self.store, c, card=trial_card(), settle=False)          # 第 2 轮原生输出都是 PASS，先不结算
        req = self.request(c, task)                               # record 已带 task_id/execution_digest
        receipts = self.receipts(req, verdicts={"dsh": "PASS", "peer": "FAIL"})
        self.assertEqual({r: d["verdict"] for r, d in receipts.items()}, {"dsh": "PASS", "peer": "FAIL"})
        self.assertTrue(complete_audit(self.store, task["task_id"], "TEST mixed ingress audit receipt"))
        record = self.records(c)[1]
        self.assertEqual(record["status"], "failed")              # 不按“全部通过”结算
        self.assertEqual(record["failure_class"], "review_disagreement")
        self.assertEqual((record["dsh_verdict"], record["peer_verdict"]), ("PASS", "FAIL"))
        self.assertEqual(self.lineage(c)["lineage_status"], "awaiting_diff_materials")
        self.assertEqual(self.events(c, "memory.trial_validated"), 0)
        self.assertEqual(self.events(c, "memory.retired"), 0)
        result = self.gate(c)
        self.assertFalse(result["qualified"])
        self.assertEqual(result["state"], "diff_materials_required")
        # 修复前：外部回执只做“通过/不通过”二值复核，一 FAIL 一 PASS 会把本轮判死。
        self.store.submit_validation_diff_materials(c["task_id"], bound_diff(self.store, c["task_id"]))
        result = self.gate(c)
        self.assertTrue(result["qualified"], result)
        self.assertTrue(result["disagreement"])
        self.assertEqual(self.status(c), "pending_final_review")

    # ---------- 验收 9：ingress_evidence 仍然拒绝 NEEDS_INFO 与第 1 轮 FAIL ----------
    def test_ingress_evidence_still_rejects_fail_needs_info_and_round1_fail(self):
        """9a 第 2 轮回执含 NEEDS_INFO → 拒绝；9b 第 1 轮有 FAIL 回执 → 拒绝（已知遗留）。"""
        # 9a：第 2 轮，一 FAIL 一 NEEDS_INFO。
        c = self.candidate()
        native_trial(self.store, c, False, card=trial_card())
        task = native_trial(self.store, c, card=trial_card(), settle=False)
        req = self.request(c, task)
        receipts = self.receipts(req, verdicts={"dsh": "FAIL", "peer": "NEEDS_INFO"})
        self.assertEqual(receipts["peer"]["verdict"], "NEEDS_INFO")
        complete_audit(self.store, task["task_id"], "TEST needs_info ingress receipt")
        # 回执核不过 → 暂停写明原因，不耗轮次，不再停在 running 卡死。
        self.assertEqual(self.lineage(c)["rounds_used"], 1)
        self.assertEqual(self.lineage(c)["lineage_status"], "paused")
        self.assertEqual(self.records(c)[1]["status"], "paused")
        self.assertEqual(self.records(c)[1]["failure_class"], "external_receipt")
        self.assertNotEqual(self.status(c), "retired")
        self.assertEqual(self.resume(c)["resumed_round"], 2)
        # 修复前：ingress_evidence 只有二值判定，NEEDS_INFO 一样被当成“未通过”，
        # 且第 2 轮非全 PASS 会直接 retired 而不是在结算时报错。

        # 9b：第 1 轮，回执一 FAIL 一 PASS。
        c2 = self.candidate()
        task2 = native_trial(self.store, c2, card=trial_card(), settle=False)
        req2 = self.request(c2, task2)
        self.receipts(req2, verdicts={"dsh": "FAIL", "peer": "PASS"})
        complete_audit(self.store, task2["task_id"], "TEST round1 fail ingress receipt")
        # 第 1 轮外部 FAIL：暂停可恢复，不耗轮次，也不进待终审。
        self.assertEqual(self.lineage(c2)["rounds_used"], 0)
        self.assertEqual(self.lineage(c2)["lineage_status"], "paused")
        self.assertEqual(self.records(c2)[0]["status"], "paused")
        self.assertEqual(self.events(c2, "memory.retired"), 0)
        self.assertFalse(self.gate(c2)["qualified"])

    def test_round2_all_native_fail_retires_even_with_unfinished_external_request(self):
        """原生全部 FAIL 时外部回执改变不了结论：没交回执也照常退役，不卡住。"""
        c = self.candidate()
        native_trial(self.store, c, False, card=trial_card())
        task = mixed_trial(self.store, c, {"dsh_review": review_output("FAIL"), "agy_review": review_output("FAIL")},
                           settle=False)
        self.request(c, task)
        complete_audit(self.store, task["task_id"], "TEST all fail")
        self.assertEqual(self.status(c), "retired")

    def test_round2_three_reviewers_with_one_pass_is_a_disagreement_not_retired(self):
        """实际配置是三名审查员：只要有人 PASS 有人 FAIL 就是分歧，全部 FAIL 才退役。"""
        c = self.candidate()
        native_trial(self.store, c, False, card=trial_card())
        mixed_trial(self.store, c, {"dsh_review": review_output("FAIL"), "agy_review": review_output("PASS"),
                                    "qoder_review": review_output("FAIL")})
        record = self.records(c)[1]
        self.assertEqual(record["failure_class"], "review_disagreement")
        self.assertEqual(self.events(c, "memory.retired"), 0)
        self.assertEqual(self.lineage(c)["lineage_status"], "awaiting_diff_materials")
        self.store.submit_validation_diff_materials(c["task_id"], bound_diff(self.store, c["task_id"]))
        self.assertTrue(self.gate(c)["qualified"])
        self.assertTrue(self.gate(c)["disagreement"])

    # ---------- 验收 10：终审绑定本轮试验卡的应用，不按最新一条 ----------
    def test_gate_binds_the_application_that_started_the_trial_not_the_latest(self):
        """A 绑定本轮试验、B 后登记：终审按 A 检查，A 的交付能让门禁通过。"""
        c = self.candidate()
        app_a = record_application(self.store, self.application(c))
        # 后登记的另一条应用：另一个真实任务、另一个真实目录、新的产物与证据文件。
        app_b = record_application(self.store, copy.deepcopy(self.application(
            c, real_task_id="host:TEST-task-2", workspace=self.other,
            artifact=self.artifact_b, evidence=self.evidence_b, task_type="TEST parsing second")))
        self.assertNotEqual(app_a["application_id"], app_b["application_id"])
        task = native_trial(self.store, c, card=trial_card(experience_application=app_a["trial_binding"]))
        self.assertEqual(self.gate(c)["state"], "application_delivery_required")   # 还没交付
        record_delivery(self.store, self.delivery())             # 只接受 A 的交付
        result = self.gate(c)
        # 修复前：require_application_delivery 取 applications[-1]（这里是 B），
        # 拿 A 的交付对不上 B 的 real_task/artifact → 返回 application_delivery_required，
        # 本条断言会失败；旧 final_binding 也是同一口径。
        self.assertTrue(result["qualified"], result)
        self.assertEqual(result["state"], "ready_for_final")
        self.assertEqual(result["task_id"], task["task_id"])
        packet = self.packet(c)
        self.assertEqual([a["real_task"]["id"] for a in packet["applications"]],
                         ["host:TEST-task-1", "host:TEST-task-2"])
        self.assertEqual(packet["applications"][-1]["application_id"], app_b["application_id"])  # 最新一条是 B
        self.assertEqual(self.status(c), "pending_final_review")

    def packet(self, c):
        with self.store.read_only() as db:
            return application_packet(db, self.store.get_memory_candidate(c["task_id"]))

    # ---------- 旧行为兜底：门禁对没有绑定试验的应用仍按旧规矩拒绝 ----------
    def test_trial_without_application_binding_keeps_old_rejection(self):
        """试验卡没写 experience_application 时维持旧行为：有应用就拒绝，不静默放行。"""
        c = self.candidate()
        record_application(self.store, self.application(c))
        # 拒绝发生在 prepare_trial -> validate_trial_application（修复前后同一行为）。
        with self.assertRaises(ValueError) as caught:
            native_trial(self.store, c, card=trial_card())                          # 不带 experience_application 绑定
        self.assertIn("explicit trial application binding", str(caught.exception))

    # ---------- 发布链兜底：绑定解析失败不能打断终审与发布 ----------
    def test_final_binding_falls_back_when_trial_snapshot_is_tampered(self):
        """发布链依赖 final_binding：发起审查那次任务的快照漂移时退回最新一条，绝不抛异常。"""
        c = self.candidate()
        app = record_application(self.store, self.application(c))
        task = native_trial(self.store, c, card=trial_card(experience_application=app["trial_binding"]))
        with self.store.read_only() as db:
            before = final_binding(db, self.store.get_memory_candidate(c["task_id"]))
        self.assertTrue(before)
        # 篡改发起审查那次任务的执行快照：verified_snapshot 从此抛完整性错误。
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET execution_snapshot_json=? WHERE id=?", ('{"TEST":"tampered"}', task["task_id"]))
        with self.store.read_only() as db:
            row = dict(db.execute("SELECT * FROM tasks WHERE id=?", (task["task_id"],)).fetchone())
        with self.assertRaises(ValueError):
            verified_snapshot(row)                     # 前提：按快照解析绑定现在必然失败
        with self.store.read_only() as db:
            after = final_binding(db, self.store.get_memory_candidate(c["task_id"]))
        # 加了本轮“终审按绑定应用”之后如果没兜住这个异常，终审 approve 与发布都会被 ValueError 打断；
        # 兜住后解析失败退回最新一条（这里就是绑定的那条），绑定摘要不变。
        self.assertEqual(after, before)

    # ---------- 暂停原因可分辨：补材料不是外部故障 ----------
    def test_paused_gate_surfaces_needs_info_reason(self):
        """门禁报暂停原因时要能分清“必须补材料”和“外部环境故障”。"""
        c = self.candidate()
        mixed_trial(self.store, c, {"dsh_review": review_output("NEEDS_INFO"), "agy_review": review_output("PASS")})
        result = self.gate(c)
        self.assertEqual(result["state"], "validation_paused")
        # 修复前：这一分支对两种暂停都只报「外部环境异常待恢复」，needs_info 被说成环境故障。
        self.assertIn("needs_info", result["reasons"][0])


if __name__ == "__main__":
    unittest.main()
