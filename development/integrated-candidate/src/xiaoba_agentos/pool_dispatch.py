"""Pick review and collaboration nodes from the Agent pool.

xiaoba_review already imports this package. This module does not import
xiaoba_review at load time, so the worker can read pool rows without a cycle.
Real CLI execution is a lazy import at the call site (json_stdio_factory).

Config ``agents`` (including the old standby flag) stay loadable as historical
route notes. They are not a dispatch list unless every enabled agent is a
``mock`` fixture used by unit tests.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import AgentConfig
from .validation_evidence import independence_key

ROLE_REVIEW = "review"
ROLE_COLLABORATE = "collaborate"
ROLE_BOTH = "both"
_STORED_ROLE = {
    "dsh": ROLE_REVIEW,
    "peer": ROLE_REVIEW,
    "collaborator": ROLE_COLLABORATE,
    "review": ROLE_REVIEW,
    "collaborate": ROLE_COLLABORATE,
    "both": ROLE_BOTH,
}
LEGACY_AGENT_NAMES = frozenset({"agy_review", "dsh_review", "qoder_review"})
SHORTAGE = "当前开启并通过测试的独立审查节点不足 2 个，这次不派审查"
SAME_ENTRY = "同一入口程序和同一模型不能算两份独立审查，这次不派审查"
ASK_HOST = "Agent 池审查员全部不可用。可以改由宿主审查，并标明“无独立审查”；不会回退到任何预设后台。"
HOST_WORKED = "宿主这次干过活或是作者，不能补这一票；请另选已开启并通过测试的审查节点。"
NO_INDEPENDENT = "无独立审查"
NO_COLLAB = "没有协作节点，这次不排队"
AWAITING_HOST = "这次没有需要审查的经验，留给宿主完成。"
DEFAULT_NO_PROGRESS_SECONDS = 300
_CLI_TYPES = ("agy", "dsh", "qoder", "workbuddy", "codebuddy", "codex", "claude", "other")
_ENTRY_TYPES = {
    "agy": "agy", "antigravity": "agy", "dsh": "dsh", "qoder": "qoder",
    "qodercli": "qoder", "codebuddy": "workbuddy", "workbuddy": "workbuddy",
    "codex": "codex", "codex.js": "codex", "claude": "claude",
}


def mapped_role(kind: str) -> str:
    """Read-time role. Stored text is left unchanged until the user edits it."""
    try:
        return _STORED_ROLE[kind]
    except KeyError as exc:
        raise ValueError("未知节点角色") from exc


def infer_agent_type(node: Dict[str, Any]) -> str:
    """Match xiaoba_review.cli_identity.agent_type; keep a core copy to avoid a cycle."""
    try:
        from xiaoba_review.cli_identity import agent_type as _agent_type
        return str(_agent_type(node))
    except ImportError:
        pass
    if node.get("agent_type") in _CLI_TYPES:
        return str(node["agent_type"])
    path = Path(node.get("entry") or "")
    if path.name == "bin.js" and path.parts[-4:-1] == ("apps", "cli", "lib"):
        return "dsh"
    return _ENTRY_TYPES.get(path.name.casefold(), "other")


def source_family(kind: str) -> str:
    try:
        from xiaoba_review.cli_identity import source_family as _source_family
        return str(_source_family(kind))
    except ImportError:
        return "codebuddy" if kind == "workbuddy" else kind


def entry_program(node: Dict[str, Any]) -> str:
    if node.get("entry_program"):
        return str(node["entry_program"])
    interface = node.get("interface")
    entry = str(node.get("entry") or "")
    kind = infer_agent_type(node)
    if interface == "cli" and kind and kind != "other":
        return "cli:" + source_family(kind)
    if interface == "cli" and entry:
        path = Path(entry)
        try:
            return str(path.resolve()) if path.is_absolute() else entry
        except (OSError, RuntimeError):
            return entry
    return entry


def independence_pair(node: Dict[str, Any]) -> Tuple[str, str]:
    """入口程序＋模型，走 independence_key。DSH 忽略节点里的模型名。"""
    program = entry_program(node)
    kind = infer_agent_type(node)
    model = node.get("model")
    if node.get("interface") == "cli" and kind == "dsh":
        model = None
    if not program:
        program = str(node.get("source_ref") or node.get("node_id") or node.get("entry") or "unknown")
    key = independence_key(program, model)
    if "|model:" in key:
        identity, _, labeled = key.partition("|model:")
        return (identity, labeled)
    return (key, "")


def no_progress_seconds(node: Dict[str, Any]) -> int:
    raw = node.get("no_progress_seconds", DEFAULT_NO_PROGRESS_SECONDS)
    if type(raw) is not int or raw < 1:
        return DEFAULT_NO_PROGRESS_SECONDS
    return raw


def _loads_source(row: Any) -> Dict[str, Any]:
    data = row["data"] if hasattr(row, "keys") else row[0]
    source = json.loads(data)
    if not isinstance(source, dict):
        raise ValueError("Agent节点记录不是对象")
    return source


def load_sources(store: Any) -> List[Dict[str, Any]]:
    """Pool rows from the native database. Missing table means an empty pool."""
    try:
        read = store.read_only
    except AttributeError:
        return []
    with read() as conn:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ri_sources'"
        ).fetchone()
        if not found:
            return []
        rows = conn.execute("SELECT data FROM ri_sources ORDER BY rowid").fetchall()
    return [_loads_source(row) for row in rows]


def _entry_present(entry: Any) -> bool:
    if not entry:
        return False
    path = Path(str(entry))
    if not path.is_absolute():
        return True
    return path.is_file() and os.access(path, os.X_OK)


def node_readiness(source: Dict[str, Any], store: Any) -> Dict[str, Any]:
    """The page and Worker use the same spec, runner, evidence and event gate."""
    try:
        from xiaoba_review.node_runtime import readiness
        from xiaoba_review.resource_stdio import json_stdio_factory
        root = bound_workspace_root(store)
        with store.read_only() as db:
            return readiness(db, source, lambda src: json_stdio_factory(src, root))
    except (ImportError, OSError, ValueError, AttributeError, TypeError):
        return {"ready": False, "reason": "test_required"}


def node_view(source: Dict[str, Any], store: Any = None) -> Dict[str, Any]:
    node = source.get("node") if isinstance(source.get("node"), dict) else {}
    kind = str(source.get("kind") or "")
    role = mapped_role(kind) if kind in _STORED_ROLE else ""
    test = node.get("test") if isinstance(node.get("test"), dict) else {}
    enabled = bool(node.get("enabled")) and not bool(node.get("removed"))
    tested = test.get("status") == "passed"
    interface = node.get("interface")
    review_role = role in {ROLE_REVIEW, ROLE_BOTH} and interface != "tmux"
    collab_role = role in {ROLE_COLLABORATE, ROLE_BOTH}
    entry = node.get("entry")
    # Absolute entries must still exist and carry the same binding fields
    # readiness checks (spec/runner/evidence). Identity-style unit-test
    # entries such as "cli:codex" only need a passed test record.
    qualification = node_readiness(source, store) if Path(str(entry or "")).is_absolute() else {"ready": tested, "reason": "fixture"}
    bound = qualification["ready"]
    ready_review = bool(
        enabled and review_role and entry and _entry_present(entry) and bound
    )
    view = {
        "node_id": source.get("reviewer_id"),
        "kind": kind,
        "role": role,
        "name": node.get("name") or source.get("reviewer_id"),
        "entry": entry,
        "model": node.get("model"),
        "interface": interface,
        "agent_type": infer_agent_type(node),
        "source_ref": source.get("source_ref"),
        "entry_program": node.get("entry_program"),
        "enabled": enabled,
        "removed": bool(node.get("removed")),
        "test_passed": tested,
        "readiness": qualification,
        "ready_review": ready_review,
        "ready_collab": bool(enabled and bound and collab_role and interface != "tmux") or bool(
            enabled and collab_role and interface == "tmux"
        ),
        "no_progress_seconds": no_progress_seconds(node),
        "_source": source,
    }
    view["independence"] = independence_pair(view)
    return view


def load_nodes(store: Any) -> List[Dict[str, Any]]:
    return [node_view(source, store) for source in load_sources(store)]


def _distinct(nodes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    chosen: List[Dict[str, Any]] = []
    seen = set()
    for node in sorted(nodes, key=lambda item: str(item.get("node_id"))):
        key = tuple(node.get("independence") or ())
        if key in seen:
            continue
        seen.add(key)
        chosen.append(node)
    return chosen


def select_reviewers(
    nodes: Sequence[Dict[str, Any]],
    *,
    exclude_ids: Iterable[str] = (),
    host: Optional[Dict[str, Any]] = None,
    user_chose_host: bool = False,
) -> Dict[str, Any]:
    """Choose two independent reviewers, or say why this round is not dispatched.

    A node that collaborated, executed, or authored this task is excluded.
    The host fills a missing second vote only when this round's records show
    the host neither worked nor authored. When every pool reviewer is
    unavailable, dispatch waits for the user; a host fallback is marked
    无独立审查 and is never a silent return to a fixed backend name.
    """
    excluded = {str(item) for item in exclude_ids}
    review_nodes = [node for node in nodes if node.get("role") in {ROLE_REVIEW, ROLE_BOTH} and node.get("interface") != "tmux"]
    eligible = [
        node for node in review_nodes
        if node.get("ready_review") and str(node.get("node_id")) not in excluded
    ]
    distinct = _distinct(eligible)
    # Two registered nodes that share an entry and a model are not a pair.
    ready_ids = [node for node in review_nodes if node.get("ready_review")]
    if len(ready_ids) >= 2 and len(_distinct(ready_ids)) < 2 and len(distinct) < 2:
        return {"route": "blocked", "reviewers": distinct[:1], "host": False,
                "label": None, "message": SAME_ENTRY}
    if len(distinct) >= 2:
        return {"route": "run", "reviewers": distinct[:2], "host": False,
                "label": None, "message": ""}
    host_ok = bool(host) and not host.get("worked") and not host.get("author") and str(host.get("id") or "") not in excluded
    host_key = host.get("independence") if host else None
    if len(distinct) == 1 and host_ok and host_key not in {tuple(node["independence"]) for node in distinct}:
        return {"route": "run", "reviewers": distinct, "host": True, "label": None, "message": ""}
    if host and (host.get("worked") or host.get("author")) and len(distinct) < 2:
        return {"route": "ask_user", "reviewers": distinct, "host": False,
                "label": None, "message": HOST_WORKED}
    if user_chose_host and host_ok and not distinct:
        return {"route": "host_fallback", "reviewers": [], "host": True,
                "label": NO_INDEPENDENT, "message": ""}
    if not review_nodes:
        return {"route": "blocked", "reviewers": [], "host": False, "label": None, "message": SHORTAGE}
    return {"route": "ask_user", "reviewers": distinct, "host": False,
            "label": None, "message": ASK_HOST}


def collab_nodes(nodes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [node for node in nodes if node.get("ready_collab")]


def load_nodes_from_database(path: Any) -> List[Dict[str, Any]]:
    """Read the pool without importing the review package or the event store."""
    if not path or not Path(str(path)).is_file():
        return []
    import sqlite3
    from contextlib import contextmanager

    class _Reader:
        @contextmanager
        def read_only(self):
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

    return load_nodes(_Reader())


def active_names(config: Any, store: Any = None, purpose: str = "review") -> List[str]:
    """Names selected for this task. An empty pool is empty; mock fixtures stay mock."""
    nodes = load_nodes(store) if store is not None else load_nodes_from_database(
        getattr(config, "database_path", None)
    )
    if nodes:
        if purpose == "collab":
            return sorted(str(node["node_id"]) for node in collab_nodes(nodes))
        decision = select_reviewers(nodes)
        if decision["route"] == "run" and decision["reviewers"]:
            return sorted(str(node["node_id"]) for node in decision["reviewers"])
        return []
    mocks = _mock_agents(config)
    if mocks:
        return sorted(agent.name for agent in mocks)
    return []


def review_report(nodes: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    decision = select_reviewers(nodes)
    enough = decision["route"] == "run" and len(decision["reviewers"]) + int(bool(decision["host"])) >= 2
    return {
        "ok": enough,
        "enough": enough,
        "shortage": not enough,
        "message": "" if enough else (decision["message"] or SHORTAGE),
        "count": len(decision["reviewers"]),
        "route": decision["route"],
    }


def _mock_agents(config: Any) -> List[Any]:
    """Unit-test fixtures. The retired standby flag is not part of the roster."""
    enabled = [agent for agent in getattr(config, "agents", []) or [] if getattr(agent, "enabled", False)]
    if not enabled or not all(getattr(agent, "type", "") == "mock" for agent in enabled):
        return []
    return [agent for agent in enabled if not getattr(agent, "standby", False)]


def bound_workspace_root(store: Any) -> Optional[Path]:
    if store is None:
        return None
    with store.read_only() as db:
        row = db.execute("SELECT payload_json FROM events WHERE event_type='agent_pool.workspace_bound' ORDER BY rowid DESC LIMIT 1").fetchone()
    return Path(json.loads(row[0])["root"]) if row else None


def workspace_root(config: Any, store: Any = None) -> Path:
    # No new configuration field: run.py's existing host argument is bound in
    # the shared audit log, and all processes read that same binding.
    return bound_workspace_root(store) or Path(config.runtime_dir) / "agent-workspaces"


def _agent_from_node(config: Any, node: Dict[str, Any], store: Any = None) -> AgentConfig:
    source = node.get("_source") or {
        "reviewer_id": node["node_id"],
        "kind": node.get("kind") or "review",
        "source_ref": node.get("source_ref") or ("agent-entry:" + str(node["node_id"])),
        "node": {
            "name": node.get("name") or node["node_id"],
            "agent": node.get("name") or node["node_id"],
            "interface": node.get("interface") or "cli",
            "entry": node.get("entry"),
            "model": node.get("model"),
            "agent_type": node.get("agent_type"),
            "no_progress_seconds": node.get("no_progress_seconds") or DEFAULT_NO_PROGRESS_SECONDS,
        },
    }
    raw = {
        "name": node["node_id"],
        "type": "pool_cli",
        "model": node.get("model"),
        "entry": node.get("entry"),
        "driver": infer_agent_type(node),
        "no_progress_seconds": node.get("no_progress_seconds") or DEFAULT_NO_PROGRESS_SECONDS,
        "pool_node": True,
        "pool_source": source,
        "workspace_root": str(workspace_root(config, store)),
    }
    return AgentConfig(
        name=str(node["node_id"]),
        type="pool_cli",
        enabled=True,
        role=str(node.get("role") or ""),
        timeout_seconds=int(node.get("no_progress_seconds") or DEFAULT_NO_PROGRESS_SECONDS),
        app_config=config,
        raw=raw,
        standby=False,
    )


def task_exclude_ids(task: Optional[Dict[str, Any]]) -> List[str]:
    if not task:
        return []
    snapshot = snapshot_of(task)
    found: List[str] = []
    for key in ("executor_ids", "author_ids", "collaborator_ids", "executors", "authors", "collaborators"):
        for item in snapshot.get(key) or []:
            if item:
                found.append(str(item))
    for key in ("executor", "author"):
        if snapshot.get(key):
            found.append(str(snapshot[key]))
    return found


def task_host(task: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not task:
        return None
    snapshot = snapshot_of(task)
    host = snapshot.get("host")
    return host if isinstance(host, dict) else None


def worker_roster(config: Any, store: Any, task: Any = None) -> Dict[str, Any]:
    """Who the worker may call on this poll. Historical config agents are not it."""
    nodes = load_nodes(store)
    report = review_report(nodes)
    snapshot = snapshot_of(task) if task else {}
    if snapshot.get("workflow", {}).get("phase") == "work":
        if nodes:
            collabs = collab_nodes(nodes)
            if not collabs:
                return {"mode": "blocked", "agents": [], "message": NO_COLLAB, "report": report,
                        "nodes": nodes, "decision": {"route": "blocked", "message": NO_COLLAB}}
            return {
                "mode": "pool",
                "agents": [_agent_from_node(config, node, store) for node in collabs],
                "message": "",
                "report": report,
                "nodes": nodes,
                "host": False,
                "label": None,
            }
        mocks = _mock_agents(config)
        if mocks:
            return {"mode": "mock", "agents": mocks, "message": "", "report": report, "nodes": []}
        return {"mode": "blocked", "agents": [], "message": NO_COLLAB, "report": report,
                "nodes": nodes, "decision": {"route": "blocked", "message": NO_COLLAB}}
    if nodes:
        from .task_participants import execution_context
        execution = execution_context(store, task) if task else {}
        exclude = list(set(task_exclude_ids(task)) | set(execution.get("excluded_ids", [])))
        host = execution.get("host") or task_host(task)
        user_chose_host = bool(snapshot.get("user_chose_host"))
        decision = select_reviewers(nodes, exclude_ids=exclude, host=host, user_chose_host=user_chose_host)
        if decision["route"] == "run" and decision["reviewers"]:
            return {
                "mode": "pool",
                "agents": [_agent_from_node(config, node, store) for node in decision["reviewers"]],
                "message": "",
                "report": report,
                "nodes": nodes,
                "host": bool(decision.get("host")),
                "label": decision.get("label"),
                "decision": decision,
            }
        if decision["route"] == "host_fallback":
            return {"mode": "awaiting_host", "agents": [], "message": "",
                    "label": decision.get("label") or NO_INDEPENDENT,
                    "report": report, "nodes": nodes, "decision": decision, "host": True}
        if decision["route"] == "ask_user":
            retest = any(n.get("readiness", {}).get("reason") == "test_required" for n in nodes)
            return {"mode": "ask_user", "agents": [], "message": (("节点调用摘要已变化，请重测。" if retest else "") + (decision["message"] or ASK_HOST)),
                    "report": report, "nodes": nodes, "decision": decision}
        return {"mode": "blocked", "agents": [], "message": decision["message"] or report["message"],
                "report": report, "nodes": nodes, "decision": decision}
    mocks = _mock_agents(config)
    if mocks:
        return {"mode": "mock", "agents": mocks, "message": "", "report": report, "nodes": []}
    if any(getattr(agent, "type", "") == "mock" for agent in getattr(config, "agents", [])):
        return {"mode": "mock_disabled", "agents": [], "message": "", "report": report, "nodes": []}
    return {"mode": "blocked", "agents": [], "message": SHORTAGE, "report": report, "nodes": []}


def intake_blockers(store: Any, *, needs_review: bool = True, needs_collab: bool = False) -> List[str]:
    nodes = load_nodes(store)
    blockers: List[str] = []
    if needs_collab and not collab_nodes(nodes):
        blockers.append(NO_COLLAB)
    if needs_review:
        report = review_report(nodes)
        if not report["enough"]:
            blockers.append(report["message"] or SHORTAGE)
    return blockers


def runnable_tasks(store: Any) -> List[Dict[str, Any]]:
    import time
    with store.read_only() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status IN ('pending','retry') AND run_after<=? "
            "ORDER BY priority DESC, created_at ASC",
            (time.time(),),
        ).fetchall()
    return [dict(row) for row in rows]


def peek_runnable(store: Any) -> Optional[Dict[str, Any]]:
    return next(iter(runnable_tasks(store)), None)


def gate_node(node: Dict[str, Any]) -> Dict[str, Any]:
    return {key: node[key] for key in ("node_id", "enabled", "test_passed", "independence") if key in node}


def snapshot_of(task: Dict[str, Any]) -> Dict[str, Any]:
    try:
        value = json.loads(task.get("execution_snapshot_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def needs_review(task: Dict[str, Any]) -> bool:
    snapshot = snapshot_of(task)
    if snapshot.get("needs_review") is False:
        return False
    if snapshot.get("workflow", {}).get("phase") == "work" and snapshot.get("needs_review") is not True:
        return False
    return True


def legacy_names(names: Iterable[str]) -> bool:
    return bool(set(names) & LEGACY_AGENT_NAMES)


def _bound(item: Dict[str, Any], evidence: Dict[str, Any]) -> bool:
    prompt = evidence.get("frozen_prompt_sha256")
    manifest = evidence.get("source_manifest_sha256")
    if not prompt or not manifest:
        return False
    return item.get("prompt_sha256") == prompt and item.get("manifest_sha256") == manifest


def release_acceptance(evidence: Dict[str, Any], pool_nodes: Sequence[Dict[str, Any]] = ()) -> Tuple[bool, str]:
    """At least two passing conclusions, one of them a live pool node.

    The host may be one vote only when execution records show that host did
    not write or run this release. Two host votes, a single vote, an old
    fixed-name pair, and a standby substitution are refused. Conclusions must
    share one frozen prompt and one source manifest.
    """
    if not isinstance(evidence, dict):
        return False, "验收证据不是对象"
    if evidence.get("standby_substituted") or evidence.get("successful_agents"):
        names = evidence.get("successful_agents") or []
        if legacy_names(names) or evidence.get("standby_substituted"):
            return False, "验收不再接受固定后台名字或备用补位"
        if names and not evidence.get("conclusions"):
            return False, "验收不再接受固定后台名字"
    conclusions = evidence.get("conclusions")
    if not isinstance(conclusions, list) or len(conclusions) < 2:
        return False, "合格结论不足 2 份"
    passed = [item for item in conclusions if isinstance(item, dict) and item.get("passed") is True]
    if len(passed) < 2:
        return False, "合格结论不足 2 份"
    if any(not _bound(item, evidence) for item in passed):
        return False, "回执没有绑定同一份冻结题面和源码清单"
    keys = []
    pool_votes = 0
    host_votes = 0
    by_id = {str(node.get("node_id")): node for node in pool_nodes}
    raw_execution = evidence.get("execution")
    if not isinstance(raw_execution, dict):
        if any(item.get("source") == "host" for item in passed):
            return False, "execution 记录缺失，拒绝宿主票"
        execution = {}
    else:
        execution = raw_execution
    authors = {str(item) for item in execution.get("author_ids") or []}
    executors = {str(item) for item in execution.get("executor_ids") or []}
    for item in passed:
        if item.get("source") == "host":
            host_votes += 1
            host_id = str(item.get("host_id") or "")
            if "checked_idle_ids" not in execution:
                return False, "execution 记录缺失，拒绝宿主票"
            if host_id in authors or host_id in executors:
                return False, "宿主是作者或执行者，不能计票"
            if host_id not in {str(v) for v in execution.get("checked_idle_ids") or []}:
                return False, "宿主没干活必须对照任务执行记录，不能只靠自述"
            keys.append(("host:" + host_id, str(item.get("model") or "")))
            continue
        node_id = str(item.get("node_id") or "")
        if node_id in LEGACY_AGENT_NAMES:
            return False, "验收不再接受固定后台名字"
        node = by_id.get(node_id)
        if node is None or not node.get("enabled") or not node.get("test_passed"):
            return False, "池节点未开启或没有测试通过"
        if node_id in authors or node_id in executors:
            return False, "干过这次发布的节点不能计票"
        pool_votes += 1
        keys.append(tuple(node.get("independence") or independence_pair(node)))
    if host_votes > 1 or pool_votes < 1:
        return False, "至少 1 份结论来自池节点，不能两票都是宿主"
    if len(set(keys)) < len(keys):
        return False, "结论的入口程序和模型不是彼此独立"
    return True, ""
