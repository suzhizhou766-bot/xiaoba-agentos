"""本地工具控制器模块：导演台里三个服务开关（反推 / 超分 / 深度图）背后的控制器，由监看中心拉起并看护。

控制器是 AI视频管线/06_内部工具/local_tools_control_server.py（只监听 127.0.0.1，默认端口 8893），
三个服务本身仍由它按页面上的开关启停，这里不改它的任何逻辑。
监看中心负责它的生命周期，所以监看中心开着，开关就能用，不需要再双击启动脚本：
  - 监看中心启动时把它拉起来并持续看护（退出自动重启，反复失败则熔断报障）；
  - 端口上已有一个控制器时（启动脚本拉起的，或监看中心上次被强行结束留下的），先结束它再自己拉起，
    这样它始终归监看中心管；占着端口的不是控制器时不动它，只报障；
  - 监看中心关闭时，先让控制器关掉还开着的服务，再关闭控制器。
"""
import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import config as cfg
from ..supervise import Backoff, Breaker, ManagedProcess
from . import Module


class LocalToolsModule(Module):
    id, title, kind = "localtools", "本地工具控制器", "service"

    def __init__(self, ctx, manage=True):
        super().__init__(ctx)
        settings = ctx.config["localtools"]
        self.port = int(settings["port"])
        self.script = Path(settings["script"])
        self.python = settings["python"]
        self.manage = manage          # 测试时可关掉
        self.process = None
        self.blocked = ""

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    # ---- 生命周期 ----
    def start(self):
        if not self.manage:
            return
        self.blocked = ""
        missing = next((path for path in (self.script, Path(self.python)) if not path.exists()), None)
        if missing:
            self.blocked = "找不到控制器的启动文件：%s" % missing
            self.ctx.log(self.id, "missing file", "error", path=str(missing))
            return
        self._retire_previous()
        if self._port_busy():
            self.blocked = "端口 %d 被其他程序占用，本地工具控制器没有启动" % self.port
            self.ctx.log(self.id, "port busy", "error", port=self.port)
            return
        watchdog = self.ctx.config["watchdog"]
        log_dir = cfg.resolve(self.ctx.config, self.ctx.config["log"]["dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.process = ManagedProcess(
            "localtools", [self.python, str(self.script)], self.ctx.logger, env=env, cwd=str(self.script.parent),
            ready=lambda: self._call("GET", "/healthz", timeout=2) is not None, ready_timeout=20,
            backoff=Backoff(watchdog["backoff_seconds"]),
            breaker=Breaker(watchdog["max_fast_failures"], watchdog["fast_failure_seconds"]),
            output_path=str(log_dir / "local-tools-control.log"))
        self.process.start()
        self.ctx.log(self.id, "controller managed by monitor", port=self.port)

    def stop(self):
        if not self.process:
            return
        payload = self._call("GET", "/healthz", timeout=3) or {}
        for name, info in (payload.get("services") or {}).items():
            if info.get("running"):
                self._call("POST", "/service", {"service": name, "action": "stop"}, timeout=15)
                self.ctx.log(self.id, "service stopped with monitor", service=name)
        self.process.stop()

    def health(self):
        if self.blocked:
            return {"status": "failed", "detail": self.blocked}
        if not self.process:
            return {"status": "warn", "detail": "未由监看中心管理"}
        status = self.process.status()
        if status["state"] == "running":
            return {"status": "ok", "detail": "控制器在线（端口 %d）；三个服务由导演台里的开关控制" % self.port}
        if status["state"] == "failed":
            return {"status": "failed", "detail": status["error"] or "控制器反复退出，已停止自动重启"}
        return {"status": "starting", "detail": status["error"] or "控制器正在启动"}

    # ---- 内部 ----
    def _port_busy(self):
        with socket.socket() as sock:
            sock.settimeout(0.5)
            return sock.connect_ex(("127.0.0.1", self.port)) == 0

    def _call(self, method, path, body=None, timeout=3):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def _listener_pids(self):
        try:
            out = subprocess.run(["/usr/sbin/lsof", "-nP", "-tiTCP:%d" % self.port, "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=5)
            return [int(line) for line in out.stdout.split() if line.isdigit()]
        except (OSError, subprocess.SubprocessError):
            return []

    def _is_controller(self, pid):
        try:
            out = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        return self.script.name in out.stdout

    def _retire_previous(self):
        """端口上已有的控制器（启动脚本或上一次运行留下的）：确认是它，就结束它，由监看中心重新拉起。"""
        retired = False
        for pid in self._listener_pids():
            if not self._is_controller(pid):
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                retired = True
                self.ctx.log(self.id, "stopped controller started elsewhere", pid=pid)
            except OSError:
                pass
        for _ in range(20 if retired else 0):
            if not self._port_busy():
                break
            time.sleep(0.25)
