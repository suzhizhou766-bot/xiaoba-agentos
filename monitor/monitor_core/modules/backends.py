"""后台并发模块：三个后台 CLI 现在各开了几个进程、各自的并发上限、近一小时 429/404/500 次数。

只读：看进程表、读 Key 池用量记录（只有元数据）、只读打开 AgentOS 数据库的运行记录。
不读凭据、不读请求和回复内容，也不输出进程命令行（里面可能有路径和参数）。
上限来自发版审查脚本的 review_concurrency.json（2026-10-24 用户选甲）；读不到就显示“未配置”。
"""
import json
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from .. import config as cfg, keypool_stats
from ..web import Route
from . import Module

BACKENDS = ("dsh", "agy", "qoder")
AGENT_OF = {"dsh_review": "dsh", "agy_review": "agy", "qoder_review": "qoder"}
CODES = (429, 404, 500)
WINDOW = 3600
# 和 AgentOS 的 HTTP_5XX 一样要有状态上下文：裸数字（pid 4012、第 404 行）不算。
_STATUS = re.compile(
    r"(?:\bhttp\b|\bstatus\b|\bcode\b|\bupstream\b|\berror\b|\bserver\b|状态码)\W{0,3}(?:code\W{0,3})?\b(429|404|500)\b"
    r"|\b(429|404|500)\s+(?:too many requests|not found|internal server error|internal)",
    re.IGNORECASE,
)
DEFAULT_LIMITS = "/Users/suzhizhou/Documents/私人助理/工作区/小八AgentOS-v1.6.5-修复/work/review_concurrency.json"


def classify_process(command):
    """一行 ps 命令属于哪个后台的一次非交互运行；不是就返回 None。

    按整行判断：DSH 的安装路径带空格（DeepSeek Harness），按空格切开会把路径拆散。
    """
    command = command or ""
    words = command.split()
    if "DeepSeek Harness/current/apps/cli/lib/bin.js " in command and " headless" in command:
        return "dsh"
    if not words:
        return None
    first = Path(words[0]).name
    if first == "agy" and any(flag in words for flag in ("--print", "-p", "--output-format")):
        return "agy"
    if first == "qodercli" and any(flag in words for flag in ("-p", "--print")):
        return "qoder"
    return None


def count_running(rows):
    counts = dict.fromkeys(BACKENDS, 0)
    for command in rows:
        kind = classify_process(command)
        if kind:
            counts[kind] += 1
    return counts


def status_code(text):
    match = _STATUS.search(text or "")
    return int(match.group(1) or match.group(2)) if match else None


def count_keypool(rows):
    out = dict.fromkeys(CODES, 0)
    for row in rows:
        if row.get("status") in CODES:
            out[row["status"]] += 1
    return out


def count_runs(rows):
    out = {kind: dict.fromkeys(CODES, 0) for kind in BACKENDS}
    for agent, error in rows:
        kind = AGENT_OF.get(agent)
        code = status_code(error)
        if kind and code:
            out[kind][code] += 1
    return out


class BackendsModule(Module):
    id, title = "backends", "后台并发"

    def __init__(self, ctx):
        super().__init__(ctx)
        conf = ctx.config.get("backends", {}) if isinstance(ctx.config.get("backends"), dict) else {}
        self.limits_path = Path(conf.get("limits_file", DEFAULT_LIMITS))
        self.usage_dir = str(cfg.resolve(ctx.config, ctx.config.get("keypool", {}).get("usage_dir", "run/keypool-usage")))
        self.databases = [Path(ctx.config["agentos_project"]) / ".runtime" / "agentos.sqlite3"]
        self.acceptance_state = self.limits_path.parent.parent / "acceptance" / "STATE.json"

    def routes(self):
        return [Route("GET", "/api/backends", self.api_backends, "read")]

    def _processes(self):
        try:
            out = subprocess.run(["/bin/ps", "-axww", "-o", "command="], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.splitlines()

    def _limits(self):
        try:
            limits = json.loads(self.limits_path.read_text(encoding="utf-8")).get("limits") or {}
        except (OSError, ValueError, AttributeError):
            return None
        return {AGENT_OF.get(name, name): value for name, value in limits.items()}

    def _run_errors(self, since):
        paths = list(self.databases)
        try:
            state = json.loads(self.acceptance_state.read_text(encoding="utf-8"))
            paths.append(Path(state["root"]) / "runtime" / "agentos.sqlite3")
        except (OSError, ValueError, KeyError, TypeError):
            pass
        rows = []
        for path in paths:
            if not path.is_file():
                continue
            try:
                conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
                try:
                    rows.extend(conn.execute(
                        "SELECT agent_name, error FROM agent_runs WHERE created_at>=? AND error IS NOT NULL", (since,)))
                finally:
                    conn.close()
            except sqlite3.Error:
                continue
        return rows

    def snapshot(self, now=None):
        now = now or time.time()
        since = now - WINDOW
        processes = self._processes()
        return {
            "ok": True,
            "running": count_running(processes) if processes is not None else None,
            "limits": self._limits(),
            "windowSec": WINDOW,
            "keypool": count_keypool(keypool_stats.load(self.usage_dir, since, now)),
            "runs": count_runs(self._run_errors(since)),
            "t": round(now, 3),
        }

    def api_backends(self, req):
        return 200, self.snapshot()

    def health(self):
        return {"status": "ok"}
