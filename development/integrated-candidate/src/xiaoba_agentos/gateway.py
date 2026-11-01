from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable

from .config import AGY_RETIRED_SELECTIONS, AppConfig, agy_configured_model, qoder_settings_default


class GatewayUnavailable(RuntimeError):
    pass


_ROUTE_DIAGNOSTIC_LIMIT = 400


def redact_credentials(value: str) -> str:
    """Mask credentials in CLI diagnostics, including JSON-shaped lines."""
    # Bearer first: otherwise the key rule below eats "Bearer" and leaves the
    # token itself behind ("Authorization: [REDACTED] <token>").
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    text = re.sub(
        r"(?i)(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
        r"client[_-]?secret|password|token|cookie)([\"']?\s*[:=]\s*)"
        r"(?![\"']?Bearer\b)(?:(?:basic|digest)\s+)?[^\s,;]+",
        r"\1\2[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    return re.sub(r"\b(?:sk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{12,}\b", "[REDACTED]", text)


def _redact_diagnostic(value: object, limit: int = _ROUTE_DIAGNOSTIC_LIMIT) -> str:
    """Keep a bounded, credential-free excerpt of a CLI diagnostic.

    卡 7 / 卡 16: the Desktop route reader threw its stderr away, so a broken
    Desktop profile only produced a generic "读取失败" and the reader's own
    sentinel line was lost. The sentinel is kept; anything credential-shaped,
    and everything beyond the last ``limit`` characters, is not.
    """
    return redact_credentials(" ".join(str(value or "").split()))[-limit:]


# A model id, never something shaped like a CLI flag ("--tools", "-c").
_QODER_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
_QODER_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def read_qodercli_selection(settings_path: Any) -> Dict[str, Any]:
    """The model the user last selected in qodercli, read from its own settings.

    Read-only; only `model.name` and that model's reasoning effort are taken.
    Anything missing, malformed or not a plain model id stops the call rather
    than letting the CLI fall back to some default.
    """
    path = Path(str(settings_path)).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
        raise ValueError("qodercli 设置文件不存在、是链接或过大")
    raw = json.loads(path.read_text(encoding="utf-8"))
    model_block = raw.get("model") if isinstance(raw, dict) else None
    name = model_block.get("name") if isinstance(model_block, dict) else None
    if not isinstance(name, str) or not _QODER_MODEL.fullmatch(name):
        raise ValueError("qodercli 设置里没有明确的模型名")
    effort = None
    preferences = model_block.get("preferences")
    if isinstance(preferences, dict) and isinstance(preferences.get(name), dict):
        reasoning = preferences[name].get("reasoning")
        if isinstance(reasoning, dict) and reasoning.get("effort") is not None:
            effort = reasoning.get("effort")
            if effort not in _QODER_EFFORTS:
                raise ValueError("qodercli 设置里的思考强度不支持")
    return {"model": name, "reasoning_effort": effort, "context_window": None,
            "model_source": "qodercli_settings", "settings_path": str(path)}


def local_cli_route(config: AppConfig, route_name: str, agent_name: str) -> Dict[str, Any]:
    """Resolve a local-account or local-harness CLI route without consulting CC Switch."""
    route = config.cli_routes.get(route_name)
    if not isinstance(route, dict):
        raise GatewayUnavailable(f"本地 CLI 路由未登记：{route_name}")
    driver = str(route.get("driver", ""))
    route_selection = str(route.get("selection", ""))
    if driver == "agy":
        # User 2026-11-01: the model written in the config is the model in use.
        # The agy CLI settings file and the Antigravity desktop are not read, so
        # a route still written the old way is refused with the migration
        # spelled out instead of quietly falling back to another model source.
        if route_selection in AGY_RETIRED_SELECTIONS:
            raise GatewayUnavailable(
                f"AGY 路由 {route_name} 仍是旧写法（selection={route_selection}）："
                f"{AGY_RETIRED_SELECTIONS[route_selection]}；请改成 selection=configured_model 并写明 model"
            )
        if route_selection != "configured_model":
            raise GatewayUnavailable(f"本地 CLI 路由选择策略无效：{route_name}")
        credential_desc = "managed_by_antigravity_local_account"
        selection = "configured_model"
    elif driver == "dsh":
        if route_selection != "local_harness":
            raise GatewayUnavailable(f"本地 CLI 路由选择策略无效：{route_name}")
        credential_desc = "managed_by_dsh_desktop_settings"
        selection = "local_harness"
    elif driver == "qoder":
        if route_selection != "qodercli_settings":
            raise GatewayUnavailable(f"本地 CLI 路由选择策略无效：{route_name}")
        credential_desc = "managed_by_qodercli_login"
        selection = "qodercli_settings"
    else:
        raise GatewayUnavailable(f"本地 CLI 路由驱动不支持：{route_name} ({driver})")

    if driver == "agy":
        try:
            model = agy_configured_model(route, route_name)
        except ValueError as exc:
            raise GatewayUnavailable(str(exc)) from exc
        return {"provider": "Antigravity", "model": model, "model_source": "node_configuration",
                "route": route_name, "driver": driver, "selection": selection,
                "credential": credential_desc, "global_switch_required": False}

    if driver == "dsh" and route.get("desktop_config") and "web_config" not in route:
        try:
            result = subprocess.run([
                str(config.integrations["node_command"]),
                str(config.project_dir / "scripts" / "dsh_desktop_route.cjs"),
                str(config.integrations["dsh_command"]), str(route["desktop_config"]),
            ], capture_output=True, text=True, timeout=10, check=False)
            if result.returncode:
                # 卡 16: keep the reader's own diagnostic, minus credentials.
                raise ValueError(
                    f"Desktop 路由脚本退出码 {result.returncode}；"
                    f"stderr：{_redact_diagnostic(result.stderr) or '（空）'}"
                )
            metadata = json.loads(result.stdout)
            if (not isinstance(metadata, dict)
                    or metadata.get("model_source") != "dsh_desktop"
                    or any(not isinstance(metadata.get(key), str) or not metadata[key].strip()
                           for key in ("provider", "provider_id", "model"))):
                raise ValueError("Desktop 路由元数据无效：provider/provider_id/model 不完整或来源不符")
            public_metadata = {key: metadata[key] for key in ("provider", "provider_id", "model", "model_source")}
            return {**public_metadata, "route": route_name, "driver": driver,
                    "selection": selection, "credential": credential_desc,
                    "global_switch_required": False, "desktop_config": str(route["desktop_config"])}
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            detail = _redact_diagnostic(exc)
            message = "DSH Desktop 路由读取失败，停止调用"
            raise GatewayUnavailable(f"{message}：{detail}" if detail else message) from exc

    if driver == "qoder":
        # User 2026-10-20: Qoder uses the model the user last chose in qodercli
        # itself (its own settings file), not the desktop app's selection.
        try:
            metadata = read_qodercli_selection(route.get("settings_path") or qoder_settings_default())
        except (OSError, KeyError, ValueError) as exc:
            raise GatewayUnavailable("qodercli 设置里没有可用的选定模型，停止调用；请在 qodercli 里选好模型：" + str(exc)) from exc
        return {**metadata, "provider": "qoder", "route": route_name, "driver": driver,
                "selection": selection, "credential": credential_desc,
                "global_switch_required": False}

    raise GatewayUnavailable("本地后台必须绑定自身界面配置；AgentOS 不选择静态模型")


def local_cli_readiness(config: AppConfig, agents: Iterable[Any]) -> Dict[str, Any]:
    """Check local CLI wiring without spending a remote inference request."""
    selected = [agent for agent in agents if agent.type == "local_cli"]
    if not selected:
        return {"ok": True, "checks": [], "problems": []}

    agy_command = str(config.integrations.get("agy_command", "")).strip()
    agy_executable = Path(agy_command).expanduser() if agy_command else None
    if agy_executable is not None and agy_executable.is_absolute():
        agy_ok = agy_executable.is_file() and os.access(agy_executable, os.X_OK)
        resolved_agy = str(agy_executable)
    else:
        resolved = shutil.which(agy_command) if agy_command else None
        agy_ok = bool(resolved)
        resolved_agy = resolved or agy_command

    node_command = str(config.integrations.get("node_command", "")).strip()
    node_executable = Path(node_command).expanduser() if node_command else None
    if node_executable is not None and node_executable.is_absolute():
        node_ok = node_executable.is_file() and os.access(node_executable, os.X_OK)
        resolved_node = str(node_executable)
    else:
        resolved = shutil.which(node_command) if node_command else None
        node_ok = bool(resolved)
        resolved_node = resolved or node_command

    dsh_command = str(config.integrations.get("dsh_command", "")).strip()
    dsh_file = Path(dsh_command).expanduser() if dsh_command else None
    dsh_ok = bool(dsh_file and dsh_file.is_file())
    resolved_dsh = str(dsh_file) if dsh_file else dsh_command

    qoder_command = str(config.integrations.get("qoder_command", "")).strip()
    qoder_executable = Path(qoder_command).expanduser() if qoder_command else None
    if qoder_executable is not None and qoder_executable.is_absolute():
        qoder_ok = qoder_executable.is_file() and os.access(qoder_executable, os.X_OK)
        resolved_qoder = str(qoder_executable)
    else:
        resolved = shutil.which(qoder_command) if qoder_command else None
        qoder_ok = bool(resolved)
        resolved_qoder = resolved or qoder_command

    checks = []
    problems = []

    for agent in selected:
        route_name = str(agent.raw.get("cli_route", ""))
        driver = getattr(agent, "driver", None) or str(getattr(agent, "raw", {}).get("driver", ""))
        try:
            selection = local_cli_route(config, route_name, agent.name)
            if driver == "agy":
                command_ok = agy_ok
                resolved_command = resolved_agy
                if not agy_ok:
                    err_msg = f"agy CLI 不可执行：{agy_command or '<missing>'}"
                else:
                    err_msg = ""
            elif driver == "dsh":
                command_ok = node_ok and dsh_ok
                resolved_command = f"{resolved_node} {resolved_dsh}".strip()
                if not node_ok and not dsh_ok:
                    err_msg = f"node 与 dsh 命令均不可访问: node={node_command}, dsh={dsh_command}"
                elif not node_ok:
                    err_msg = f"node 命令不可执行: {node_command or '<missing>'}"
                elif not dsh_ok:
                    err_msg = f"dsh CLI 脚本不存在: {dsh_command or '<missing>'}"
                else:
                    err_msg = ""
            elif driver == "qoder":
                command_ok = qoder_ok
                resolved_command = resolved_qoder
                if not qoder_ok:
                    err_msg = f"qodercli 不可执行: {qoder_command or '<missing>'}"
                else:
                    err_msg = ""
            else:
                command_ok = False
                resolved_command = ""
                err_msg = f"未知 driver: {driver}"

            check = {
                "agent": agent.name,
                "route": route_name,
                "driver": driver,
                "provider": selection["provider"],
                "model": selection["model"],
                "credential": selection["credential"],
                "command": resolved_command,
                "ok": command_ok,
            }
            if not command_ok:
                check["error"] = err_msg
                problems.append(f"{agent.name}: {err_msg}")
            checks.append(check)
        except GatewayUnavailable as exc:
            message = str(exc)
            checks.append({"agent": agent.name, "route": route_name, "driver": driver, "ok": False, "error": message})
            problems.append(f"{agent.name}: {message}")
    return {"ok": not problems, "checks": checks, "problems": problems}


def command_environment(config: AppConfig, driver: str) -> Dict[str, str]:
    """Build a scrubbed environment for a local desktop CLI driver."""
    environment = os.environ.copy()
    for key in (
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "XIAOBA_ROUTE_API_KEY",
    ):
        environment.pop(key, None)
    if driver == "agy":
        configured = str(config.integrations.get("agy_command", "")).strip()
        command_path = Path(configured).expanduser() if configured else None
        path_parts = [str(Path.home() / ".local" / "bin")]
        if command_path is not None and command_path.is_absolute():
            path_parts.append(str(command_path.parent))
        existing = environment.get("PATH", "").split(":")
        node_dir = str(Path(config.integrations["node_command"]).expanduser().parent)
        path_parts.extend([node_dir, *existing, "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
        environment["PATH"] = ":".join(dict.fromkeys(item for item in path_parts if item))
        return environment
    if driver == "dsh":
        configured = str(config.integrations.get("dsh_command", "")).strip()
        command_path = Path(configured).expanduser() if configured else None
        path_parts = []
        if command_path is not None and command_path.is_absolute():
            path_parts.append(str(command_path.parent))
        existing = environment.get("PATH", "").split(":")
        node_dir = str(Path(config.integrations["node_command"]).expanduser().parent)
        path_parts.extend([node_dir, *existing, "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
        environment["PATH"] = ":".join(dict.fromkeys(item for item in path_parts if item))
        return environment
    if driver == "qoder":
        # No QODER_* variable from the parent reaches qodercli: the model,
        # upstream and credentials come only from qodercli's own settings and
        # login, never from an inherited environment (pre-review R10).
        for key in [name for name in environment if name.startswith("QODER_")]:
            environment.pop(key, None)
        configured = str(config.integrations.get("qoder_command", "")).strip()
        command_path = Path(configured).expanduser() if configured else None
        path_parts = []
        if command_path is not None and command_path.is_absolute():
            path_parts.append(str(command_path.parent))
        existing = environment.get("PATH", "").split(":")
        path_parts.extend([*existing, "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
        environment["PATH"] = ":".join(dict.fromkeys(item for item in path_parts if item))
        return environment
    raise GatewayUnavailable(f"未知驱动：{driver}")
