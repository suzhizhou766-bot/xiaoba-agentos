#!/usr/bin/env python3
"""小八监看中心：入口。

用法：
  python3 xiaoba_monitor.py               直接运行监看中心（前台）
  python3 xiaoba_monitor.py --supervise   带看护运行：监看中心退出或不响应时自动重启（启动脚本用这个）
  python3 xiaoba_monitor.py --selftest    自检：配置、文件、路由、数据库是否就绪，不启动任何服务
  python3 xiaoba_monitor.py --once        打印一次运行总览快照后退出

结构：monitor_core 是内核（HTTP 与安全门禁、进程看护、日志、配置），
monitor_core/modules 下每个文件是一个模块。新增模块在下面 MODULES 里加一行即可。
只监听 127.0.0.1。
"""
import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

from monitor_core import VERSION, config as cfg, logs
from monitor_core.modules.backends import BackendsModule
from monitor_core.modules.director import DirectorModule
from monitor_core.modules.frozen import FrozenModule
from monitor_core.modules.keypool import KeyPoolModule
from monitor_core.modules.localtools import LocalToolsModule
from monitor_core.modules.overview import OverviewModule
from monitor_core.modules.review import ReviewModule
from monitor_core.modules.stage import StageModule
from monitor_core.modules.system import SystemModule
from monitor_core.supervise import Backoff, Breaker, ManagedProcess
from monitor_core.web import App, Context

# 模块注册表：顺序即启动顺序。
MODULES = [SystemModule, ReviewModule, FrozenModule, OverviewModule, DirectorModule, LocalToolsModule, StageModule, KeyPoolModule, BackendsModule]

ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / "run"


def build_app(config, logger):
    ctx = Context(config, logger, int(config["port"]))
    app = App(ctx)
    for module_class in MODULES:
        app.register(module_class(ctx))
    return app


def port_in_use(port):
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def healthy(port, timeout=3):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def make_logger(config, filename="monitor.log"):
    settings = config["log"]
    return logs.setup(cfg.resolve(config, settings["dir"]), settings["max_bytes"], settings["backups"], filename=filename)


# ---------------------------------------------------------------- 直接运行
def serve(config):
    port = int(config["port"])
    if port_in_use(port):
        print("端口 %d 已被占用：监看中心可能已在运行。" % port, file=sys.stderr)
        return 2
    logger = make_logger(config)
    app = build_app(config, logger)
    httpd = app.server()
    stopping = threading.Event()

    def shutdown(_signum, _frame):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    app.start()
    app.ctx.log("core", "monitor started", port=port, version=VERSION, pid=os.getpid(),
                modules=[module.id for module in app.modules])
    print("小八监看中心 v%s：http://127.0.0.1:%d/" % (VERSION, port), flush=True)
    try:
        httpd.serve_forever()
    finally:
        app.stop()          # 先停掉自己拉起的子服务，再退出
        httpd.server_close()
        app.ctx.log("core", "monitor stopped")
    return 0


# ---------------------------------------------------------------- 带看护运行
def supervise(config):
    port = int(config["port"])
    RUN_DIR.mkdir(exist_ok=True)
    pid_file = RUN_DIR / "supervisor.pid"
    try:
        other = int(pid_file.read_text().strip())
        os.kill(other, 0)
        print("看护进程已在运行（pid %d）。" % other, file=sys.stderr)
        return 2
    except (OSError, ValueError):
        pass
    if port_in_use(port):
        print("端口 %d 已被占用：请先关闭正在运行的监看中心。" % port, file=sys.stderr)
        return 2
    pid_file.write_text(str(os.getpid()))
    logger = make_logger(config, "supervisor.log")
    watchdog = config["watchdog"]
    child = ManagedProcess(
        "monitor", [sys.executable, str(ROOT / "xiaoba_monitor.py")], logger, cwd=str(ROOT),
        ready=lambda: healthy(port), ready_timeout=30,
        backoff=Backoff(watchdog["backoff_seconds"]),
        breaker=Breaker(watchdog["max_fast_failures"], watchdog["fast_failure_seconds"]),
        output_path=str(cfg.resolve(config, config["log"]["dir"]) / "monitor.out.log"))
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    logs.event(logger, "supervisor", "started", pid=os.getpid(), port=port)
    child.start()
    misses = 0
    try:
        while not stopping.wait(watchdog["health_interval_seconds"]):
            state = child.status()["state"]
            if state == "failed":
                logs.event(logger, "supervisor", "monitor keeps failing; giving up", "error", error=child.last_error)
                return 1
            if state != "running":
                misses = 0
                continue
            # 进程还在但不响应，同样算故障：连续几次探测失败就重启它
            misses = 0 if healthy(port) else misses + 1
            if misses >= watchdog["health_failures_to_restart"]:
                logs.event(logger, "supervisor", "monitor unresponsive; restarting", "warning", misses=misses)
                misses = 0
                child.kill_child()
    finally:
        child.stop()
        try:
            pid_file.unlink()
        except OSError:
            pass
        logs.event(logger, "supervisor", "stopped")
    return 0


# ---------------------------------------------------------------- 自检
def selftest(config):
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": str(detail)})

    try:
        app = build_app(config, logs.setup(None))
        check("模块注册", True, "、".join(module.id for module in app.modules))
        check("路由表", len(app.routes) > 0, "%d 条" % len(app.routes))
        for module in app.modules:
            if module.id == "review":
                check("裁决后端脚本", module.script.is_file(), module.script)
                check("AgentOS 配置", module.config_path.is_file(), module.config_path)
            else:
                health = module.health()
                check("模块 %s" % module.id, health.get("status") != "failed", health.get("detail", ""))
        page = cfg.resolve(config, config["page_file"])
        check("页面文件", page.is_file(), page)
        snapshot = app.ctx.modules["overview"].monitor.snapshot()
        check("运行数据可读", not snapshot["system"].get("note"), snapshot["system"].get("note") or "数据库与日志读取正常")
    except Exception as exc:
        check("自检过程", False, repr(exc))
    ok = all(item["ok"] for item in checks)
    print(json.dumps({"ok": ok, "version": VERSION, "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(description="小八监看中心")
    parser.add_argument("--config-file", default=None, help="默认读取同目录的 monitor.json（可以没有）")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--supervise", action="store_true", help="带看护运行")
    parser.add_argument("--selftest", action="store_true", help="自检后退出")
    parser.add_argument("--once", action="store_true", help="打印一次运行总览快照后退出")
    args = parser.parse_args()
    config = cfg.load(args.config_file, {"port": args.port} if args.port else None)
    if args.selftest:
        return selftest(config)
    if args.once:
        app = build_app(config, logs.setup(None))
        app.ctx.modules["overview"].start()
        print(json.dumps(app.ctx.modules["overview"].monitor.snapshot(), ensure_ascii=False, indent=2, default=str))
        return 0
    if args.supervise:
        return supervise(config)
    return serve(config)


if __name__ == "__main__":
    sys.exit(main())
