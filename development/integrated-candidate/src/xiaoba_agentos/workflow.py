"""Audited work contributions followed by a separate, frozen review task."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .receipts import ExecutionReceiptStore
from .task_context import freeze_context, render_context


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verified_snapshot(task):
    snapshot = json.loads(task["execution_snapshot_json"])
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if digest(canonical) != task["execution_snapshot_hash"]:
        raise ValueError("workflow execution snapshot integrity mismatch")
    return snapshot


def _roster(config, store=None, purpose="collab"):
    from .pool_dispatch import active_names
    return active_names(config, store, purpose=purpose)


def work_plan(config, assignments, context, store=None):
    names = _roster(config, store)
    if not names or not isinstance(assignments, dict) or sorted(assignments) != names:
        raise ValueError("assignments must name every enabled Agent exactly once")
    if any(not isinstance(value, str) or not value.strip() for value in assignments.values()):
        raise ValueError("every Agent needs a non-empty work assignment")
    for value in assignments.values():
        freeze_context(value)
    if not context.strip():
        raise ValueError("work requires frozen source context")
    return {"version": 1, "phase": "work", "agents": names, "assignments": assignments}


def contribution_records(config, store, task_id, names):
    task = store.get_task(task_id)
    snapshot = verified_snapshot(task)
    context = "\n\n".join(value for value in (
        render_context(snapshot.get("task_context")), snapshot.get("skill_context", ""),
        snapshot.get("knowlp_context", "")) if value)
    latest = store.latest_agent_runs(task_id)
    records = []
    for name in names:
        run = latest.get(name, {})
        if run.get("status") != "completed" or not str(run.get("output") or "").strip():
            raise ValueError("work contribution missing: " + name)
        receipt = ExecutionReceiptStore(config.runtime_dir / "receipts" / task_id / (str(run["id"]) + ".json"))
        if not receipt.is_success(str(run["id"])):
            raise ValueError("work contribution receipt missing: " + name)
        record = receipt.get(str(run["id"]))
        expected_command = agent_prompt(task["prompt"], snapshot["workflow"], name) + "\n" + context
        if (record.get("task_id") != task_id or record.get("agent") != name
                or record.get("tool_call_id") != str(run["id"])
                or record.get("command_hash") != digest(expected_command)):
            raise ValueError("work contribution receipt integrity mismatch: " + name)
        records.append({"agent": name, "run_id": run["id"], "output_sha256": digest(run["output"]),
                        "receipt_sha256": hashlib.sha256(receipt.path.read_bytes()).hexdigest()})
    return records


def review_plan(config, store, work_task_id, context, artifacts):
    task = store.get_task(work_task_id)
    if not task or task["status"] != "completed":
        raise ValueError("collaboration work must complete before review")
    snapshot = verified_snapshot(task)
    plan = snapshot.get("workflow", {})
    if plan.get("phase") != "work":
        raise ValueError("review requires a collaboration work task, not a prior review")
    workers = list(plan["agents"] or [])
    from .pool_dispatch import load_nodes, select_reviewers
    nodes = load_nodes(store)
    if nodes:
        decision = select_reviewers(nodes, exclude_ids=workers)
        names = [n['node_id'] for n in decision['reviewers']] if decision['route']=='run' else []
    else:
        names = _roster(config, store, purpose="review")
    if set(names) & set(workers):
        raise ValueError("同一任务不能既协作又审查")
    if not names:
        raise ValueError("没有可用的独立审查节点")
    if not context.strip() or digest(context) == snapshot["task_context"]["sha256"]:
        raise ValueError("review requires new frozen integration evidence")
    records = contribution_records(config, store, work_task_id, workers)
    bundle = json.loads(task["result"])
    if bundle.get("contributions") != records:
        raise ValueError("completed work contribution integrity mismatch")
    if not artifacts:
        raise ValueError("review requires at least one integrated artifact")
    manifest = []
    for value in sorted(set(artifacts)):
        path = Path(value).resolve(strict=True)
        manifest.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {"version": 1, "phase": "review", "agents": names, "work_task_id": work_task_id,
            "work_snapshot_sha256": task["execution_snapshot_hash"], "contributions": records,
            "artifacts": manifest}


def validate_plan(config, store, task, snapshot):
    plan = snapshot.get("workflow")
    if not plan:
        return
    verified_snapshot(task)
    if plan.get("version") != 1 or plan.get("phase") not in {"work", "review"}:
        raise ValueError("invalid collaboration phase")
    purpose = "collab" if plan["phase"] == "work" else "review"
    if plan["phase"] == "work" and plan.get("agents") != _roster(config, store, purpose=purpose):
        raise ValueError("collaboration Agent roster changed")
    if plan["phase"] == "work":
        work_plan(config, plan.get("assignments"), snapshot["task_context"]["text"], store=store)
    else:
        expected = review_plan(config, store, plan["work_task_id"], snapshot["task_context"]["text"],
                               [item["path"] for item in plan["artifacts"]])
        if expected != plan:
            raise ValueError("integration artifacts or work evidence changed; submit a new review")


def agent_prompt(prompt, plan, name):
    if plan.get("phase") == "work":
        return (prompt + "\n\n[协作工作阶段，不是独立审核]\n仅完成以下分工，提供具体补丁、依据与验证建议，"
                "由宿主集成；不得声称已修改文件或完成最终审查。\n" + plan["assignments"][name])
    if plan.get("phase") == "review":
        return prompt + "\n\n[集成后的独立审核]\n仅依据本次冻结集成证据检查缺陷与回归，不复用工作阶段输出作为审核结论。"
    return prompt
