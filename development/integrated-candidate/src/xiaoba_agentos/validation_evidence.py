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


# 审查结论只有三种：通过、未通过、需补材料。FAIL 比 NEEDS_INFO 更不利。
_VERDICT_SEVERITY = {"PASS": 0, "NEEDS_INFO": 1, "FAIL": 2}


def _normalize_verdict(verdict):
    """未知写法一律按 FAIL 处理，保持原来的严格口径。"""
    return verdict if verdict in _VERDICT_SEVERITY else "FAIL"


def _worst_verdict(values, fallback=None):
    ranked = [_normalize_verdict(v) for v in values if v is not None]
    if not ranked:
        return fallback
    return max(ranked, key=lambda v: _VERDICT_SEVERITY[v])


def role_verdicts(verdicts):
    """把本轮审查结论拆成 dsh / peer 两列，各记各的结论，不互相覆盖。

    沿用既有命名约定：名字含 dsh 的算 DSH 侧，其余算 AGY(peer) 侧。
    同一侧有多名审查员时取最不利结论——合并只会更严，不会把未通过的洗成通过。
    计划里没有按两侧命名的审查员时退回单一结论，保持“两列都 PASS 当且仅当全部通过”。
    宿主兜底仅有 peer_host_review 时不把 dsh 列回填成 PASS，避免伪装双票。
    """
    items = list((verdicts or {}).items())
    dsh = [v for n, v in items if "dsh" in str(n).lower()]
    peer = [v for n, v in items if "dsh" not in str(n).lower()]
    combined = _worst_verdict(list((verdicts or {}).values()))
    if dsh:
        return {
            "dsh": _worst_verdict(dsh),
            "peer": _worst_verdict(peer, combined if not peer else None),
        }
    if peer and all("host" in str(n).lower() for n, _ in items):
        return {"dsh": None, "peer": _worst_verdict(peer)}
    return {"dsh": combined, "peer": _worst_verdict(peer, combined)}


def decide_round(number, execution_passed, verdicts, external=None):
    """按 CONTRACTS 第 2 节的两轮判定边界给出一轮结论（纯函数，结算与终审共用）。

    verdicts 是本轮原生审查员 name->结论；external 是外部回执 role->结论。
    外部回执只许更严：按角色取最不利合并，原生未通过不会因为回执通过就变通过。
    返回 (outcome, columns)：outcome 取 passed / failed / paused / disagreement，
    columns 是写库用的 dsh、peer 两列结论。
    """
    columns = role_verdicts(verdicts)
    for role, value in (external or {}).items():
        if role in columns and value is not None:
            columns[role] = _worst_verdict([columns[role], value])
    signals = [_normalize_verdict(v) for v in
               list((verdicts or {}).values()) + list((external or {}).values())]
    needs_info = "NEEDS_INFO" in signals
    any_fail = "FAIL" in signals
    column_values = [v for v in columns.values() if v]
    all_pass = bool(column_values) and _worst_verdict(column_values) == "PASS"
    # 看每位审查员本人的结论：同一列多人时列值取最不利，不能把列里的 PASS 洗掉。
    any_pass = "PASS" in signals
    if int(number or 1) < 2:
        # 第一轮：有 FAIL 就是本轮未通过，进第二轮重审。
        if any_fail or not execution_passed:
            return "failed", columns
        if needs_info:
            return "paused", columns
        return ("passed" if all_pass else "failed"), columns
    # 第二轮只放宽审查结论，不放宽真实执行：执行未通过按未通过处理，不算分歧。
    if not execution_passed:
        return "failed", columns
    if needs_info:
        # 还有人要补材料，就暂停补完再判；全部 FAIL 才退役。
        return "paused", columns
    if any_fail:
        return ("disagreement" if any_pass else "failed"), columns
    return ("passed" if all_pass else "failed"), columns


