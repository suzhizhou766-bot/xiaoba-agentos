"""Pure-data AgentOS config resolution, installed byte-for-byte in monitor_core.

This file is a release asset. Edit the asset source and refresh its manifest;
health and the monitor must never maintain separate precedence rules.
"""
import json
import os
import plistlib
from pathlib import Path


def worker_config_path(plist=None):
    path = Path(plist) if plist is not None else Path.home() / "Library/LaunchAgents/com.xiaoba.agentos.plist"
    try:
        with path.open("rb") as handle:
            value = (plistlib.load(handle).get("EnvironmentVariables") or {}).get("XIAOBA_CONFIG")
        return Path(value).expanduser() if isinstance(value, str) and value.strip() else None
    except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
        return None


def agentos_config_path(config, *, env=None, worker_plist=None):
    env = os.environ if env is None else env
    root = Path(config.get("root") or Path.cwd()).expanduser().resolve()
    def resolved(value):
        path = Path(value).expanduser()
        return path if path.is_absolute() else root / path
    explicit = config.get("agentos_config") or env.get("XIAOBA_CONFIG")
    if explicit:
        return resolved(explicit)
    worker = worker_config_path(worker_plist)
    if worker is not None and resolved(worker).is_file():
        return resolved(worker)
    project = resolved(config["agentos_project"])
    override = project / ".runtime/model-overrides/agentos-agy-flash-high.json"
    return override if override.is_file() else project / "config/agentos.json"


def agentos_runtime(config_path):
    raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("runtime_dir"), str) or not raw["runtime_dir"].strip():
        raise ValueError("AgentOS 配置缺少 runtime_dir")
    return Path(raw["runtime_dir"]).expanduser().resolve()
