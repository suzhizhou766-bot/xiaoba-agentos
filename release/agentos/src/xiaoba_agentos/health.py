from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from .config import AppConfig
from .gateway import local_cli_readiness
from .knowledge import KnowledgeCatalog
from .memory import MemoryBridge
from .release import release_binding_status
from .retained import collect as retained_check
from .skills import SkillRegistry
from .store import EventStore


def model_paths(agents: list[Any]) -> Dict[str, list[str]]:
    """Describe each enabled Agent's real network and credential path."""
    return {
        agent.name: (
            ["agy_cli", "antigravity_local_account", "antigravity_service"]
            if agent.type == "local_cli" and agent.raw.get("driver") == "agy"
            else
            ["dsh_cli", "dsh_local_settings", "provider_endpoint"]
            if agent.type == "local_cli" and agent.raw.get("driver") == "dsh"
            else []
        )
        for agent in agents
    }


_ENTRYPOINT_DEFAULT_CONFIG = re.compile(r'^DEFAULT_CONFIG = Path\("(?P<path>[^"\n]+)"\)$', re.MULTILINE)
_ENTRYPOINT_NAMES = ("xiaoba", "verify-release")
_ENTRYPOINT_ROOTS = (".agents", ".codex")
_SERVICE_LABELS = {
    "worker_service": "com.xiaoba.agentos",
    "review_center_service": "com.xiaoba.review-center",
}
# Written by the review backend while it runs (the monitor centre starts it on demand).
_REVIEW_BACKEND_STATE = ("review-center", "backend-state.json")


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _review_backend_config(config: AppConfig) -> Optional[Path]:
    """Config path of the review backend that is running right now, if any.

    The backend is no longer a permanent LaunchAgent: the monitor centre starts it
    on demand and it records the config it actually loaded. A missing, unreadable
    or stale record (its process is gone) means no backend is running, which is a
    normal state and not a consistency problem.
    """
    state = config.runtime_dir.joinpath(*_REVIEW_BACKEND_STATE)
    try:
        record = json.loads(state.read_text(encoding="utf-8"))
        pid = int(record["pid"])
        raw_path = record["config_path"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if pid <= 0 or not isinstance(raw_path, str) or not raw_path.strip():
        return None
    path = Path(raw_path)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except OSError:
        pass  # exists but not signalable from here (e.g. sandbox): still a live consumer
    return path


def config_consistency(
    config: AppConfig,
    release_binding: Optional[Dict[str, Any]] = None,
    *,
    home: Optional[Path] = None,
) -> Dict[str, Any]:
    """Check that the known consumers of this project run on one configuration.

    Scope, exactly: this session's config; the entrypoints installed under
    ~/.agents and ~/.codex (skills/xiaoba-agentos/scripts/{xiaoba,
    verify-release}); the XIAOBA_CONFIG of the worker and review-center
    LaunchAgents; and the config recorded by a review backend that is running
    on demand under the monitor centre. Installed entrypoints are compared with the release copy
    because the source manifest does not cover them. Copies installed anywhere
    else are not seen, so this is not a guarantee about every entrypoint.
    """
    home = home or Path.home()
    project = config.project_dir
    problems: list = []
    warnings: list = []
    consumers: Dict[str, Dict[str, Any]] = {}

    def register(name: str, path: Path) -> None:
        if not _inside(path, project):
            return  # belongs to another installation
        entry: Dict[str, Any] = {"path": str(path)}
        try:
            entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            entry["sha256"] = None
            problems.append(f"{name}: 配置文件不可读：{path}")
        consumers[name] = entry

    register("session", config.path)

    release_dir = (release_binding or {}).get("release_dir")
    reference_root = (
        Path(str(release_dir)) / "agentos" if release_dir else project
    ) / ".agents" / "skills" / "xiaoba-agentos" / "scripts"
    entrypoints: Dict[str, Any] = {}
    for install_root, name in (
        (root, name) for root in _ENTRYPOINT_ROOTS for name in _ENTRYPOINT_NAMES
    ):
        installed = home / install_root / "skills" / "xiaoba-agentos" / "scripts" / name
        key = f"{install_root}/{name}"
        if not installed.is_file():
            continue
        try:
            text = installed.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            problems.append(f"已安装入口不可读：{installed}")
            continue
        match = _ENTRYPOINT_DEFAULT_CONFIG.search(text)
        if not match:
            # Cannot tell which installation this is; never skip it silently.
            entrypoints[key] = {"installed": str(installed), "ok": False}
            problems.append(f"已安装入口的默认配置行无法解析：{installed}")
            continue
        if not _inside(Path(match.group("path")), project):
            continue  # belongs to another installation
        if name == "xiaoba":
            register(f"installed_entrypoint:{install_root}", Path(match.group("path")))
        reference = reference_root / name
        state: Dict[str, Any] = {"installed": str(installed), "reference": str(reference)}
        try:
            expected = reference.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            state["ok"] = False
            problems.append(f"入口参照文件不可读：{reference}")
        else:
            normalize = lambda value: _ENTRYPOINT_DEFAULT_CONFIG.sub("DEFAULT_CONFIG = <config>", value)
            state["ok"] = normalize(text) == normalize(expected)
            if not state["ok"]:
                problems.append(f"已安装入口与发布入口不一致（默认配置路径以外的差异）：{installed}")
        entrypoints[key] = state

    for name, label in _SERVICE_LABELS.items():
        plist = home / "Library" / "LaunchAgents" / f"{label}.plist"
        if not plist.is_file():
            continue
        try:
            with plist.open("rb") as handle:
                environment = plistlib.load(handle).get("EnvironmentVariables") or {}
        except (OSError, ValueError, plistlib.InvalidFileException):
            warnings.append(f"{name}: LaunchAgent 不可解析：{plist}")
            continue
        value = environment.get("XIAOBA_CONFIG")
        if value:
            register(name, Path(str(value)))

    backend_config = _review_backend_config(config)
    if backend_config is not None:
        register("review_backend", backend_config)

    hashes = {entry["sha256"] for entry in consumers.values() if entry["sha256"]}
    if len(hashes) > 1:
        problems.append(
            "各入口使用的配置内容不一致："
            + "；".join(f"{name}={entry['path']}" for name, entry in sorted(consumers.items()))
        )
    elif len({entry["path"] for entry in consumers.values()}) > 1:
        warnings.append(
            "各入口使用的配置路径不同，当前内容相同但后续修改会分叉："
            + "；".join(f"{name}={entry['path']}" for name, entry in sorted(consumers.items()))
        )
    return {
        "ok": not problems,
        "consumers": consumers,
        "installed_entrypoints": entrypoints,
        "problems": problems,
        "warnings": warnings,
    }


def execution_blockers(config: AppConfig, store: EventStore) -> list:
    """Why a task submitted right now would sit in the queue unexecuted.

    Mirrors the gates the worker applies before leasing a task, without side
    effects (nothing is started or repaired) and without remote inference.
    """
    enabled = [agent for agent in config.agents if agent.enabled]
    if not enabled:
        return ["没有启用任何 Agent"]
    blockers: list = []
    try:
        status = local_cli_readiness(config, enabled)
    except Exception as exc:  # noqa: BLE001 - a broken gate is itself a blocker
        blockers.append(f"{type(exc).__name__}: {exc}")
    else:
        if not status.get("ok", False):
            blockers.extend(str(item) for item in status.get("problems", []) or ["readiness check failed"])
    names = [agent.name for agent in enabled]
    rows = {
        row["agent_name"]: row
        for row in store.agent_health(
            enabled_names=names,
            max_age_seconds=float(config.worker.get("health_max_age_seconds", 86400)),
        )
    }
    if rows or any(agent.type == "local_cli" for agent in enabled):
        for agent in enabled:
            status = rows.get(agent.name, {}).get("status", "unprobed")
            if status != "healthy":
                blockers.append(f"{agent.name}: 实际推理健康状态为 {status}，需要先运行 session-start")
    return blockers


def declared_automatic_actions(config: AppConfig) -> list:
    """Recovery actions the loaded config still declares; none of them run."""
    declared = config.raw.get("recovery", {}).get("automatic_actions", []) or []
    return sorted({str(item) for item in declared})


def doctor(config: AppConfig, store: EventStore, entity_root: Optional[Path] = None) -> Dict[str, Any]:
    integrations = config.integrations
    enabled = [agent for agent in config.agents if agent.enabled]
    required_drivers = {str(agent.raw.get("driver", "")) for agent in enabled}
    paths = {
        "project_dir": config.project_dir.exists(),
        "obsidian_vault": Path(config.memory["obsidian_vault"]).exists(),
        "knowlp_python": Path(config.memory["knowlp_python"]).is_file(),
        "knowlp_module": Path(config.memory["knowlp_module"]).is_file(),
        "node_command": Path(integrations["node_command"]).is_file(),
        "agy_command": "agy" not in required_drivers or Path(integrations.get("agy_command", "")).exists(),
        "dsh_command": "dsh" not in required_drivers or Path(integrations.get("dsh_command", "")).exists(),
    }
    credentials = {
        agent.name: (
            "managed_by_antigravity_local_account"
            if agent.type == "local_cli" and agent.raw.get("driver") == "agy"
            else "managed_by_dsh_local_settings"
            if agent.type == "local_cli" and agent.raw.get("driver") == "dsh"
            else "unknown"
        )
        for agent in config.agents
        if agent.type != "mock"
    }
    local_routes = local_cli_readiness(config, enabled)
    memory_bridge = MemoryBridge(config, store)
    memory_probe = memory_bridge.probe()
    try:
        memory_lifecycle = memory_bridge.audit_lifecycle(store.memory_lifecycle_rows())
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        memory_lifecycle = {"ok": False, "checked": 0, "problems": [str(exc)]}
    skill_registry = SkillRegistry(config, entity_root=entity_root).verify()
    knowledge_catalog = KnowledgeCatalog(config).verify()
    # 受限方案丙：每一次撤销都可能把被认领的原对象留在私有名字下。自检必须
    # 枚举它们（并覆盖记录缺失/失配、对象缺失、不可读等异常），绝不能因为
    # "内存里没有记录" 或 "换个进程/重启过" 就静默当作没有。
    try:
        retained_objects = retained_check(config)
    except (OSError, RuntimeError, ValueError) as exc:
        retained_objects = {
            "ok": False,
            "enumeration_ok": False,
            "retained_count": 0,
            "object_count": 0,
            "pending_count": 0,
            "resolved_count": 0,
            "anomaly_count": 0,
            "manual_action_required": True,
            "reconciliation_required": True,
            "objects": [],
            "anomalies": [],
            "problems": [f"the retained-object self-check could not run: {exc}"],
        }
    database_schema = store.schema_status()
    release_binding = release_binding_status(config)
    config_check = config_consistency(config, release_binding)
    try:
        event_chain = store.verify_event_chain()
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        event_chain = {"ok": False, "reason": str(exc)}
    warnings = list(config_check["warnings"])
    resolved_model_paths = model_paths(enabled)
    checks = {
        "paths": paths,
        "credentials": credentials,
        "event_chain": event_chain,
        "database_schema": database_schema,
        "release_binding": release_binding,
        "config_consistency": config_check,
        "local_cli_routes": local_routes,
        "memory_probe": memory_probe,
        "memory_lifecycle": memory_lifecycle,
        "skill_registry": skill_registry,
        "knowledge_catalog": knowledge_catalog,
        "retained_objects": retained_objects,
        # The pending retained objects are a reminder for the user, not a system
        # fault: they must be clearly visible here (count, list command, and what
        # to run) without turning the whole deployment into "not ok".
        "retained_manual_review": {
            "required": bool(retained_objects.get("manual_action_required")),
            "pending_count": int(retained_objects.get("pending_count") or 0),
            "resolved_count": int(retained_objects.get("resolved_count") or 0),
            "list_command": "memory-retained-list",
            "resolve_command": "memory-retained-resolve <retained_path> --trash|--confirm-deleted",
            "enumeration_ok": bool(retained_objects.get("enumeration_ok", False)),
            "note": (
                "有文件待人工核对：请亲自查看 memory-retained-list 并逐个处理；"
                "待核对保留物不阻断接单、切换或发布（用户裁定），但保留物模块自身的"
                "枚举错误仍然在这里如实上报"
            ),
        },
        "warnings": warnings,
        "enabled_agents": [a.name for a in enabled],
        "minimum_successful_agents": int(config.worker["minimum_successful_agents"]),
        "model_path": (
            ["mixed_agent_routes"]
            if len({tuple(path) for path in resolved_model_paths.values()}) > 1
            else next(iter(resolved_model_paths.values()), [])
        ),
        "model_paths": resolved_model_paths,
        "memory_publish_enabled": bool(config.memory.get("publish_enabled")),
        "intake_routing": {
            "enabled": True,
            "policy_version": "v1.3",
            "routes": ["direct", "agentos_multi", "review", "ask_user"],
            "stores_full_direct_message": False,
        },
        "memory_review": {
            "delivery_review_required": True,
            "final_review_required": True,
            "index_verification_required": True,
            "retire_after_failed_reuses": int(config.memory.get("retire_after_failed_reuses", 2)),
            "bulk_publish_allowed": False,
        },
        "recovery": {
            "live_inference_tested": False,
            "requires_session_start_probe": bool(
                config.raw.get("recovery", {}).get("probe_on_session_start", True)
            ),
            "agent_health_last_recorded": store.agent_health(
                enabled_names=[agent.name for agent in enabled],
                max_age_seconds=float(config.worker.get("health_max_age_seconds", 86400)),
            ),
            "repair_jobs": store.repair_jobs(10),
            "pending_notifications": store.pending_notifications(acknowledge=False),
            "peer_auto_repair": "retired",
            # What this release does on its own is nothing, so the effective list
            # is empty. Actions a config still declares are drift and stay
            # visible rather than being reported as though they were in force.
            "automatic_actions": [],
            "declared_but_not_executed": declared_automatic_actions(config),
            "unfreeze_requires": "explicit task-resume with freeze hash",
        },
    }
    if checks["recovery"]["declared_but_not_executed"]:
        checks["warnings"].append(
            "recovery.automatic_actions 仍声明自动动作，但同伴自动修复已退役，这些动作不会执行："
            + ", ".join(checks["recovery"]["declared_but_not_executed"])
        )
    enough_agents = len(enabled) >= checks["minimum_successful_agents"] > 0
    checks["ok"] = (
        all(paths.values())
        and release_binding.get("ok", False)
        and config_check.get("ok", False)
        and database_schema.get("ok", False)
        and event_chain.get("ok", False)
        and local_routes.get("ok", False)
        and memory_probe.get("ok", False)
        and memory_lifecycle.get("ok", False)
        and skill_registry.get("ok", False)
        and knowledge_catalog.get("ok", False)
        # Retained original objects are deliberately *not* part of this gate.  The
        # user ruled that a pending manual review must not block intake, switching
        # or publishing: it stays loudly visible in checks["retained_objects"] and
        # checks["retained_manual_review"], and it is still reported by its own
        # check, but it never turns the whole deployment red.  A real fault in the
        # retention module itself is reported the same two ways (its own check is
        # not ok, the enumeration is not ok) instead of masquerading as ordinary
        # pending work.
    )
    checks["ready_for_external_tasks"] = checks["ok"] and enough_agents
    return checks