def independence_key(source_ref, model=None):
    """两个审查员是否独立：看“入口程序＋模型”。

    同一入口用 A 模型、B 模型算两个；同一入口同一模型，改名字也只算一个。
    模型名由用户手填，比较前统一大小写并把连续空白压成一个空格，
    避免“Gemini Flash”和“gemini  flash”被当成两个来源凑独立。
    真实模型名的别名（不同写法指同一模型）无法判断，不在本函数处理。
    旧的四字段来源没有模型资料，仍只按 source_ref 判断，行为不变。
    CLI 节点模型留空表示跟随 CLI 当前配置，同样只按 source_ref 判断。
    """
    if not isinstance(source_ref, str) or not source_ref.strip():
        raise ValueError("缺少实际来源标识，无法核对独立性")
    if model is None or (isinstance(model, str) and not model.strip()):
        return source_ref
    if not isinstance(model, str):
        raise ValueError("模型标识格式异常，无法核对独立性")
    normalized_model = " ".join(model.split()).casefold()
    return source_ref.strip() + "|model:" + normalized_model


def _is_dsh_identity(identity, kind=None):
    """cli:dsh / path/.../dsh / bare dsh — any DSH-homologous entry ignores model."""
    if kind == "dsh":
        return True
    if not isinstance(identity, str) or not identity.strip():
        return False
    token = identity.strip()
    lowered = token.casefold()
    if lowered in {"dsh", "cli:dsh"} or lowered.startswith("cli:dsh"):
        return True
    try:
        return Path(token).name.casefold() == "dsh"
    except (TypeError, ValueError, OSError):
        return False


def review_independence(identity, model=None, *, kind=None):
    """Pool/external independence using normalized entry_program semantics.

    WorkBuddy/CodeBuddy already collapse to the same cli:codebuddy family at
    registration/entry_program. DSH ignores the model field by policy.
    """
    if _is_dsh_identity(identity, kind):
        model = None
    return independence_key(identity, model)


def _pool_item_independence(pool):
    identity = pool.get("entry_program") or pool.get("source_ref")
    kind = pool.get("agent_type") or pool.get("kind")
    if not kind and isinstance(identity, str) and identity.startswith("cli:"):
        kind = identity.split(":", 1)[1]
    return review_independence(identity, pool.get("model"), kind=kind)


def _run_failed(run, extract_trial_verdict):
    if not run or run.get("status") != "completed":
        return True
    return extract_trial_verdict(run.get("output") or "") == "FAIL"


