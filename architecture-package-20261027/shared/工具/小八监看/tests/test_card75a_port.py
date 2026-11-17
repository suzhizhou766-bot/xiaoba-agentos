"""卡 75A r2：配置端口完整启停路径。

全部在临时副本 + 动态临时端口上跑；禁止启动/停止 8799。
关闭测试直接执行候选关闭脚本原文，用 XIAOBA_LSOF 包装器把探测限制在本轮临时端口。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
import unittest
import urllib.request

try:
    from .detach_support import wait_supervisor_ready
except ImportError:
    from detach_support import wait_supervisor_ready

MONITOR = Path(__file__).resolve().parents[1]
PY = "/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14"
FORBIDDEN_PORT = 8799

LSOF_WRAPPER = """#!/bin/bash
# 只允许查询 XIAOBA_LSOF_ALLOW_PORT；其它端口（含 8799）一律当作“无监听”，绝不转调真实 lsof。
allowed="${XIAOBA_LSOF_ALLOW_PORT:?}"
port=""
for a in "$@"; do
  case "$a" in
    -tiTCP:*|-iTCP:*)
      port="${a##*:}"
      port="${port%%[^0-9]*}"
      ;;
  esac
done
if [ -z "$port" ] || [ "$port" != "$allowed" ]; then
  # 非本轮端口：对 -t 返回空；对探测返回“无监听”
  for a in "$@"; do
    case "$a" in
      -t*) exit 1 ;;
    esac
  done
  exit 1
