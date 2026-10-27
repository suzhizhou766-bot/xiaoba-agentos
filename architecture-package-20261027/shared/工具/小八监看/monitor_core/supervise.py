"""进程看护：退避重启 + 熔断。

同一套机制用在两处：
  1. 监看中心看护它管理的子服务（如裁决后端）；
  2. 启动脚本里的看护进程看护监看中心自己。
规则：子进程退出就按退避间隔重启；如果连续多次“起来没多久就退出”，说明不是偶发故障，
熔断并停止自动重启，把故障原样报出来，等人处理后再手动复位。
"""
import os
import subprocess
import threading
import time

from . import logs


class Backoff:
    def __init__(self, delays=(1, 2, 5, 10, 30)):
        self.delays = tuple(delays) or (1,)
        self.step = 0

    def next(self):
        delay = self.delays[min(self.step, len(self.delays) - 1)]
        self.step += 1
        return delay

    def reset(self):
        self.step = 0


class Breaker:
    """连续 max_failures 次快速失败后断开。运行超过 fast_seconds 的那次退出不算快速失败，并清零计数。"""

    def __init__(self, max_failures=5, fast_seconds=60):
        self.max_failures = max_failures
        self.fast_seconds = fast_seconds
        self.failures = 0
        self.open = False

    def record_exit(self, lifetime_seconds):
        if lifetime_seconds >= self.fast_seconds:
            self.failures = 0
            return False
        self.failures += 1
        if self.failures >= self.max_failures:
            self.open = True
        return self.open

    def reset(self):
        self.failures = 0
        self.open = False


class ManagedProcess:
    """看护一个子进程。状态：stopped / starting / running / backoff / failed（已熔断）。"""

    def __init__(self, name, argv, logger, env=None, cwd=None, umask=None, ready=None, ready_timeout=60,
                 backoff=None, breaker=None, output_path=None, sleep=time.sleep):
        self.name, self.argv, self.logger = name, list(argv), logger
        self.env, self.cwd, self.umask = env, cwd, umask
        self.ready, self.ready_timeout = ready, ready_timeout
        self.backoff = backoff or Backoff()
        self.breaker = breaker or Breaker()
        self.output_path = output_path
        self._sleep = sleep
        self.state = "stopped"
        self.proc = None
        self.restarts = 0
        self.last_exit = None
        self.last_error = ""
        self.since = time.time()
        self._stopping = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    # ---- 对外 ----
    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stopping.clear()
            self._thread = threading.Thread(target=self._run, name="watch-" + self.name, daemon=True)
            self._thread.start()

    def stop(self, timeout=8):
        self._stopping.set()
        self._terminate(timeout)
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout + 2)
        self._set("stopped")

    def reset(self):
        """人工复位熔断并重新拉起。"""
        self.breaker.reset()
        self.backoff.reset()
        self.last_error = ""
        if self.state == "failed":
            self._set("stopped")
        self.start()

    def kill_child(self):
        """只结束当前子进程，看护循环会按规则重启它（用于“活着但不响应”的情况）。"""
        self._terminate(5)

    def status(self):
        proc = self.proc
        return {"state": self.state, "pid": proc.pid if proc and proc.poll() is None else None,
                "restarts": self.restarts, "last_exit": self.last_exit, "error": self.last_error or None,
                "since": self.since}

    # ---- 内部 ----
    def _set(self, state):
        if state != self.state:
            self.state, self.since = state, time.time()

    def _spawn(self):
        output = open(self.output_path, "ab") if self.output_path else subprocess.DEVNULL
        try:
            def prepare():
                os.setsid()  # 独立会话：启动它的终端或会话关掉时，不会顺带把它带走
                if self.umask is not None:
                    os.umask(self.umask)
            return subprocess.Popen(self.argv, env=self.env, cwd=self.cwd, stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=subprocess.STDOUT, preexec_fn=prepare)
        finally:
            if output is not subprocess.DEVNULL:
                output.close()

    def _terminate(self, timeout):
        proc = self.proc
        if not proc or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                # SIGKILL 后仍未退出（卡在内核态等），记下来但不让看护进程跟着崩。
                logs.event(self.logger, self.name, "kill timeout", "error", pid=proc.pid)
        except OSError:
            pass

    def _wait_ready(self):
        if not self.ready:
            return True
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline and not self._stopping.is_set():
            if self.proc.poll() is not None:
                return False
            try:
                if self.ready():
                    return True
            except Exception:
                pass
            self._sleep(0.5)
        return False

    def _run(self):
        while not self._stopping.is_set():
            self._set("starting")
            started = time.monotonic()
            try:
                self.proc = self._spawn()
            except OSError as exc:
                self.last_error = "无法启动：%s" % exc
                logs.event(self.logger, self.name, "spawn failed", "error", error=str(exc))
                self.proc = None
            if self.proc:
                logs.event(self.logger, self.name, "started", pid=self.proc.pid)
                if self._wait_ready():
                    self._set("running")
                    self.last_error = ""
                    logs.event(self.logger, self.name, "ready", pid=self.proc.pid)
                    self.proc.wait()
                elif not self._stopping.is_set():
                    self.last_error = self.last_error or "在 %d 秒内没有就绪" % self.ready_timeout
                    self._terminate(5)
                self.last_exit = self.proc.poll()
            if self._stopping.is_set():
                break
            lifetime = time.monotonic() - started
            if self.state == "running" and lifetime >= self.breaker.fast_seconds:
                self.backoff.reset()
            logs.event(self.logger, self.name, "exited", "warning", exit=self.last_exit, lifetime=round(lifetime, 1))
            if self.breaker.record_exit(lifetime):
                self.last_error = (self.last_error or "进程反复退出（退出码 %s）" % self.last_exit) + "；已停止自动重启"
                self._set("failed")
                logs.event(self.logger, self.name, "circuit open", "error", failures=self.breaker.failures)
                return
            self.restarts += 1
            self._set("backoff")
            delay = self.backoff.next()
            if self._stopping.wait(delay):
                break
        self._set("stopped")