def current_pool_review_participants(conn, task_id, *, allow_partial=False):
    """Current bound pool review seats after substitution.

    Returns:
      None  – no pool context (legacy local-agent task)
      []    – pool context but cannot form two independent current votes
      [a,b] – exactly two current seats; replaced failures stay in history
    allow_partial is only for checking a bound host-complemented review.
    """
    from .experience_workflow import extract_trial_verdict

    runs = {str(row["id"]): dict(row) for row in conn.execute(
        "SELECT * FROM agent_runs WHERE task_id=?", (task_id,))}
    current = []
    pool_context = False
    for event in conn.execute(
        "SELECT payload_json FROM events WHERE task_id=? AND event_type='agent.started' ORDER BY sequence",
        (task_id,),
    ):
        try:
            payload = json.loads(event["payload_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        execution = payload.get("execution")
        if not isinstance(execution, dict):
            continue
        pool = execution.get("pool_score")
        if pool is not None or execution.get("pool_source_ref"):
            pool_context = True
        # purpose=review binds the vote; node config may be review or both.
        if execution.get("purpose") != "review" or not isinstance(pool, dict):
            continue
        if pool.get("role") not in {None, "review", "both"}:
            continue
        run_id = payload.get("run_id")
        node_id = pool.get("node_id")
        source_ref = pool.get("source_ref")
        run = runs.get(run_id) if isinstance(run_id, str) else None
        if (run is None or not isinstance(node_id, str) or not node_id
                or run.get("agent_name") != node_id or payload.get("agent") != node_id
                or not isinstance(source_ref, str) or not source_ref.strip()
                or (pool.get("model") is not None and not isinstance(pool.get("model"), str))):
            continue
        try:
            independence = _pool_item_independence(pool)
        except ValueError:
            continue
        item = {
            "node_id": node_id,
            "run": run,
            "source_identity": (pool.get("entry_program") or source_ref, pool.get("model")),
            "independence": independence,
            "pool": pool,
        }
        replaced = False
        for index, seat in enumerate(current):
            if seat["node_id"] == node_id or seat["independence"] == independence:
                current[index] = item
                replaced = True
                break
        if replaced:
            continue
        if len(current) < 2:
            current.append(item)
            continue
        # Third (or later) independent node: replace one failed current seat.
        failed_index = next(
            (index for index, seat in enumerate(current)
             if _run_failed(seat["run"], extract_trial_verdict)),
            None,
        )
        if failed_index is not None:
            current[failed_index] = item

    if not pool_context:
        return None
    if allow_partial:
        return current
    if len(current) != 2 or len({item["independence"] for item in current}) != 2:
        return []
    return current


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


def pool_review_verdicts(conn, task_id):
    """Return the two bound pool votes, FAIL closed, or None for legacy local tasks."""
    from .experience_workflow import extract_trial_verdict

    participants = current_pool_review_participants(conn, task_id)
    if participants is None:
        return None
    if len(participants) != 2:
        return {"dsh:pool_invalid": "FAIL", "peer:pool_invalid": "FAIL"}

    participants = sorted(participants, key=lambda item: (
        item["source_identity"][0], item["source_identity"][1] or "", item["run"]["id"],
    ))
    verdicts = {}
    for lane, item in zip(("dsh_pool_review", "peer_pool_review"), participants):
        run = item["run"]
        verdicts[lane] = (
            extract_trial_verdict(run.get("output") or "") if run.get("status") == "completed" else "FAIL"
        )
    return verdicts


def _host_trial_verdicts(conn, task):
    """Read a settled host vote and its exact pool run; never invent a second vote."""
    from .experience_workflow import extract_trial_verdict
    from .pool_dispatch import NO_INDEPENDENT, review_vote_runs
    from .task_participants import execution_context

    event = conn.execute(
        "SELECT payload_json FROM events WHERE task_id=? AND event_type='host.receipt_accepted' ORDER BY sequence DESC LIMIT 1",
        (task["id"],),
    ).fetchone()
    if not event:
        return None
    accepted = json.loads(event[0])
    receipt = accepted.get("receipt") or {}
    provenance = json.loads(task.get("provenance_json") or "{}")
    context = execution_context(None, task, conn)
    host = context.get("host")
    run_ids = accepted.get("pool_run_ids")
    if (not host or host["id"] in context["excluded_ids"]
            or receipt.get("reviewer_id") != host["id"]
            or receipt.get("task_id") != task["id"]
            or receipt.get("snapshot_sha256") != task["execution_snapshot_hash"]
            or receipt.get("verdict") not in {"PASS", "FAIL", "NEEDS_INFO"}
            or checksum(receipt) != accepted.get("receipt_sha256")
            or provenance.get("kind") != "host_review"
            or provenance.get("host_id") != host["id"]
            or provenance.get("receipt_sha256") != accepted.get("receipt_sha256")
            or not isinstance(run_ids, list) or provenance.get("pool_run_ids") != run_ids):
        raise ValueError("宿主回执与冻结任务或结算来源不一致")
    if not run_ids:
        if accepted.get("user_chose_host") is not True or accepted.get("label") != NO_INDEPENDENT:
            raise ValueError("宿主回执缺少独立池票且未经用户选择兜底")
        return {"peer_host_review": receipt["verdict"]}, False
    participants = current_pool_review_participants(conn, task["id"], allow_partial=True) or []
    if len(run_ids) != 1 or len(participants) != 1:
        raise ValueError("宿主回执必须绑定恰好一份独立池票")
    participant = participants[0]
    run = participant["run"]
    output = run.get("output") or ""
    actual_digest = hashlib.sha256(output.encode()).hexdigest()
    digests = accepted.get("pool_run_digests") or {}
    expected = digests.get(run_ids[0]) if isinstance(digests, dict) else None
    votes = review_vote_runs(conn, task["id"])
    vote = votes.get(participant["node_id"]) if isinstance(votes, dict) else None
    vote_digest = hashlib.sha256((vote.get("output") or "").encode()).hexdigest() if vote else None
    if (run["id"] != run_ids[0] or run.get("status") != "completed"
            or participant["node_id"] in context["excluded_ids"]
            or participant["independence"] == independence_key(*host["independence"])
            or (expected is not None and expected != actual_digest)
            or not vote or vote.get("id") != run_ids[0] or vote_digest != actual_digest):
        raise ValueError("宿主回执绑定的池票不合格或来源不独立")
    return {"dsh_pool_review": extract_trial_verdict(output),
            "peer_host_review": receipt["verdict"]}, True


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
    host_review = _host_trial_verdicts(conn, task)
    independent_review = True
    verdicts = pool_review_verdicts(conn, task_id) if host_review is None else host_review[0]
    if host_review is not None:
        independent_review = host_review[1]
    if verdicts is None:
        # Only legacy local-agent tasks require the historical judge record.
        # Dynamic pool and host-complemented reviews prove their actual bound votes.
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
            "independent_review": independent_review,
            "reviews_passed": all(v == "PASS" for v in verdicts.values()), "trial": trial}


def binding(record):
    return {"attempt_id": record["id"], "round": record["round"], "task_id": record["task_id"],
            "plan_digest": record["plan_digest"], "execution_digest": record["execution_digest"]}


def ingress_evidence(conn, candidate, approved, record, execution):
    """Optional external reviews add requirements; they never replace execution.

    返回本轮外部回执的核对结果：
    {bound, receipts, verdicts(role->结论), all_pass, disagreement}。
    未绑定回执时 bound=False，其余字段为空。
    材料包必须自证一致。业务 FAIL / NEEDS_INFO 返回给 decide_round，
    不把第一轮 FAIL 伪装成基础设施暂停；仅绑定/完整性错误抛出。
    """
    unbound = {"bound": False, "receipts": {}, "verdicts": {}, "all_pass": None,
               "disagreement": False}
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    explicit = [record.get("dsh_receipt_id"), record.get("peer_receipt_id")]
    if "ri_requests" not in tables:
        if any(explicit):
            raise ValueError("回执实体不存在")
        return unbound
    expected = binding(record)
    requests = []
    for row in conn.execute("SELECT * FROM ri_requests WHERE candidate_id=? ORDER BY rowid DESC", (candidate["task_id"],)):
        common = json.loads(row["common"])
        if common.get("validation_binding") == expected:
            requests.append((dict(row), common))
    if not requests:
        if any(explicit):
            raise ValueError("回执未绑定当前轮次/任务/计划与执行")
        return unbound
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
    published = [_normalize_verdict((r.get("receipt") or {}).get("verdict"))
                 for r in payload.get("reviews") or [] if isinstance(r, dict)]
    # Current seats decide all_pass; historical prior_failures stay audit-only.
    current_all_pass = bool(published) and _worst_verdict(published) == "PASS"
    legacy_all_pass = current_all_pass and not payload.get("prior_failures")
    claimed = bool(payload.get("all_pass"))
    if (checksum(payload) != packet["digest"]
            or claimed not in {current_all_pass, legacy_all_pass}
            or payload.get("plan_revision") != request["revision"]):
        raise ValueError("当前材料包未全数通过或版本不一致")
    packet_all_pass = current_all_pass
    assignments = [dict(a) for a in conn.execute("SELECT * FROM ri_assignments WHERE request_id=? AND active=1", (request["id"],))]
    if len(assignments) != 2 or {a["role"] for a in assignments} != {"dsh", "peer"}:
        raise ValueError("缺少当前独立双审任务卡")
    ids, verdicts, sources = {}, {}, set()
    for assignment in assignments:
        receipt = conn.execute("SELECT * FROM ri_receipts WHERE assignment_id=? ORDER BY rowid DESC LIMIT 1", (assignment["id"],)).fetchone()
        if not receipt or receipt["status"] != "valid" or not receipt["attestation"]:
            raise ValueError("当前回执尚未认证或已失效")
        data, card = json.loads(receipt["data"]), json.loads(assignment["card"])
        frozen_card = dict(card)
        frozen_digest = frozen_card.pop("card_digest", None)
        if checksum(frozen_card) != frozen_digest or card.get("input_digest") != checksum(common):
            raise ValueError("审查任务卡或证据输入已变动")
        receipt_verdict = data.get("verdict")
        if checksum(data) != receipt["digest"] or receipt_verdict not in ("PASS", "FAIL", "NEEDS_INFO"):
            raise ValueError("当前回执未通过或摘要不一致")
        source = conn.execute("SELECT data FROM ri_sources WHERE id=?", (assignment["source_id"],)).fetchone()
        if not source:
            raise ValueError("审查来源未登记")
        source = json.loads(source[0])
        keys = ("review_request_id", "assignment_id", "reviewer_role", "plan_revision", "reviewer_id", "source_ref", "candidate_sha256", "input_digest", "card_digest")
        if (any(data.get(k) != card.get(k) for k in keys)
                or type(card.get("plan_revision")) is not int
                or not (1 <= card["plan_revision"] <= request["revision"])):
            raise ValueError("回执与当前任务卡/角色/版本不一致")
        if data.get("source_ref") != source["source_ref"] or data.get("reviewer_id") != assignment["source_id"]:
            raise ValueError("回执来源不一致")
        if data.get("started_at", 0) < execution["finished_at"]:
            raise ValueError("审查时间早于执行证据完成")
        plan = card.get("resource_plan") if isinstance(card.get("resource_plan"), dict) else {}
        identity = source["source_ref"]
        kind = plan.get("agent_type")
        if plan.get("interface") or plan.get("entry") or plan.get("entry_program"):
            from .pool_dispatch import entry_program, infer_agent_type
            identity = entry_program(plan) or identity
            kind = infer_agent_type(plan)
        sources.add(review_independence(identity, plan.get("model"), kind=kind))
        ids[assignment["role"]] = receipt["id"]
        verdicts[assignment["role"]] = receipt_verdict
    if len(sources) != 2 or len(set(ids.values())) != 2:
        raise ValueError("两份回执来源不独立")
    if any(explicit) and explicit != [ids["dsh"], ids["peer"]]:
        raise ValueError("轮次记录不是当前双审回执")
    packet_ids = {r["receipt"]["receipt_id"] for r in payload.get("reviews", [])}
    if packet_ids != set(ids.values()):
        raise ValueError("材料包不对应最新回执")
    result = {"bound": True, "receipts": ids, "verdicts": verdicts,
              "all_pass": packet_all_pass, "disagreement": False}
    if packet_all_pass:
        return result
    if int(record.get("round") or 0) == 2 and set(verdicts.values()) == {"PASS", "FAIL"}:
        result["disagreement"] = True
        return result
    # Business FAIL / NEEDS_INFO: hand to decide_round (consume FAIL rounds / pause NEEDS_INFO).
    if verdicts and set(verdicts.values()) <= {"PASS", "FAIL", "NEEDS_INFO"}:
        return result
    raise ValueError("当前回执未通过或摘要不一致")
