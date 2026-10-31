"""Shared, read-only validation of frozen trial evidence and review provenance."""
import hashlib
import json
import math
from pathlib import Path
import time

from .workflow import verified_snapshot


def checksum(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def independence_key(source_ref, model=None):
    """两个审查员是否独立：看“入口程序＋模型”。

    同一入口用 A 模型、B 模型算两个；同一入口同一模型，改名字也只算一个。
    模型名由用户手填，比较前统一大小写并把连续空白压成一个空格，
    避免“Gemini Flash”和“gemini  flash”被当成两个来源凑独立。
    真实模型名的别名（不同写法指同一模型）无法判断，不在本函数处理。
    旧的四字段来源没有模型资料，仍只按 source_ref 判断，行为不变。
    """
    if not isinstance(source_ref, str) or not source_ref.strip():
        raise ValueError("缺少实际来源标识，无法核对独立性")
    if model is None:
        return source_ref
    if not isinstance(model, str) or not model.strip():
        raise ValueError("模型标识格式异常，无法核对独立性")
    normalized_model = " ".join(model.split()).casefold()
    return source_ref.strip() + "|model:" + normalized_model


def protect_final_decision(conn, candidate):
    final = conn.execute(
        "SELECT decision FROM memory_reviews WHERE task_id=? AND stage='final' ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (candidate["task_id"],)).fetchone()
    if candidate["status"] in {"final_rejected", "retired", "published"} or (final and final[0] in {"reject", "revise", "retire"}):
        raise ValueError("人工终审决定或已结束状态禁止自动推进候选")


def execution_receipt(candidate, trial):
    """A review of JSON is not host execution: verify the saved controller receipt."""
    execution, card = trial.get("execution"), trial.get("card")
    if not isinstance(execution, dict) or not isinstance(card, dict):
        raise ValueError("缺少受控执行或计划快照")
    if checksum(execution) != trial.get("execution_sha256") or checksum(card) != trial.get("card_sha256"):
        raise ValueError("执行/计划快照摘要不一致")
    workspace = execution.get("workspace")
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise ValueError("缺少受控执行工作目录")
    workspace = Path(workspace)
    staged = Path(candidate["staged_path"]).resolve()
    runtime = staged.parent.parent
    root = runtime / "trial-workspaces"
    if (staged.parent.name != "memory-outbox" or workspace.resolve() != workspace
            or workspace == root or not workspace.is_relative_to(root)):
        raise ValueError("执行与候选不属于同一独立试验空间")
    if not str(execution.get("sandbox", "")).startswith("macOS Seatbelt;"):
        raise ValueError("缺少受控沙箱来源")
    value = {"source_task_id": candidate["task_id"], "card": card, "execution": execution}
    path = runtime / "trial-receipts" / (checksum(value) + ".json")
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
        raise ValueError("缺少真实受控执行回执文件")
    if json.loads(path.read_text()) != value:
        raise ValueError("执行回执与冻结快照不一致")
    cases = execution.get("cases")
    planned = card.get("cases")
    if not isinstance(cases, list) or not cases or not isinstance(planned, list) or len(cases) != len(planned):
        raise ValueError("执行用例与计划不一致")
    start, finish = execution.get("started_at"), execution.get("finished_at")
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in (start, finish)) or not 0 < start <= finish <= time.time() + 5:
        raise ValueError("执行时间无效")
    passed = execution.get("passed") is True
    for result, plan in zip(cases, planned):
        if not isinstance(result, dict) or not isinstance(plan, dict):
            raise ValueError("执行用例格式无效")
        if result.get("argv") != plan.get("argv") or result.get("expect_stdout") != plan.get("expect_stdout"):
            raise ValueError("用例断言或命令与计划不一致")
        expect, output = result.get("expect_stdout"), result.get("output")
        good = (result.get("passed") is True and result.get("returncode") == 0
                and not result.get("timed_out") and not result.get("truncated")
                and isinstance(expect, str) and bool(expect.strip()) and isinstance(output, str) and expect in output)
        passed = passed and good
    return execution, bool(passed)


def task_evidence(conn, candidate, approved, record):
    """Validate identity first; return actual outcomes, never caller-supplied PASS."""
    from .experience_workflow import extract_trial_verdict
    task_id = record.get("task_id")
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone() if task_id else None
    if not row or row["status"] != "completed":
        raise ValueError("缺少已完成的真实受控执行审查任务")
    task = dict(row)
    snapshot = verified_snapshot(task)
    if snapshot.get("experience_policy", {}).get("kind") != "audit":
        raise ValueError("验证任务必须是审计任务")
    trial = snapshot.get("experience_trial")
    if not isinstance(trial, dict) or (
        trial.get("source_task_id"), trial.get("candidate_sha256"), trial.get("delivery_review_id")
    ) != (candidate["task_id"], candidate["evidence_sha256"], approved["id"]):
        raise ValueError("验证任务与候选/初审版本不一致")
    execution, passed = execution_receipt(candidate, trial)
    if execution["started_at"] < approved["created_at"]:
        raise ValueError("执行证据早于当前初审")
    if record.get("plan_digest") != trial["card_sha256"] or record.get("execution_digest") != trial["execution_sha256"]:
        raise ValueError("轮次记录与任务执行/计划未同源绑定")
    if record.get("candidate_sha256", candidate["evidence_sha256"]) != candidate["evidence_sha256"] or record.get("delivery_review_id", approved["id"]) != approved["id"]:
        raise ValueError("轮次绑定失效")
    if trial.get("validation_attempt_id") and trial["validation_attempt_id"] != record.get("id"):
        raise ValueError("执行任务不属于当前验证尝试")
    settled = conn.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='audit.settled' ORDER BY sequence DESC LIMIT 1", (task_id,)).fetchone()
    result_hash = hashlib.sha256((task.get("result") or "").encode()).hexdigest()
    if not settled or json.loads(settled[0]).get("result_sha256") != result_hash:
        raise ValueError("任务缺少可核对的原生审计结算")
    judge = conn.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='judge.decided' ORDER BY sequence DESC LIMIT 1", (task_id,)).fetchone()
    if not judge or json.loads(judge[0]).get("successful_agents", 0) < 2:
        raise ValueError("缺少原生双审裁判记录")
    names = {a["name"] for a in snapshot.get("agents", []) if a.get("enabled")}
    if len(names) < 2:
        raise ValueError("冻结计划缺少两个独立审查者")
    runs = {r["agent_name"]: dict(r) for r in conn.execute("SELECT * FROM agent_runs WHERE task_id=? ORDER BY created_at,rowid", (task_id,))}
    verdicts = {name: extract_trial_verdict(runs[name].get("output") or "")
                if name in runs and runs[name]["status"] == "completed" else "FAIL" for name in names}
    return {"execution": execution, "execution_passed": passed, "verdicts": verdicts,
            "reviews_passed": all(v == "PASS" for v in verdicts.values()), "trial": trial}


def binding(record):
    return {"attempt_id": record["id"], "round": record["round"], "task_id": record["task_id"],
            "plan_digest": record["plan_digest"], "execution_digest": record["execution_digest"]}


def ingress_evidence(conn, candidate, approved, record, execution):
    """Optional external reviews add requirements; they never replace execution."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    explicit = [record.get("dsh_receipt_id"), record.get("peer_receipt_id")]
    if "ri_requests" not in tables:
        if any(explicit):
            raise ValueError("回执实体不存在")
        return
    expected = binding(record)
    requests = []
    for row in conn.execute("SELECT * FROM ri_requests WHERE candidate_id=? ORDER BY rowid DESC", (candidate["task_id"],)):
        common = json.loads(row["common"])
        if common.get("validation_binding") == expected:
            requests.append((dict(row), common))
    if not requests:
        if any(explicit):
            raise ValueError("回执未绑定当前轮次/任务/计划与执行")
        return
    request, common = requests[0]
    if common.get("candidate_sha256") != candidate["evidence_sha256"] or common.get("delivery_review", {}).get("id") != approved["id"]:
        raise ValueError("审查请求的初审/候选已失效")
    if record["execution_digest"] not in {x.get("sha256") for x in common.get("evidence_refs", [])}:
        raise ValueError("审查材料未包含当前真实执行摘要")
    if request["state"] in {"waiting_receipts", "stale"}:
        raise ValueError("当前回执待认证或计划失效")
    packet = conn.execute("SELECT * FROM ri_inbox WHERE request_id=? AND current=1", (request["id"],)).fetchone()
    if not packet:
        raise ValueError("当前审查材料包已被撤销或未投递")
    payload = json.loads(packet["data"])
    if checksum(payload) != packet["digest"] or payload.get("all_pass") is not True or payload.get("plan_revision") != request["revision"]:
        raise ValueError("当前材料包未全数通过或版本不一致")
    assignments = [dict(a) for a in conn.execute("SELECT * FROM ri_assignments WHERE request_id=? AND active=1", (request["id"],))]
    if len(assignments) != 2 or {a["role"] for a in assignments} != {"dsh", "peer"}:
        raise ValueError("缺少当前独立双审任务卡")
    ids, sources = {}, set()
    for assignment in assignments:
        receipt = conn.execute("SELECT * FROM ri_receipts WHERE assignment_id=? ORDER BY rowid DESC LIMIT 1", (assignment["id"],)).fetchone()
        if not receipt or receipt["status"] != "valid" or not receipt["attestation"]:
            raise ValueError("当前回执尚未认证或已失效")
        data, card = json.loads(receipt["data"]), json.loads(assignment["card"])
        frozen_card = dict(card)
        frozen_digest = frozen_card.pop("card_digest", None)
        if checksum(frozen_card) != frozen_digest or card.get("input_digest") != checksum(common):
            raise ValueError("审查任务卡或证据输入已变动")
        if checksum(data) != receipt["digest"] or data.get("verdict") != "PASS":
            raise ValueError("当前回执未通过或摘要不一致")
        source = conn.execute("SELECT data FROM ri_sources WHERE id=?", (assignment["source_id"],)).fetchone()
        if not source:
            raise ValueError("审查来源未登记")
        source = json.loads(source[0])
        keys = ("review_request_id", "assignment_id", "reviewer_role", "plan_revision", "reviewer_id", "source_ref", "candidate_sha256", "input_digest", "card_digest")
        if any(data.get(k) != card.get(k) for k in keys) or data.get("plan_revision") != request["revision"]:
            raise ValueError("回执与当前任务卡/角色/版本不一致")
        if data.get("source_ref") != source["source_ref"] or data.get("reviewer_id") != assignment["source_id"]:
            raise ValueError("回执来源不一致")
        if data.get("started_at", 0) < execution["finished_at"]:
            raise ValueError("审查时间早于执行证据完成")
        # 独立性按“入口程序＋模型”；旧四字段来源的任务卡没有 resource_plan，仍只看 source_ref。
        plan = card.get("resource_plan")
        sources.add(independence_key(source["source_ref"], plan.get("model") if isinstance(plan, dict) else None))
        ids[assignment["role"]] = receipt["id"]
    if len(sources) != 2 or len(set(ids.values())) != 2:
        raise ValueError("两份回执来源不独立")
    if any(explicit) and explicit != [ids["dsh"], ids["peer"]]:
        raise ValueError("轮次记录不是当前双审回执")
    packet_ids = {r["receipt"]["receipt_id"] for r in payload.get("reviews", [])}
    if packet_ids != set(ids.values()):
        raise ValueError("材料包不对应最新回执")
