"""配置：默认值 + 可选的 monitor.json 覆盖。路径、端口都从这里取，模块里不写死。"""
import copy
import json
import os
import plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "port": 8799,
    "agentos_project": "/Users/suzhizhou/Documents/小八AgentOS",
    # 留空则与 Worker 用同一份（读取 Worker 的 LaunchAgent），见 agentos_config_path
    "agentos_config": "",
    "page_file": "xiaoba-monitor.html",
    "director": {
        "file": "director.html",
        # 导演台页面允许连接的本地工具服务端口
        "service_ports": [8890, 8891, 8892, 8893],
        "migration_token_file": ".director-migration-token",
    },
    "localtools": {
        # 导演台三个服务开关背后的控制器：随监看中心启停，所以开关随时可用，不需要另外双击启动脚本
        "port": 8893,
        "script": "/Users/suzhizhou/Documents/私人助理/AI视频管线/06_内部工具/local_tools_control_server.py",
        "python": "/Users/suzhizhou/Documents/私人助理/.runtime/venvs/core/bin/python",
    },
    "stage": {
        # 3D 导演台的构建产物（在「小八3D导演台」目录执行 npm run build 生成）
        "dir": "小八3D导演台/dist",
        # 本机工程库：导演台的草稿和素材存在这里（用户数据，不进版本历史）
        "library_dir": "小八3D导演台/工程库",
    },
    "review": {
        # 裁决后端由监看中心拉起，端口每次临时分配。这里的 port 只是旧独立服务的端口，
        # 用来发现“旧服务又被启动了”，此时改为使用它而不重复拉起。
        "port": 8797,
        "script": "integrations/review-center/server.py",  # 相对 agentos_project
        "python": "/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14",  # 统一解释器
        "start_timeout_seconds": 150,
    },
    "keypool": {
        # Key 池代理：把多个中转站的 Key 合并成一个。只监听 127.0.0.1，随监看中心启停。
        "port": 8780,
        "file": "keypool.json",  # 节点与 Key 存在这里（权限 600，不进版本历史）
        "usage_dir": "run/keypool-usage",  # 用量记录（每次请求一行元数据，不含内容与 Key；留 90 天）
    },
    "watchdog": {
        "backoff_seconds": [1, 2, 5, 10, 30],
        "max_fast_failures": 5,       # 这么多次“启动后很快就退出”即熔断，不再自动重启
        "fast_failure_seconds": 60,
        "health_interval_seconds": 15,
        "health_failures_to_restart": 3,
    },
    "log": {"dir": "logs", "max_bytes": 1048576, "backups": 3},
}


def _merge(base, extra):
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load(path=None, overrides=None):
    """返回配置字典。monitor.json 不存在时全部用默认值；写错了直接报错，不带病运行。"""
    config = copy.deepcopy(DEFAULTS)
    path = Path(path) if path else ROOT / "monitor.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("monitor.json 必须是一个 JSON 对象")
        _merge(config, data)
    _merge(config, overrides)
    config["root"] = str(ROOT)
    return config


def resolve(config, relative):
    """相对路径一律相对监看中心目录。"""
    path = Path(relative)
    return path if path.is_absolute() else Path(config["root"]) / path


WORKER_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.xiaoba.agentos.plist"


def worker_config_path(plist=None):
    """Worker 的 LaunchAgent 里写的 XIAOBA_CONFIG：Worker 实际使用的那份配置。"""
    try:
        with open(plist or WORKER_PLIST, "rb") as handle:
            value = (plistlib.load(handle).get("EnvironmentVariables") or {}).get("XIAOBA_CONFIG")
        return Path(value) if value else None
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def agentos_config_path(config):
    """裁决后端与运行总览用哪份 AgentOS 配置。

    顺序：monitor.json 明确指定 → 环境变量 XIAOBA_CONFIG → 与 Worker 的 LaunchAgent 一致 → 项目默认。
    默认跟随 Worker，这样裁决与 Worker 永远是同一份配置，不会各用各的。
    """
    explicit = config.get("agentos_config") or os.environ.get("XIAOBA_CONFIG")
    if explicit:
        return Path(explicit)
    worker = worker_config_path()
    if worker and worker.is_file():
        return worker
    project = Path(config["agentos_project"])
    override = project / ".runtime" / "model-overrides" / "agentos-agy-flash-high.json"
    return override if override.exists() else project / "config" / "agentos.json"
