"""B09–B11：旧监管预检、受控迁移停止、关闭报告（API 在 lifecycle.py）。

合成进程 + 随机回环端口；不碰 8799 / 生产 / 真实 r9 关闭脚本。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

MONITOR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MONITOR))

from monitor_core import lifecycle  # noqa: E402

PY = "/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14"
FORBIDDEN = 8799


def free_port() -> int:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    if port == FORBIDDEN:
        return free_port()
    return port


def copy_cli_entry(directory: Path) -> Path:
    entry = directory / "xiaoba_monitor.py"
    shutil.copy2(MONITOR / entry.name, entry)
    shutil.copytree(MONITOR / "monitor_core", directory / "monitor_core",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return entry


STUB = r'''
import argparse, os, signal, socket, sys, time
from pathlib import Path

def serve_child(port, ready):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    srv.settimeout(0.5)
    Path(ready).write_text(str(os.getpid()), encoding="utf-8")
    stop = False
    def _s(s, f):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, _s)
    while not stop:
        try:
            c, _ = srv.accept(); c.close()
        except socket.timeout:
            pass
    srv.close()

def supervise(port, run_dir, ready):
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    pid_file = Path(run_dir) / "supervisor.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    child = os.fork()
    if child == 0:
        os.execv(sys.executable, [sys.executable, "-B", str(Path(__file__).resolve()),
                                  "--port", str(port), "--child", "--child-ready", ready])
    stop = False
    def _s(s, f):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, _s)
    while not stop:
        time.sleep(0.05)
    try:
        os.kill(child, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(child, 0)
    except ChildProcessError:
        pass
    try:
        if pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pid_file.unlink()
    except OSError:
        pass

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--supervise", action="store_true")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--child", action="store_true")
    p.add_argument("--run-dir", default="")
    p.add_argument("--child-ready", default="")
    a = p.parse_args()
    if a.port == 8799:
        return 2
    if a.child:
        serve_child(a.port, a.child_ready); return 0
    if a.supervise:
        supervise(a.port, a.run_dir, a.child_ready); return 0
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
'''


def wait_file(path: Path, seconds: float = 8.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            return True
        time.sleep(0.05)
    return False


def wait_gone(pid: int, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        # zombie
        st = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "state="], capture_output=True, text=True)
        if (st.stdout or "").strip().startswith("Z"):
            return True
        time.sleep(0.05)
    return False


class SyntheticLegacyScene:
    """自建监管/子进程；root = 含 xiaoba_monitor.py 的 stubs 目录。"""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "stubs"
        self.root.mkdir(parents=True)
        self.entry = self.root / "xiaoba_monitor.py"
        self.entry.write_text(STUB, encoding="utf-8")
        self.run_dir = Path(self.tmp.name) / "run"
        self.run_dir.mkdir()
        self.ready = Path(self.tmp.name) / "child.ready"
        self.port = free_port()
        self.sup = None
        self.child_pid = None

    def start(self):
        self.sup = subprocess.Popen(
            [PY, "-B", str(self.entry), "--supervise", "--port", str(self.port),
             "--run-dir", str(self.run_dir), "--child-ready", str(self.ready)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if not wait_file(self.run_dir / "supervisor.pid"):
            raise RuntimeError("supervisor.pid 未写出")
        if not wait_file(self.ready):
            raise RuntimeError("child 未就绪")
        self.child_pid = int(self.ready.read_text(encoding="utf-8").strip())
        return self

    def cleanup(self):
        if self.sup is not None and self.sup.poll() is None:
            try:
                os.kill(self.sup.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.sup.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.sup.pid, signal.SIGKILL)
                self.sup.wait(timeout=3)
        if self.child_pid:
            wait_gone(self.child_pid, 3)
            try:
                os.kill(self.child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.tmp.cleanup()


class B09PreflightTests(unittest.TestCase):
    def test_missing_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            port = free_port()
            r = lifecycle.preflight_legacy_migration(run_dir, MONITOR, port, write_binding=False)
            self.assertEqual(r["status"], lifecycle.STATUS_MISSING_PID)
            self.assertIsNone(r["binding"])

    def test_stale_pid_or_port_idle_when_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            port = free_port()
            (run_dir / "supervisor.pid").write_text("99999999", encoding="utf-8")
            # 端口空闲时单独表达 port_idle，不与归属绑定混用
            r = lifecycle.preflight_legacy_migration(run_dir, MONITOR, port, write_binding=False)
            self.assertIn(r["status"], (lifecycle.STATUS_STALE_PID, lifecycle.STATUS_PORT_IDLE), r)
            self.assertIsNone(r["binding"])
            r2 = lifecycle.preflight_legacy_migration(
                run_dir, MONITOR, port, write_binding=False,
                listen_pids=lambda p: (_ for _ in ()).throw(OSError("no lsof")),
            )
            self.assertEqual(r2["status"], lifecycle.STATUS_STALE_PID)

    def test_pid_reused_unrelated_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            port = free_port()
            (run_dir / "supervisor.pid").write_text(str(os.getpid()), encoding="utf-8")
            r = lifecycle.preflight_legacy_migration(run_dir, MONITOR, port, write_binding=False)
            self.assertEqual(r["status"], lifecycle.STATUS_PID_REUSED_OR_MISMATCH)
            self.assertIsNone(r["binding"])

    def test_has_claim_refuses_migration_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            port = free_port()
            token, err = lifecycle.acquire_claim(run_dir, MONITOR, port, lifecycle.ROLE_SERVE, pid=os.getpid())
            self.assertIsNone(err)
            r = lifecycle.preflight_legacy_migration(run_dir, MONITOR, port, write_binding=False)
            self.assertEqual(r["status"], lifecycle.STATUS_HAS_CLAIM)
            lifecycle.release_claim(run_dir, token)

    def test_corrupt_claim_blocks_bare_pid_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            port = free_port()
            (run_dir / "lifecycle.claim").write_text("{not-json", encoding="utf-8")
            (run_dir / "supervisor.pid").write_text(str(os.getpid()), encoding="utf-8")
            r = lifecycle.preflight_legacy_migration(run_dir, MONITOR, port, write_binding=False)
            self.assertEqual(r["status"], lifecycle.STATUS_CLAIM_CORRUPT)
            self.assertIsNone(r["binding"])
            stop = lifecycle.migration_stop(run_dir, MONITOR, port, {"schema": lifecycle.BINDING_SCHEMA})
            self.assertEqual(stop["status"], "rejected")
            self.assertEqual(stop["signals"], 0)

    def test_bound_identity_and_listen_not_signal_target(self):
        scene = SyntheticLegacyScene().start()
        self.addCleanup(scene.cleanup)
        foreign = 4194303
        r = lifecycle.preflight_legacy_migration(
            scene.run_dir, scene.root, scene.port,
            listen_pids=lambda p: [scene.child_pid],
        )
        self.assertEqual(r["status"], lifecycle.STATUS_BOUND, r)
        b = r["binding"]
        self.assertEqual(b["schema"], lifecycle.BINDING_SCHEMA)
        self.assertEqual(b["pid"], scene.sup.pid)
        self.assertEqual(b["signal_targets"], [scene.sup.pid])
        self.assertNotIn(foreign, b["signal_targets"])
        self.assertTrue(lifecycle._fingerprint_complete(b["fingerprint"]))
        self.assertEqual(b["issuer"]["module"], "monitor_core.lifecycle")
        self.assertEqual(Path(b["run_dir"]), scene.run_dir.resolve())
        self.assertTrue(any(c["pid"] == scene.child_pid for c in b["children"]))
        self.assertTrue((scene.run_dir / lifecycle.BINDING_NAME).is_file())
        # 监听不可观测 → 不签发可停凭证
        r2 = lifecycle.preflight_legacy_migration(
            scene.run_dir, scene.root, scene.port,
            listen_pids=lambda p: (_ for _ in ()).throw(OSError("lsof down")),
            write_binding=False,
        )
        self.assertEqual(r2["status"], lifecycle.STATUS_INCOMPLETE_IDENTITY, r2)
        self.assertIsNone(r2["binding"])
        # 端口空闲 → port_idle，不是 bound
        r3 = lifecycle.preflight_legacy_migration(
            scene.run_dir, scene.root, scene.port,
            listen_pids=lambda p: [],
            write_binding=False,
        )
        self.assertEqual(r3["status"], lifecycle.STATUS_PORT_IDLE, r3)
        self.assertIsNone(r3["binding"])

    def test_incomplete_when_foreign_listener_on_owned_port(self):
        scene = SyntheticLegacyScene().start()
        self.addCleanup(scene.cleanup)
        r = lifecycle.preflight_legacy_migration(
            scene.run_dir, scene.root, scene.port,
            listen_pids=lambda p: [4194303],
            write_binding=False,
        )
        self.assertEqual(r["status"], lifecycle.STATUS_INCOMPLETE_IDENTITY)
        self.assertIsNone(r["binding"])


class B10MigrationStopTests(unittest.TestCase):
    def test_rejects_without_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = lifecycle.migration_stop(Path(tmp), MONITOR, free_port(), {"schema": "nope"})
            self.assertEqual(r["status"], "rejected")
            self.assertEqual(r["signals"], 0)

    def test_rejects_expanded_signal_targets(self):
        scene = SyntheticLegacyScene().start()
        self.addCleanup(scene.cleanup)
        pre = lifecycle.preflight_legacy_migration(scene.run_dir, scene.root, scene.port)
        self.assertEqual(pre["status"], lifecycle.STATUS_BOUND)
        bad = dict(pre["binding"])
        bad["signal_targets"] = [scene.sup.pid, scene.child_pid]
        r = lifecycle.migration_stop(scene.run_dir, scene.root, scene.port, bad, wait_rounds=2)
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["signals"], 0)
        # 进程仍在
        os.kill(scene.sup.pid, 0)

    def test_controlled_stop_signals_only_bound_supervisor(self):
        scene = SyntheticLegacyScene().start()
        self.addCleanup(scene.cleanup)
        pre = lifecycle.preflight_legacy_migration(scene.run_dir, scene.root, scene.port)
        self.assertEqual(pre["status"], lifecycle.STATUS_BOUND, pre)
        recorded = []

        def kill_rec(pid, sig):
            recorded.append((pid, sig))
            return os.kill(pid, sig)

        r = lifecycle.migration_stop(
            scene.run_dir, scene.root, scene.port, pre["binding"],
            kill=kill_rec, wait_rounds=40,
        )
        self.assertEqual(r["status"], "stopped", r)
        self.assertEqual(r["signals"], 1)
        self.assertEqual([p for p, _ in recorded], [scene.sup.pid])
        self.assertTrue(wait_gone(scene.sup.pid))
        self.assertTrue(wait_gone(scene.child_pid))
        self.assertFalse((scene.run_dir / "supervisor.pid").exists())
        self.assertFalse((scene.run_dir / lifecycle.BINDING_NAME).exists())

    def test_does_not_widen_stop_owned_for_legacy_pid(self):
        scene = SyntheticLegacyScene().start()
        self.addCleanup(scene.cleanup)
        # 普通 stop_owned 仍拒绝裸 pid
        r = lifecycle.stop_owned(scene.run_dir, scene.root, scene.port, wait_rounds=1)
        self.assertEqual(r["status"], lifecycle.STATUS_LEGACY_UNCLAIMED)
        self.assertEqual(r["signals"], 0)
        os.kill(scene.sup.pid, 0)

    def test_wrong_run_dir_or_empty_fingerprint_zero_signals(self):
        scene = SyntheticLegacyScene().start()
        self.addCleanup(scene.cleanup)
        pre = lifecycle.preflight_legacy_migration(scene.run_dir, scene.root, scene.port)
        self.assertEqual(pre["status"], lifecycle.STATUS_BOUND, pre)
        bad_run = dict(pre["binding"])
        bad_run["run_dir"] = str(Path(scene.run_dir).parent / "other-run")
        r1 = lifecycle.migration_stop(scene.run_dir, scene.root, scene.port, bad_run, wait_rounds=1)
        self.assertEqual(r1["status"], "rejected")
        self.assertEqual(r1["signals"], 0)
        bad_fp = dict(pre["binding"])
        bad_fp["fingerprint"] = {"kind": "darwin_bsdinfo"}
        r2 = lifecycle.migration_stop(scene.run_dir, scene.root, scene.port, bad_fp, wait_rounds=1)
        self.assertEqual(r2["status"], "rejected")
        self.assertEqual(r2["signals"], 0)
        os.kill(scene.sup.pid, 0)

    def test_supervisor_gone_child_alive_is_partial(self):
        scene = SyntheticLegacyScene().start()
        self.addCleanup(scene.cleanup)
        pre = lifecycle.preflight_legacy_migration(scene.run_dir, scene.root, scene.port)
        self.assertEqual(pre["status"], lifecycle.STATUS_BOUND, pre)
        # 只杀监管，保留子进程（模拟目标 PID 已退、绑定子进程仍活）
        os.kill(scene.sup.pid, signal.SIGKILL)
        self.assertTrue(wait_gone(scene.sup.pid))
        # 阻止监管的 SIGTERM 收割逻辑：子进程可能成孤儿仍监听
        r = lifecycle.migration_stop(
            scene.run_dir, scene.root, scene.port, pre["binding"], wait_rounds=8,
        )
        self.assertIn(r["status"], ("partial", "already_stopped"), r)
        self.assertEqual(r["signals"], 0)
        if r["status"] == "partial":
            self.assertTrue(any(v == "survived" for v in (r.get("children") or {}).values()), r)
            self.assertIn("cleanup_evidence", r)
        # 夹具收尾子进程
        try:
            os.kill(scene.child_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def test_cli_missing_binding_json_no_traceback(self):
        port = free_port()
        with tempfile.TemporaryDirectory() as tmp:
            scene = Path(tmp)
            entry = copy_cli_entry(scene)
            done = subprocess.run(
                [PY, "-B", str(entry), "--migration-stop", "--port", str(port),
                 "--binding-file", str(scene / "missing-binding.json")],
                capture_output=True, text=True, cwd=scene,
            )
        self.assertNotIn("Traceback", done.stderr)
        self.assertNotIn("Traceback", done.stdout)
        line = [ln for ln in done.stdout.splitlines() if ln.startswith("{")][0]
        payload = json.loads(line)
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["signals"], 0)
        self.assertNotEqual(done.returncode, 0)

    def _forged_binding(self, run_dir: Path, root: Path, port: int, pid: int, fingerprint: dict) -> dict:
        bid = "c08f1%024x" % (pid & 0xFFFFFFFF)
        entry = lifecycle.entry_path(root)
        return {
            "schema": lifecycle.BINDING_SCHEMA,
            "binding_id": bid,
            "pid": pid,
            "port": port,
            "role": lifecycle.ROLE_SUPERVISE,
            "root": str(root.resolve()),
            "run_dir": str(run_dir.resolve()),
            "entry": str(entry),
            "entry_resolved": str(entry.resolve()),
            "entry_index": 1,
            "argv": [sys.executable, "-B", str(entry), "--supervise", "--port", str(port)],
            "fingerprint": fingerprint,
            "children": [],
            "bound_at": time.time(),
            "issuer": {
                "module": "monitor_core.lifecycle",
                "api": "preflight_legacy_migration",
                "binding_id": bid,
                "schema": lifecycle.BINDING_SCHEMA,
            },
            "signal_targets": [pid],
            "listen_diagnostic_at_bind": {"port": port, "pids": [], "ok": True},
        }

    def test_c08f1_live_wrong_identity_rejected_keeps_pid_file(self):
        """C08-F1：字段合法但 PID 指向无关活进程 → rejected，零信号，保留 supervisor.pid。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            port = free_port()
            unrelated = subprocess.Popen(
                [PY, "-B", "-c", "import time; time.sleep(60)"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            def _reap_unrelated():
                if unrelated.poll() is None:
                    try:
                        os.killpg(unrelated.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        try:
                            unrelated.kill()
                        except ProcessLookupError:
                            pass
                try:
                    unrelated.wait(timeout=5)
                except Exception:
                    pass

            self.addCleanup(_reap_unrelated)
            time.sleep(0.15)
            fp, err = lifecycle.read_process_fingerprint(unrelated.pid)
            self.assertIsNotNone(fp, err)
            self.assertTrue(lifecycle._fingerprint_complete(fp))
            (run_dir / "supervisor.pid").write_text(str(unrelated.pid), encoding="utf-8")
            forged = self._forged_binding(run_dir, MONITOR, port, unrelated.pid, fp)
            recorded = []

            def deny_kill(pid, sig):
                recorded.append((pid, sig))
                raise AssertionError("不得向无关活进程发信号")

            r = lifecycle.migration_stop(
                run_dir, MONITOR, port, forged, kill=deny_kill, wait_rounds=2,
            )
            self.assertEqual(r["status"], "rejected", r)
            self.assertEqual(r["signals"], 0)
            self.assertEqual(recorded, [])
            self.assertEqual(
                (run_dir / "supervisor.pid").read_text(encoding="utf-8").strip(),
                str(unrelated.pid),
            )
            self.assertEqual(r.get("cleaned") or [], [])
            os.kill(unrelated.pid, 0)

    def test_c08f1_true_exit_still_already_stopped_and_cleans(self):
        """真实已退出：仍 already_stopped，允许清理裸 pid（合法重入）。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            port = free_port()
            dead_pid = 99999999
            (run_dir / "supervisor.pid").write_text(str(dead_pid), encoding="utf-8")
            fp = {"kind": "darwin_bsdinfo", "pid": dead_pid, "start_tvsec": 1, "start_tvusec": 2}
            forged = self._forged_binding(run_dir, MONITOR, port, dead_pid, fp)
            r = lifecycle.migration_stop(run_dir, MONITOR, port, forged, wait_rounds=2)
            self.assertEqual(r["status"], "already_stopped", r)
            self.assertEqual(r["signals"], 0)
            self.assertFalse((run_dir / "supervisor.pid").exists())
            self.assertIn("supervisor.pid", r.get("cleaned") or [])


class B11CloseReportTests(unittest.TestCase):
    def test_separates_stop_and_port_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            port = free_port()
            broken = Path(tmp) / "lsof-broken"
            broken.write_text("#!/bin/bash\necho fail >&2\nexit 127\n", encoding="utf-8")
            broken.chmod(0o755)
            report = lifecycle.close_report(
                run_dir, MONITOR, port,
                stop_result={"status": "already_stopped", "signals": 0, "detail": "无 claim"},
                lsof_bin=str(broken),
            )
            self.assertEqual(report["schema"], "monitor_close_report_v1")
            self.assertEqual(report["stop"]["status"], "already_stopped")
            self.assertEqual(report["port_probe"]["status"], lifecycle.PORT_UNOBSERVABLE)
            self.assertFalse(report["closed_ok"])
            self.assertEqual(report["exit_code"], 4)
            self.assertIn("查不了", report["user_message"])

    def test_busy_port_after_stop_not_closed_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            port = free_port()
            # 假 lsof：exit 0 = busy
            fake = Path(tmp) / "lsof"
            fake.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
            fake.chmod(0o755)
            report = lifecycle.close_report(
                run_dir, MONITOR, port,
                stop_result={"status": "stopped", "signals": 1, "detail": "ok"},
                lsof_bin=str(fake),
            )
            self.assertTrue(report["stop"]["status"] == "stopped")
            self.assertEqual(report["port_probe"]["status"], lifecycle.PORT_BUSY)
            self.assertFalse(report["closed_ok"])
            self.assertEqual(report["exit_code"], 1)

    def test_free_port_and_stop_ok_is_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            port = free_port()
            fake = Path(tmp) / "lsof"
            fake.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
            fake.chmod(0o755)
            report = lifecycle.close_report(
                run_dir, MONITOR, port,
                stop_result={"status": "already_stopped", "signals": 0, "detail": "无 claim"},
                lsof_bin=str(fake),
            )
            self.assertTrue(report["closed_ok"])
            self.assertEqual(report["exit_code"], 0)
            self.assertEqual(report["port_probe"]["status"], lifecycle.PORT_FREE)

    def test_lsof_failure_not_treated_as_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = free_port()
            fake = Path(tmp) / "lsof"
            fake.write_text("#!/bin/bash\necho boom >&2\nexit 127\n", encoding="utf-8")
            fake.chmod(0o755)
            probe = lifecycle.probe_port_status(port, lsof_bin=str(fake))
            self.assertEqual(probe["status"], lifecycle.PORT_UNOBSERVABLE)
            self.assertNotEqual(probe["status"], lifecycle.PORT_BUSY)

    def test_default_port_8799_follows_normal_rules_via_stub_argv(self):
        """产品不得硬拒 8799；替身记录实参，不向真实 8799 发信号/建连。"""
        with tempfile.TemporaryDirectory() as tmp:
            argv_log = Path(tmp) / "argv.log"
            stub = Path(tmp) / "lsof-stub"
            stub.write_text(
                "#!/bin/bash\nprintf '%%s\\n' \"$*\" >> \"%s\"\n"
                "for a in \"$@\"; do case \"$a\" in -t*|tiTCP:*) exit 1;; esac; done\n"
                "exit 1\n" % argv_log,
                encoding="utf-8",
            )
            stub.chmod(0o755)
            probe = lifecycle.probe_port_status(8799, lsof_bin=str(stub))
            self.assertEqual(probe["status"], lifecycle.PORT_FREE, probe)
            self.assertIn("-iTCP:8799", probe.get("lsof_argv") or [])
            logged = argv_log.read_text(encoding="utf-8")
            self.assertIn("8799", logged)
            # busy 路径同样走通常规则
            stub2 = Path(tmp) / "lsof-busy"
            stub2.write_text(
                "#!/bin/bash\nprintf '%%s\\n' \"$*\" >> \"%s\"\n"
                "for a in \"$@\"; do case \"$a\" in -t*|tiTCP:*) echo 1; exit 0;; esac; done\n"
                "exit 0\n" % argv_log,
                encoding="utf-8",
            )
            stub2.chmod(0o755)
            probe2 = lifecycle.probe_port_status(8799, lsof_bin=str(stub2))
            self.assertEqual(probe2["status"], lifecycle.PORT_BUSY, probe2)
            report = lifecycle.close_report(
                Path(tmp) / "run", MONITOR, 8799,
                stop_result={"status": "already_stopped", "signals": 0, "detail": "无 claim"},
                lsof_bin=str(stub),
            )
            self.assertTrue(report["closed_ok"])
            self.assertEqual(report["port_probe"]["status"], lifecycle.PORT_FREE)


class PublicCloseEntryTests(unittest.TestCase):
    def test_close_report_cli_emits_machine_json(self):
        port = free_port()
        with tempfile.TemporaryDirectory() as tmp:
            scene = Path(tmp)
            entry = copy_cli_entry(scene)
            broken = scene / "lsof-broken"
            broken.write_text("#!/bin/bash\necho fail >&2\nexit 127\n", encoding="utf-8")
            broken.chmod(0o755)
            env = dict(os.environ)
            env["XIAOBA_LSOF"] = str(broken)
            done = subprocess.run(
                [PY, "-B", str(entry), "--close-report", "--port", str(port)],
                capture_output=True, text=True, env=env, cwd=scene,
            )
        line = [ln for ln in done.stdout.splitlines() if ln.startswith("{")][0]
        report = json.loads(line)
        self.assertEqual(report["schema"], "monitor_close_report_v1")
        self.assertIn("closed_ok", report)
        self.assertIn("port_probe", report)
        self.assertIn("stop", report)
        # 无 claim + lsof 失败 → 不得仅凭退出码当成功
        self.assertFalse(report["closed_ok"])
        self.assertEqual(report["port_probe"]["status"], "unobservable")
        self.assertNotEqual(done.returncode, 0)


if __name__ == "__main__":
    unittest.main()
