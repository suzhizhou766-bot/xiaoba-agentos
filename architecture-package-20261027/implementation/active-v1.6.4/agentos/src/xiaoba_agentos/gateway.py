from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .config import AppConfig


class GatewayUnavailable(RuntimeError):
    pass


_DESKTOP_REASON_TEXT = {
    "desktop_not_running": "Antigravity 桌面应用未运行或调试端口不可用，请打开 Antigravity",
    "desktop_unresponsive": "Antigravity 桌面应用无响应",
    "ambiguous_window": "Antigravity 打开了多个窗口，请只保留一个",
    "desktop_state_unreadable": "读不到 Antigravity 界面里的模型选择，界面结构可能已变化",
    "model_unavailable": "Antigravity 当前选中的模型不可用或无法映射到 CLI 模型",
    "non_local_debugger": "调试端口不是本机回环地址",
    "missing_arguments": "缺少调试端口文件参数",
    "unknown": "未知，请运行 doctor 查看",
}
_DESKTOP_REASON_LINE = re.compile(r"^reason=([a-z_]{1,40})$", re.MULTILINE)


def _read_agy_desktop(config: AppConfig, route: Dict[str, Any]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run([
        str(config.integrations["node_command"]),
        str(config.project_dir / "scripts" / "agy_desktop_route.cjs"),
        str(route["desktop_debug_port"]),
    ], capture_output=True, text=True, timeout=15, check=False)


def _desktop_failure_reason(stderr: Optional[str]) -> str:
    match = _DESKTOP_REASON_LINE.search(stderr or "")
    reason = match.group(1) if match else "unknown"
    return reason if reason in _DESKTOP_REASON_TEXT else "unknown"


def ensure_desktop_available(config: AppConfig, agents: Iterable[Any]) -> list:
    """Start the Antigravity desktop when a route explicitly asks for it.

    Opt-in per route (desktop_auto_start: true with desktop_app_path). It only
    acts when the desktop is not running; an ambiguous window, an unreadable
    UI or an unavailable model are left for the readiness check to report. The
    model still comes from the desktop selection; nothing falls back.
    Never raises: a failed start surfaces through the normal readiness gate.
    """
    actions: list = []
    seen: set = set()
    for agent in agents:
        if agent.type != "local_cli" or agent.raw.get("driver") != "agy":
            continue
        route_name = str(agent.raw.get("cli_route", ""))
        route = config.cli_routes.get(route_name)
        if route_name in seen or not isinstance(route, dict) or route.get("desktop_auto_start") is not True:
            continue
        seen.add(route_name)
        action = {"route": route_name, "started": False, "ready": False}
        actions.append(action)
        try:
            first = _read_agy_desktop(config, route)
            if first.returncode == 0:
                action["ready"] = True
                continue
            action["reason"] = _desktop_failure_reason(first.stderr)
            if action["reason"] != "desktop_not_running":
                continue
            subprocess.run(
                ["/usr/bin/open", "-g", "-a", str(route["desktop_app_path"])],
                capture_output=True, text=True, timeout=15, check=False,
            )
            action["started"] = True
            deadline = time.monotonic() + float(route.get("desktop_start_wait_seconds", 30))
            while time.monotonic() < deadline:
                time.sleep(1)
                if _read_agy_desktop(config, route).returncode == 0:
                    action["ready"] = True
                    break
        except (OSError, KeyError, ValueError, subprocess.TimeoutExpired) as exc:
            action["error"] = type(exc).__name__
    return actions


def local_cli_route(config: AppConfig, route_name: str, agent_name: str) -> Dict[str, Any]:
    """Resolve a local-account or local-harness CLI route without consulting CC Switch."""
    route = config.cli_routes.get(route_name)
    if not isinstance(route, dict):
        raise GatewayUnavailable(f"本地 CLI 路由未登记：{route_name}")
    driver = str(route.get("driver", ""))
    if driver == "agy":
        if str(route.get("selection", "")) != "local_account":
            raise GatewayUnavailable(f"本地 CLI 路由选择策略无效：{route_name}")
        credential_desc = "managed_by_antigravity_local_account"
        selection = "local_account"
    elif driver == "dsh":
        if str(route.get("selection", "")) != "local_harness":
            raise GatewayUnavailable(f"本地 CLI 路由选择策略无效：{route_name}")
        credential_desc = "managed_by_dsh_desktop_settings"
        selection = "local_harness"
    else:
        raise GatewayUnavailable(f"本地 CLI 路由驱动不支持：{route_name} ({driver})")

    if driver == "agy" and route.get("desktop_debug_port"):
        reason = "unknown"
        try:
            result = _read_agy_desktop(config, route)
            if result.returncode:
                reason = _desktop_failure_reason(result.stderr)
                raise ValueError("invalid desktop route")
            metadata = json.loads(result.stdout)
            if not isinstance(metadata, dict):
                raise ValueError("invalid desktop metadata")
            model = metadata.get("model")
            if (metadata.get("provider") != "Antigravity"
                    or not isinstance(model, str) or not model.strip()
                    or metadata.get("model_source") != "antigravity_desktop"):
                raise ValueError("invalid desktop metadata")
            return {**metadata, "route": route_name, "driver": driver,
                    "selection": selection, "credential": credential_desc,
                    "global_switch_required": False}
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            failure = GatewayUnavailable(
                "Antigravity 桌面模型读取失败，停止调用；不使用静态备用模型"
                + "（原因：" + _DESKTOP_REASON_TEXT.get(reason, _DESKTOP_REASON_TEXT["unknown"]) + "）"
            )
            failure.desktop_reason = reason
            raise failure from exc

    if driver == "dsh" and route.get("desktop_config") and "web_config" not in route:
        try:
            result = subprocess.run([
                str(config.integrations["node_command"]),
                str(config.project_dir / "scripts" / "dsh_desktop_route.cjs"),
                str(config.integrations["dsh_command"]), str(route["desktop_config"]),
            ], capture_output=True, text=True, timeout=10, check=False)
            if result.returncode:
                raise ValueError("invalid Desktop route")
            metadata = json.loads(result.stdout)
            if (not isinstance(metadata, dict)
                    or metadata.get("model_source") != "dsh_desktop"
                    or any(not isinstance(metadata.get(key), str) or not metadata[key].strip()
                           for key in ("provider", "provider_id", "model"))):
                raise ValueError("invalid Desktop metadata")
            public_metadata = {key: metadata[key] for key in ("provider", "provider_id", "model", "model_source")}
            return {**public_metadata, "route": route_name, "driver": driver,
                    "selection": selection, "credential": credential_desc,
                    "global_switch_required": False, "desktop_config": str(route["desktop_config"])}
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise GatewayUnavailable("DSH Desktop 路由读取失败，停止调用") from exc

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
    raise GatewayUnavailable(f"未知驱动：{driver}")
