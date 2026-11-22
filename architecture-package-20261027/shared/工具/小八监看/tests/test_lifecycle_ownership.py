"""监看启停归属：无关监听零信号、指纹、EPERM、旧 pid 迁移、停等不持锁。

仅用随机回环端口、自有临时目录/进程或记录型替身；不碰 8799、不改生产。
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

MONITOR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MONITOR))

from monitor_core import lifecycle  # noqa: E402
import xiaoba_monitor  # noqa: E402

PY = "/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14"
FORBIDDEN = 8799


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    if port == FORBIDDEN:
        return free_port()
    return port


class ArgvEntryPositionTests(unittest.TestCase):
    def test_python_c_data_containing_path_and_flags_is_not_match(self):
        root = MONITOR
        port = 54321
        entry = str(root / "xiaoba_monitor.py")
        # 路径与 --supervise/--port 都埋在 -c 代码字符串里，不得算执行入口。
        code = "print(%r); print('--supervise --port %d')" % (entry, port)
        argv = [sys.executable, "-c", code]
        kind, detail = lifecycle.classify_argv(argv, port, root, role=lifecycle.ROLE_SUPERVISE)
        self.assertEqual(kind, lifecycle.KIND_STALE)
        self.assertIn("执行位置", detail)

    def test_real_entry_position_with_port_and_supervise_matches(self):
        root = MONITOR
        port = 54321
        argv = [sys.executable, "-B", str(root / "xiaoba_monitor.py"), "--supervise", "--port", str(port)]
        kind, detail = lifecycle.classify_argv(argv, port, root, role=lifecycle.ROLE_SUPERVISE)
        self.assertEqual((kind, detail), (lifecycle.KIND_MATCH, ""))

    def test_same_pid_alone_is_not_enough_without_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            port = free_port()
            token, err = lifecycle.acquire_claim(run_dir, MONITOR, port, lifecycle.ROLE_SERVE, pid=os.getpid())
            self.assertIsNone(err, err)
            claim = lifecycle.read_claim(run_dir)
            self.assertIsInstance(claim.get("fingerprint"), dict)
            bad_fp = dict(claim["fingerprint"])
            if bad_fp.get("kind") == "darwin_bsdinfo":
                bad_fp["start_tvusec"] = int(bad_fp["start_tvusec"]) ^ 0xFFFFFF
            else:
                bad_fp["starttime"] = int(bad_fp["starttime"]) + 1
            # 入口 argv 用补丁放行，只检验指纹不一致时不得 match。
            with mock.patch.object(lifecycle, "classify_argv", return_value=(lifecycle.KIND_MATCH, "")):
                kind, detail = lifecycle.classify_process(
                    os.getpid(), port, MONITOR, role=lifecycle.ROLE_SERVE, expected_fingerprint=bad_fp,
                )
            self.assertEqual(kind, lifecycle.KIND_STALE)
            self.assertIn("指纹", detail)
            lifecycle.release_claim(run_dir, token)


class SignalRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, pid, sig):
        self.calls.append((int(pid), int(sig)))
        # 默认不真正发信号；由用例决定是否转调真实 kill。
        raise AssertionError("测试替身未配置放行，拒绝向 pid %s 发信号" % pid)


class UnrelatedListenerTests(unittest.TestCase):
    def test_stop_owned_never_signals_foreign_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            port = free_port()
            server = socket.socket()
            self.addCleanup(server.close)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", port))
            server.listen(1)
            recorder = SignalRecorder()
            result = lifecycle.stop_owned(run_dir, MONITOR, port, kill=recorder, wait_rounds=1)
            self.assertEqual(result["signals"], 0)
            self.assertEqual(recorder.calls, [])
            self.assertIn(result["status"], ("already_stopped", lifecycle.STATUS_LEGACY_UNCLAIMED))


class EpermTests(unittest.TestCase):
    def test_probe_alive_treats_eperm_as_unknown_not_stale(self):
        def boom(pid, sig):
            raise OSError(errno.EPERM, "Operation not permitted")

        with mock.patch("os.kill", boom):
            kind, detail = lifecycle.probe_alive(12345)
        self.assertEqual(kind, lifecycle.KIND_UNKNOWN)
        self.assertIn("EPERM", detail)

    def test_stop_owned_eperm_on_signal_keeps_zero_success_and_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            port = free_port()
            token, err = lifecycle.acquire_claim(run_dir, MONITOR, port, lifecycle.ROLE_SERVE, pid=os.getpid())
            self.assertIsNone(err)

            def deny(pid, sig):
                raise PermissionError("denied")

            # classify_claim 会对当前进程做 argv 匹配；本测试进程不是 xiaoba_monitor 入口，
            # 会先得到 stale。改为补丁 classify 路径验证 EPERM 分支。
            claim = lifecycle.read_claim(run_dir)
            with mock.patch.object(lifecycle, "classify_claim", return_value=(lifecycle.KIND_MATCH, "")), \
                    mock.patch.object(lifecycle, "revalidate_before_signal", return_value=(lifecycle.KIND_MATCH, "")):
                result = lifecycle.stop_owned(run_dir, MONITOR, port, kill=deny, wait_rounds=1)
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["signals"], 0)
            # EPERM 后应保留 claim
            self.assertEqual(lifecycle.read_claim(run_dir).get("token"), token)
            lifecycle.release_claim(run_dir, token)


class LegacyMigrationTests(unittest.TestCase):
    def test_old_supervisor_pid_without_claim_is_not_reported_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            port = free_port()
            # 用一个仍存活的无关 PID 冒充旧监管文件。
            (run_dir / lifecycle.PID_NAME).write_text(str(os.getpid()), encoding="utf-8")
            self.assertIsNone(lifecycle.read_claim(run_dir))
            legacy = lifecycle.inspect_legacy_pid_file(run_dir, MONITOR, port)
            self.assertEqual(legacy["status"], lifecycle.STATUS_LEGACY_UNCLAIMED)
            self.assertIn("lifecycle.claim", legacy["migration"])
            self.assertIn("monitor_core/lifecycle.py", legacy["migration"])
            recorder = SignalRecorder()
            result = lifecycle.stop_owned(run_dir, MONITOR, port, kill=recorder, wait_rounds=1)
            self.assertEqual(result["status"], lifecycle.STATUS_LEGACY_UNCLAIMED)
            self.assertEqual(result["signals"], 0)
            self.assertEqual(recorder.calls, [])
            self.assertTrue((run_dir / lifecycle.PID_NAME).exists(), "不得擅自删除旧 pid 冒充已关闭")
            # 启动认领也应拒绝自动接管仍存活的裸 PID
            token, err = lifecycle.acquire_claim(run_dir, MONITOR, port, lifecycle.ROLE_SUPERVISE)
            self.assertIsNone(token)
            self.assertIsNotNone(err)
            self.assertIn("lifecycle.claim", err[1])

    def test_close_script_prints_migration_for_legacy_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            copy = root / "monitor"
            shutil.copytree(
                MONITOR, copy,
                ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", "run", "logs", "node_modules", "tests", "小八3D导演台",
                ),
            )
            port = free_port()
            (copy / "monitor.json").write_text(json.dumps({"port": port}))
            run = copy / "run"
            run.mkdir()
            (run / "supervisor.pid").write_text(str(os.getpid()))
            lsof = root / "lsof"
            lsof.write_text("#!/bin/bash\nexit 1\n")
            lsof.chmod(0o755)
            env = dict(os.environ, XIAOBA_LSOF=str(lsof))
            env.pop("XIAOBA_MONITOR_PORT", None)
            done = subprocess.run(
                ["/bin/bash", str(copy / "关闭监看中心.command")],
                capture_output=True, text=True, timeout=60, env=env,
            )
            self.assertNotEqual(0, done.returncode)
            self.assertNotIn("小八监看中心已关闭", done.stdout)
            combined = (done.stderr + done.stdout).lower()
            self.assertTrue(
                ("legacy" in combined) or ("lifecycle.claim" in combined) or ("supervisor.pid" in done.stderr),
                done.stderr,
            )
            self.assertIn("lifecycle.claim", done.stderr)


class ZombieProbeTests(unittest.TestCase):
    def test_unreaped_zombie_is_stale_not_alive(self):
        """父进程未 wait 时 kill(0) 仍成功；probe_alive 必须把僵尸判为已退出。"""
        child = subprocess.Popen([PY, "-B", "-c", "import time; time.sleep(0.15)"])
        self.addCleanup(lambda: child.poll() is None and child.kill() or child.wait(timeout=5))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            # 不调用 poll/wait，保留僵尸
            try:
                os.kill(child.pid, 0)
            except ProcessLookupError:
                self.fail("子进程过早从 pid 表消失")
            state = subprocess.run(
                ["/bin/ps", "-p", str(child.pid), "-o", "state="],
                capture_output=True, text=True,
            ).stdout.strip()
            if state.startswith("Z"):
                break
            time.sleep(0.05)
        else:
            self.fail("未观察到僵尸状态")
        kind, detail = lifecycle.probe_alive(child.pid)
        self.assertEqual(kind, lifecycle.KIND_STALE, detail)
        self.assertIn("僵尸", detail)
        child.wait(timeout=5)


class LockReleaseBeforeWaitTests(unittest.TestCase):
    def test_stop_owned_does_not_hold_lock_while_waiting_after_signal(self):
        """反例①：等待 PID 退出时不得仍持 lifecycle.lock，否则目标 finally→release_claim 会死锁。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            port = free_port()
            fp, fp_detail = lifecycle.read_process_fingerprint(os.getpid())
            self.assertIsNotNone(fp, fp_detail)
            token = "b" * 32
            lifecycle.write_claim_atomic(run_dir, {
                "pid": os.getpid(),
                "port": port,
                "role": lifecycle.ROLE_SERVE,
                "token": token,
                "entry": str(MONITOR / "xiaoba_monitor.py"),
                "entry_resolved": str((MONITOR / "xiaoba_monitor.py").resolve()),
                "fingerprint": fp,
                "claimed_at": time.time(),
            })
            import fcntl as fcntl_mod

            lock_held_during_wait = []
            signal_count = {"n": 0}
            raise_exit = [False]

            def kill_stub(pid, sig):
                signal_count["n"] += 1
                # 发信号瞬间允许仍持锁；关键是随后的 wait/sleep 不得持锁。

            def sleep_stub(seconds):
                # 非阻塞探测：若锁仍被 stop_owned 持有，这里会拿不到 LOCK_EX|LOCK_NB。
                path = lifecycle.lock_path(run_dir)
                fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
                try:
                    try:
                        fcntl_mod.flock(fd, fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
                    except OSError:
                        lock_held_during_wait.append(True)
                    else:
                        lock_held_during_wait.append(False)
                        fcntl_mod.flock(fd, fcntl_mod.LOCK_UN)
                finally:
                    os.close(fd)
                # 第一次 wait 就把目标标成已退出，避免长时间循环。
                raise_exit[0] = True

            def probe_stub(pid):
                if raise_exit[0]:
                    return lifecycle.KIND_STALE, "gone"
                return "alive", ""

            with mock.patch.object(lifecycle, "classify_claim", return_value=(lifecycle.KIND_MATCH, "")), \
                    mock.patch.object(lifecycle, "revalidate_before_signal", return_value=(lifecycle.KIND_MATCH, "")), \
                    mock.patch.object(lifecycle, "probe_alive", side_effect=probe_stub):
                result = lifecycle.stop_owned(
                    run_dir, MONITOR, port, kill=kill_stub, sleep=sleep_stub, wait_rounds=5,
                )
            self.assertGreaterEqual(signal_count["n"], 1)
            self.assertTrue(lock_held_during_wait, "应进入过等待路径")
            self.assertFalse(any(lock_held_during_wait), "等待期间仍持有 lifecycle.lock：%s" % lock_held_during_wait)
            self.assertEqual(result["status"], "stopped", result)
            # token 清理在重新持锁后执行
            self.assertIsNone(lifecycle.read_claim(run_dir))

    def test_target_release_claim_can_run_between_signal_and_cleanup(self):
        """发信号后放锁，目标得以 release_claim；父进程异步收割，避免僵尸让 kill(0) 假活。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            port = free_port()
            ready = Path(tmp) / "ready"
            done = Path(tmp) / "released"
            child_src = r"""
import os, signal, sys, time
sys.path.insert(0, %r)
from monitor_core import lifecycle
run_dir = %r
token = open(%r).read().strip()
open(%r, "w").write("1")
stopping = []
def handle(signum, frame):
    stopping.append(1)
signal.signal(signal.SIGTERM, handle)
while not stopping:
    time.sleep(0.05)
ok = lifecycle.release_claim(run_dir, token)
open(%r, "w").write("1" if ok else "0")
""" % (str(MONITOR), str(run_dir), str(Path(tmp) / "token.txt"), str(ready), str(done))
            token = "d" * 32
            (Path(tmp) / "token.txt").write_text(token)
            proc = subprocess.Popen([PY, "-B", "-c", child_src])
            self.addCleanup(lambda: proc.poll() is None and proc.kill() or None)
            # 异步收割：模拟非测试夹具下 init/父进程及时 wait，不把判定放宽成“忽略僵尸”。
            reaped = []

            def _reaper():
                try:
                    reaped.append(proc.wait(timeout=60))
                except Exception as exc:  # noqa: BLE001 — 测试收割线程
                    reaped.append(exc)

            threading.Thread(target=_reaper, daemon=True).start()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not ready.exists():
                time.sleep(0.05)
            self.assertTrue(ready.exists())
            fp, fp_detail = lifecycle.read_process_fingerprint(proc.pid)
            self.assertIsNotNone(fp, fp_detail)
            lifecycle.write_claim_atomic(run_dir, {
                "pid": proc.pid,
                "port": port,
                "role": lifecycle.ROLE_SERVE,
                "token": token,
                "entry": str(MONITOR / "xiaoba_monitor.py"),
                "entry_resolved": str((MONITOR / "xiaoba_monitor.py").resolve()),
                "fingerprint": fp,
                "claimed_at": time.time(),
            })

            with mock.patch.object(lifecycle, "classify_claim", return_value=(lifecycle.KIND_MATCH, "")), \
                    mock.patch.object(lifecycle, "revalidate_before_signal", return_value=(lifecycle.KIND_MATCH, "")):
                result = lifecycle.stop_owned(
                    run_dir, MONITOR, port, kill=os.kill, sleep=time.sleep, wait_rounds=40,
                )
            self.assertEqual(result["status"], "stopped", result)
            self.assertTrue(done.exists(), "子进程未跑完 release_claim")
            self.assertEqual(done.read_text().strip(), "1", "release_claim 应在放锁后成功")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not reaped:
                time.sleep(0.05)
            self.assertTrue(reaped, "异步收割未完成")
            self.assertEqual(reaped[0], 0)
            self.assertIsNone(lifecycle.read_claim(run_dir))


class ConcurrentClaimTests(unittest.TestCase):
    def test_second_acquire_while_first_holds_match_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            port = free_port()
            # 用真实入口进程当 supervise 目标较重；这里构造 claim + 补丁 classify 为 match。
            fp, fp_detail = lifecycle.read_process_fingerprint(os.getpid())
            self.assertIsNotNone(fp, fp_detail)
            token = "a" * 32
            lifecycle.write_claim_atomic(run_dir, {
                "pid": os.getpid(),
                "port": port,
                "role": lifecycle.ROLE_SUPERVISE,
                "token": token,
                "entry": str(MONITOR / "xiaoba_monitor.py"),
                "entry_resolved": str((MONITOR / "xiaoba_monitor.py").resolve()),
                "fingerprint": fp,
                "claimed_at": time.time(),
            })
            with mock.patch.object(lifecycle, "classify_claim", return_value=(lifecycle.KIND_MATCH, "")):
                other, err2 = lifecycle.acquire_claim(run_dir, MONITOR, port, lifecycle.ROLE_SUPERVISE)
            self.assertIsNone(other)
            self.assertIsNotNone(err2)
            self.assertEqual(err2[0], 2)
            self.assertIn("已在运行", err2[1])
            lifecycle.release_claim(run_dir, token)


class RealServiceStartStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.copy = self.root / "monitor"
        shutil.copytree(
            MONITOR, self.copy,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "run", "logs", "node_modules", "tests", "小八3D导演台",
            ),
        )
        self.port = free_port()
        self.review_port = free_port()
        (self.copy / "monitor.json").write_text(json.dumps({
            "port": self.port,
            "agentos_project": str(self.root / "agentos-project"),
            "agentos_config": str(self.root / "agentos.json"),
            "review": {"port": self.review_port, "start_timeout_seconds": 5},
            "keypool": {
                "port": free_port(),
                "file": str(self.root / "keypool.json"),
                "usage_dir": str(self.root / "keypool-usage"),
            },
            "localtools": {"port": free_port(), "script": str(self.root / "missing-local-tools.py")},
            "stage": {"dir": str(self.root / "dist"), "library_dir": str(self.root / "library")},
            "taskcards": {"dirs": [], "ledger": str(self.root / "taskcards.jsonl"), "receipt_root": str(self.root)},
            "log": {"dir": str(self.root / "logs")},
        }))
        (self.root / "agentos.json").write_text("{}")
        self.supervisor = None

    def tearDown(self):
        if self.supervisor:
            try:
                os.kill(self.supervisor, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.tmp.cleanup()

    def test_supervised_start_and_owned_close(self):
        env = dict(os.environ, XIAOBA_MONITOR_NO_BROWSER="1")
        env.pop("XIAOBA_MONITOR_PORT", None)
        lsof = self.root / "lsof"
        lsof.write_text(
            "#!/bin/bash\n"
            "allowed=%d\n"
            "port=\"\"\n"
            "for a in \"$@\"; do case \"$a\" in -tiTCP:*|-iTCP:*) port=\"${a##*:}\"; port=\"${port%%[^0-9]*}\";; esac; done\n"
            "if [ \"$port\" != \"$allowed\" ]; then exit 1; fi\n"
            "exec /usr/sbin/lsof \"$@\"\n" % self.port
        )
        lsof.chmod(0o755)
        env["XIAOBA_LSOF"] = str(lsof)
        start = subprocess.run(
            ["/bin/bash", str(self.copy / "启动监看中心.command")],
            capture_output=True, text=True, timeout=60, env=env, cwd=str(self.copy),
        )
        self.assertEqual(0, start.returncode, start.stderr + start.stdout)
        pid_file = self.copy / "run" / "supervisor.pid"
        self.assertTrue(pid_file.is_file())
        self.supervisor = int(pid_file.read_text().strip())
        claim = lifecycle.read_claim(self.copy / "run")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.get("role"), lifecycle.ROLE_SUPERVISE)
        self.assertIsInstance(claim.get("fingerprint"), dict)
        close = subprocess.run(
            ["/bin/bash", str(self.copy / "关闭监看中心.command")],
            capture_output=True, text=True, timeout=60, env=env, cwd=str(self.copy),
        )
        self.assertEqual(0, close.returncode, close.stderr + close.stdout)
        self.assertIn("小八监看中心已关闭", close.stdout)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                os.kill(self.supervisor, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        with self.assertRaises(ProcessLookupError):
            os.kill(self.supervisor, 0)
        self.supervisor = None
        self.assertIsNone(lifecycle.read_claim(self.copy / "run"))


class ImplicitPortClaimTests(unittest.TestCase):
    def test_config_default_port_without_cli_port_matches_with_fingerprint(self):
        """direct CLI 仅 --config-file / 配置默认端口：无显式 --port 时，凭 claim 指纹仍可识别。"""
        root = MONITOR
        port = 54321
        argv = [sys.executable, "-B", str(root / "xiaoba_monitor.py"), "--config-file", "/tmp/m.json"]
        # 无指纹：不得仅凭入口 match（避免把默认端口实例误判到任意查询端口）
        kind_strict, detail_strict = lifecycle.classify_argv(
            argv, port, root, role=lifecycle.ROLE_SERVE, require_explicit_port=True,
        )
        self.assertEqual(kind_strict, lifecycle.KIND_UNKNOWN)
        self.assertIn("--port", detail_strict)
        # 有指纹路径：允许隐式端口
        kind_soft, detail_soft = lifecycle.classify_argv(
            argv, port, root, role=lifecycle.ROLE_SERVE, require_explicit_port=False,
        )
        self.assertEqual((kind_soft, detail_soft), (lifecycle.KIND_MATCH, ""))
        fp, fp_detail = lifecycle.read_process_fingerprint(os.getpid())
        self.assertIsNotNone(fp, fp_detail)
        with mock.patch.object(lifecycle, "read_process_argv", return_value=(argv, "")), \
                mock.patch.object(lifecycle, "probe_alive", return_value=("alive", "")):
            kind, detail = lifecycle.classify_process(
                os.getpid(), port, root, role=lifecycle.ROLE_SERVE, expected_fingerprint=fp,
            )
        self.assertEqual((kind, detail), (lifecycle.KIND_MATCH, ""))


class HelperSyncContractTests(unittest.TestCase):
    def test_lifecycle_helper_is_part_of_monitor_package(self):
        path = MONITOR / "monitor_core" / "lifecycle.py"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("发版后置同步须带上本模块", text)
        self.assertIn("禁止改用", text)
        self.assertIn("旧关闭脚本", text)
        close = (MONITOR / "关闭监看中心.command").read_text(encoding="utf-8")
        self.assertIn("monitor_core/lifecycle.py", close)
        migration = lifecycle.LEGACY_MIGRATION
        self.assertNotIn("当时安装的关闭脚本", migration)
        self.assertIn("禁止改用", migration)


if __name__ == "__main__":
    unittest.main()