fi
exec /usr/sbin/lsof "$@"
"""


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    if port == FORBIDDEN_PORT:
        return free_port()
    return port


def healthy(port, timeout=0.5):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def listening_pids(port):
    out = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-tiTCP:%d" % port, "-sTCP:LISTEN"],
        capture_output=True, text=True,
    ).stdout.split()
    return [int(pid) for pid in out if pid.strip().isdigit()]


def kill_port(port):
    for pid in listening_pids(port):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and listening_pids(port):
        time.sleep(0.1)


class PortLaunchTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._temp.name).resolve()
        self.copy = self.root / "monitor"
        shutil.copytree(
            MONITOR, self.copy,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "run", "logs", "node_modules", "tests", "小八3D导演台",
            ),
        )
        self.ports = []
        self.supervisor = None
        self.caller = None
        self.lsof = self.root / "lsof-allow-temp-port"
        self.lsof.write_text(LSOF_WRAPPER)
        self.lsof.chmod(self.lsof.stat().st_mode | stat.S_IXUSR)
        self.pid_8799_before = set(listening_pids(FORBIDDEN_PORT))

    def tearDown(self):
        if self.caller and self.caller.poll() is None:
            try:
                os.killpg(self.caller.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.supervisor:
            try:
                os.kill(self.supervisor, signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(0.2)
            try:
                os.kill(self.supervisor, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for port in self.ports:
            self.assertNotEqual(port, FORBIDDEN_PORT)
            kill_port(port)
        self.assertEqual(self.pid_8799_before, set(listening_pids(FORBIDDEN_PORT)))
        self._temp.cleanup()

    def alloc(self):
        port = free_port()
        self.ports.append(port)
        return port

    def write_monitor_json(self, port, **extra):
        (self.root / "agentos.json").write_text("{}")
        payload = {
            "port": port,
            "agentos_project": str(self.root / "agentos-project"),
            "agentos_config": str(self.root / "agentos.json"),
            "review": {"port": self.alloc(), "start_timeout_seconds": 5},
            "keypool": {
                "port": self.alloc(),
                "file": str(self.root / "keypool.json"),
                "usage_dir": str(self.root / "keypool-usage"),
            },
            "localtools": {"port": self.alloc(), "script": str(self.root / "missing-local-tools.py")},
            "stage": {"dir": str(self.root / "dist"), "library_dir": str(self.root / "library")},
            "taskcards": {"dirs": [], "ledger": str(self.root / "taskcards.jsonl"), "receipt_root": str(self.root)},
            "log": {"dir": str(self.root / "logs")},
        }
        payload.update(extra)
        path = self.copy / "monitor.json"
        path.write_text(json.dumps(payload))
        return path

    def env(self, allow_port=None, **extra):
        env = {k: v for k, v in os.environ.items() if k != "XIAOBA_MONITOR_PORT"}
        env["XIAOBA_MONITOR_NO_BROWSER"] = "1"
        if allow_port is not None:
            env["XIAOBA_LSOF"] = str(self.lsof)
            env["XIAOBA_LSOF_ALLOW_PORT"] = str(allow_port)
        env.update(extra)
        return env

    def resolve_port(self, config_file=None, port=None, env=None, extra_args=None):
        cmd = [PY, "-B", str(self.copy / "xiaoba_monitor.py"), "--resolve-port"]
        if config_file is not None:
            cmd.extend(["--config-file", str(config_file)])
        if port is not None:
            cmd.extend(["--port", str(port)])
        if extra_args:
            cmd.extend(extra_args)
        return subprocess.run(cmd, capture_output=True, text=True, env=env or self.env(), cwd=str(self.copy))

    def start(self, env=None, keep_caller=False):
        script = str(self.copy / "启动监看中心.command")
        command = ["/bin/bash", script]
        if keep_caller:
            # 启动成功后读管道阻塞，保证调用方进程组确实能被整组结束。
            command = ["/bin/bash", "-c", '"$1" || exit "$?"; read -r _', "detach-caller", script]
        self.caller = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if keep_caller else None,
            env=env or self.env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        return self.caller

    def close_script(self, env=None):
        # 直接执行候选关闭脚本原文（临时树里的拷贝与仓库字节同源，不做 8799 文本替换）。
        return subprocess.run(
            ["/bin/bash", str(self.copy / "关闭监看中心.command")],
            capture_output=True, text=True, timeout=60, env=env or self.env(),
        )

    def wait_healthy(self, port, seconds=40):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if healthy(port):
                return True
            time.sleep(0.1)
        return False

    def read_supervisor(self):
        pid_file = self.copy / "run" / "supervisor.pid"
        self.assertTrue(pid_file.is_file(), "缺少 run/supervisor.pid")
        self.supervisor = int(pid_file.read_text().strip())
        self.assertTrue(wait_supervisor_ready(self.root, self.supervisor), '监管进程未完成就绪探测')
        return self.supervisor

    def assert_no_start_artifacts(self):
        self.assertFalse((self.copy / "run" / "supervisor.pid").exists())
        self.assertFalse((self.copy / "logs").exists())

    def test_script_uses_shared_resolver_and_passes_resolved_port(self):
        start = (self.copy / "启动监看中心.command").read_text(encoding="utf-8")
        close = (self.copy / "关闭监看中心.command").read_text(encoding="utf-8")
        self.assertNotIn('PORT="${XIAOBA_MONITOR_PORT:-8799}"', start)
        self.assertIn("--resolve-port", start)
        self.assertIn('--port "$PORT"', start)
        self.assertIn("--resolve-port", close)
        self.assertIn('tiTCP:${PORT}', close)
        self.assertNotIn("tiTCP:8799", close)

    def test_resolve_port_reads_monitor_json(self):
        port = self.alloc()
        path = self.write_monitor_json(port)
        done = self.resolve_port(config_file=path)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(str(port), done.stdout.strip())

    def test_missing_monitor_json_defaults_to_8799_without_starting(self):
        self.assertFalse((self.copy / "monitor.json").exists())
        done = self.resolve_port()
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual("8799", done.stdout.strip())

    def test_cli_port_zero_rejected_not_fallback(self):
        # --port 0 必须拒绝，不能因真假判断落到默认 8799。
        done = self.resolve_port(port=0)
        self.assertNotEqual(0, done.returncode)
        self.assertIn("无法解析监看中心端口", done.stderr)
        self.assertNotIn("8799", done.stdout)
        # 直接走 main 运行入口同样拒绝
        run = subprocess.run(
            [PY, "-B", str(self.copy / "xiaoba_monitor.py"), "--port", "0", "--once"],
            capture_output=True, text=True, cwd=str(self.copy), env=self.env(),
        )
        self.assertNotEqual(0, run.returncode)
        self.assertNotIn("8799", run.stdout)

    def test_custom_port_without_env_starts_and_opens_same_url(self):
        port = self.alloc()
        self.write_monitor_json(port)
        proc = self.start(self.env())
        self.assertTrue(self.wait_healthy(port), "自定义端口 %d 未起来" % port)
        supervisor = self.read_supervisor()
        proc.wait(timeout=15)
        out = (proc.stdout.read() if proc.stdout else "") + (proc.stderr.read() if proc.stderr else "")
        self.assertIn("http://127.0.0.1:%d/" % port, out)
        self.assertNotEqual(os.getsid(supervisor), proc.pid)

    def test_env_overrides_monitor_json_port(self):
        json_port = self.alloc()
        env_port = self.alloc()
        self.write_monitor_json(json_port)
        proc = self.start(self.env(XIAOBA_MONITOR_PORT=str(env_port)))
        self.assertTrue(self.wait_healthy(env_port), "环境变量端口 %d 未起来" % env_port)
        self.assertFalse(healthy(json_port), "不应仍监听 monitor.json 端口")
        self.read_supervisor()
        proc.wait(timeout=15)
        out = proc.stdout.read() if proc.stdout else ""
        self.assertIn("http://127.0.0.1:%d/" % env_port, out)

    def test_env_invalid_values_fail_before_probe_or_start(self):
        # 合法配置在，但环境覆盖非法：必须在健康探测/建日志/拉起前失败。
        good = self.alloc()
        self.write_monitor_json(good)
        before_8799 = set(listening_pids(FORBIDDEN_PORT))
        for bad in ("0", "-1", "999999", "abc", "true", "1.5"):
            with self.subTest(bad=bad):
                kill_port(good)
                shutil.rmtree(self.copy / "run", ignore_errors=True)
                shutil.rmtree(self.copy / "logs", ignore_errors=True)
                done = subprocess.run(
                    [PY, "-B", str(self.copy / "xiaoba_monitor.py"), "--resolve-port", "--port", bad],
                    capture_output=True, text=True, cwd=str(self.copy), env=self.env(),
                )
                self.assertNotEqual(0, done.returncode, bad)
                proc = self.start(self.env(XIAOBA_MONITOR_PORT=bad))
                code = proc.wait(timeout=20)
                err = (proc.stderr.read() if proc.stderr else "") + (proc.stdout.read() if proc.stdout else "")
                self.assertNotEqual(0, code, bad)
                self.assertTrue("启动失败" in err or "无法解析" in err or "invalid" in err.lower() or "error" in err.lower())
                self.assert_no_start_artifacts()
                self.assertFalse(healthy(good))
                self.assertEqual(before_8799, set(listening_pids(FORBIDDEN_PORT)))

    def test_boolean_and_bad_json_config_fail_before_start(self):
        before_8799 = set(listening_pids(FORBIDDEN_PORT))
        cases = [
            ("bool", json.dumps({"port": True})),
            ("badjson", "{not json"),
        ]
        for label, text in cases:
            with self.subTest(label=label):
                (self.copy / "monitor.json").write_text(text)
                shutil.rmtree(self.copy / "run", ignore_errors=True)
                shutil.rmtree(self.copy / "logs", ignore_errors=True)
                done = self.resolve_port(config_file=self.copy / "monitor.json")
                self.assertNotEqual(0, done.returncode)
                proc = self.start(self.env())
                code = proc.wait(timeout=20)
                self.assertNotEqual(0, code)
                self.assert_no_start_artifacts()
                self.assertEqual(before_8799, set(listening_pids(FORBIDDEN_PORT)))

    def test_invalid_port_in_json_fails_before_start(self):
        self.write_monitor_json(999999)
        before = set(listening_pids(FORBIDDEN_PORT))
        done = self.resolve_port(config_file=self.copy / "monitor.json")
        self.assertNotEqual(0, done.returncode)
        self.assertIn("无法解析监看中心端口", done.stderr)
        proc = self.start(self.env())
        code = proc.wait(timeout=20)
        self.assertNotEqual(0, code)
        self.assert_no_start_artifacts()
        self.assertEqual(before, set(listening_pids(FORBIDDEN_PORT)))

    def test_detach_survives_caller_group_kill(self):
        port = self.alloc()
        self.write_monitor_json(port)
        caller = self.start(self.env(), keep_caller=True)
        self.assertTrue(self.wait_healthy(port))
        supervisor = self.read_supervisor()
        self.assertNotEqual(caller.pid, os.getsid(supervisor))
        self.assertNotEqual(caller.pid, os.getpgid(supervisor))
        os.killpg(caller.pid, signal.SIGKILL)
        self.assertEqual(-signal.SIGKILL, caller.wait(timeout=10))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not healthy(port):
            time.sleep(0.2)
        self.assertTrue(healthy(port))
        os.kill(supervisor, 0)

    def test_duplicate_start_while_listening_keeps_single_instance(self):
        port = self.alloc()
        self.write_monitor_json(port)
        first = self.start(self.env())
        self.assertTrue(self.wait_healthy(port))
        self.read_supervisor()
        first.wait(timeout=15)
        second = subprocess.run(
            ["/bin/bash", str(self.copy / "启动监看中心.command")],
            env=self.env(), capture_output=True, text=True, timeout=40,
        )
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertIn("http://127.0.0.1:%d/" % port, second.stdout)
        self.assertEqual(1, len(listening_pids(port)))

    def test_real_close_script_stops_temp_port(self):
        port = self.alloc()
        self.write_monitor_json(port)
        proc = self.start(self.env())
        self.assertTrue(self.wait_healthy(port))
        supervisor = self.read_supervisor()
        proc.wait(timeout=15)
        # 候选关闭脚本原文 + LSOF 包装器仅放行本轮临时端口
        done = self.close_script(self.env(allow_port=port))
        self.assertEqual(0, done.returncode, done.stderr + done.stdout)
        self.assertIn("小八监看中心已关闭", done.stdout)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and (healthy(port) or listening_pids(port)):
            time.sleep(0.2)
        self.assertFalse(healthy(port))
        self.assertEqual([], listening_pids(port))
        self.supervisor = None
        with self.assertRaises(ProcessLookupError):
            os.kill(supervisor, 0)

    def test_close_when_not_running_reports_closed(self):
        port = self.alloc()
        self.write_monitor_json(port)
        self.assertFalse(listening_pids(port))
        done = self.close_script(self.env(allow_port=port))
        self.assertEqual(0, done.returncode, done.stderr + done.stdout)
        self.assertIn("小八监看中心已关闭", done.stdout)

    def test_close_invalid_config_fails_before_kill(self):
        (self.copy / "monitor.json").write_text(json.dumps({"port": 0}))
        before_8799 = set(listening_pids(FORBIDDEN_PORT))
        # 放一个假 pid 文件：若错误地先杀，会动到这个标记
        run = self.copy / "run"
        run.mkdir(exist_ok=True)
        marker = run / "supervisor.pid"
        marker.write_text("1")  # pid 1 通常杀不掉；脚本若先 kill 会尝试
        done = self.close_script(self.env(allow_port=FORBIDDEN_PORT))  # allow 故意给 8799 包装器拒答
        self.assertNotEqual(0, done.returncode)
        self.assertIn("关闭失败", done.stderr)
        self.assertTrue(marker.exists())
        self.assertEqual(before_8799, set(listening_pids(FORBIDDEN_PORT)))


if __name__ == "__main__":
    unittest.main()
