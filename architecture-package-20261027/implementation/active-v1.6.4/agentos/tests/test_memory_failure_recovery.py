"""失败恢复回归：事务退出异常、索引半更新、证据读不到时的还原边界。

三个缺口都由主力的真实临时文件/SQLite 故障注入发现：

1. 提交发生在 `connect()` 的 context manager 退出过程里，之后的权限维护报错
   落在 `publish_approved_candidate` 的旧 try/except 之外，已提交的发布被降级。
2. `_rebuild_and_verify_index` 逐文件写到一半失败时，Vault 与目录恢复正确，
   图输出留着本次的半更新。
3. 证据文件存在但读不出来（真实 chmod 0200）时，旧 `_read_evidence` 把
   PermissionError 当成"文件不存在"，还原阶段据此删掉了原本存在的文件。

全部用例只在临时配置、临时 SQLite 和临时 Vault/图目录上运行。
"""

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures_review import _run_cli
# 按模块导入夹具，避免把别人的 TestCase 再收集一遍（loadTestsFromModule 会把
# 模块命名空间里的 TestCase 子类全部算进来）。
import test_memory_publication_gate as gate_fixture
import test_memory_terminal_registration as release_fixture
from xiaoba_agentos import cli as cli_module
from xiaoba_agentos import retained as retained_objects
from xiaoba_agentos.cli import EvidenceRestoreError, _restore_evidence
from xiaoba_agentos.health import doctor
from xiaoba_agentos.memory import MemoryBridge
from xiaoba_agentos.review_flow import execute_review
from xiaoba_agentos.store import EventStore

BODY_APPROVED = gate_fixture.BODY_APPROVED
IMPLEMENTATION_ROOT = Path(__file__).resolve().parents[1]

# 自检的跨重启验证：换一个进程（另一个 PID）、只看磁盘，重新枚举一遍。
_NEW_PROCESS_SELF_CHECK = """
import json, sys
from pathlib import Path
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos import retained
config = AppConfig.load(Path(sys.argv[1]))
payload = retained.collect(config)
Path(sys.argv[2]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
"""

# 另一个进程里跑同一条处理命令：并发验收用来让两个进程真的抢同一个对象。
# 第三个参数是门闩文件，两个进程都等它出现再动手，把竞争窗口放到最大。
_NEW_PROCESS_RESOLVE_TRASH = """
import json, sys, time
from pathlib import Path
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos import retained
config = AppConfig.load(Path(sys.argv[1]))
barrier = Path(sys.argv[3]) if sys.argv[3] != "-" else None
if barrier is not None:
    deadline = time.monotonic() + 60
    while not barrier.exists() and time.monotonic() < deadline:
        time.sleep(0.002)
outcome = retained.resolve_retained(
    config, retained_path=Path(sys.argv[2]), action="trash"
)
Path(sys.argv[4]).write_text(json.dumps(outcome, ensure_ascii=False), encoding="utf-8")
"""

# 另一个进程里只看磁盘：枚举误捕获对象，并对命令行给出的每个路径给出检索排除判定。
# 参数：config、输出 json、若干待判定的路径。枚举与排除都在新进程里发生，本进程
# 只读结果，避免"同一个进程里的字典"冒充跨重启的持久发现。
_NEW_PROCESS_CAPTURE_CHECK = """
import json, sys
from pathlib import Path
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos import retained
config = AppConfig.load(Path(sys.argv[1]))
payload = retained.collect(config)
excluded = {path: retained.is_retained_path(config, Path(path)) for path in sys.argv[3:]}
Path(sys.argv[2]).write_text(
    json.dumps({"payload": payload, "excluded": excluded}, ensure_ascii=False),
    encoding="utf-8",
)
"""

# 真实 fd 控制组的写者：认领**之前**就打开文件并一直持有这个 fd，之后借旧 fd
# 写入（WRITE）与追加（APPEND）。丙不删除被认领的对象，所以这些字节必须落在
# 保留物里；inode 号由子进程自己报出来，用来证明保留物就是同一个原对象。
_FD_WRITER_CHILD = """
import os, sys
path = sys.argv[1]
new_bytes = bytes.fromhex(sys.argv[2])
more_bytes = bytes.fromhex(sys.argv[3])
descriptor = os.open(path, os.O_RDWR)
print("READY %d" % os.fstat(descriptor).st_ino, flush=True)
for line in sys.stdin:
    command = line.strip()
    if command == "WRITE":
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, new_bytes)
        os.fsync(descriptor)
        print("WROTE", flush=True)
    elif command == "APPEND":
        os.lseek(descriptor, 0, os.SEEK_END)
        os.write(descriptor, more_bytes)
        os.fsync(descriptor)
        print("APPENDED", flush=True)
    else:
        os.close(descriptor)
        break
"""

FD_CONTROL_TASK = "fd-control-task-0000-4000-8000-000000000000"

# 受限方案丙（v1.5.5）改变了失败恢复的产物形状：被认领的原对象不再被 unlink，
# 而是以私有名字留在同级目录里（`.{原名}.{写者PID}.rollback`）。旧断言"失败后
# Vault/图目录逐字节等于原样"因此不再成立——它把保留物也算成了活文件。下面这些
# 助手把两件事分开验证：活文件必须逐字节还原，保留物必须单独列出并由自检枚举。
# 这不是把断言放松：任何非保留物的新增/改动、任何未被自检列出的保留物，依旧失败。


def _is_retained_name(name):
    return retained_objects.parse_retained_name(name) is not None


def _live_only(mapping):
    """只有活文件：排除丙保留物（它们由 `_retained_only` 与自检单独验证）。"""
    return {name: content for name, content in mapping.items() if not _is_retained_name(Path(name).name)}


def _retained_only(mapping):
    return {name: content for name, content in mapping.items() if _is_retained_name(Path(name).name)}


def _retained_in(directory):
    """`directory` 表层的保留物：`{私有名: {"original", "pid", "suffix", "path"}}`。

    `path` 与自检给出的保留位置用同一种规范化写法（realpath），这样断言比的是同一个
    文件，而不是 /var 与 /private/var 这两种拼法。
    """
    found = {}
    for path in sorted(Path(directory).iterdir()):
        parsed = retained_objects.parse_retained_name(path.name)
        if parsed is not None:
            found[path.name] = dict(parsed, path=Path(os.path.realpath(str(path))))
    return found


class _GateFixture(unittest.TestCase):
    """复用发布门禁夹具：临时配置/库/Vault/图目录，且目录与图索引非空。"""

    def setUp(self):
        self.gate = gate_fixture.PublicationGateTest()
        self.gate.setUp()

    def tearDown(self):
        self.gate.tearDown()

    def _live_vault_bytes(self):
        return _live_only(self.gate._vault_bytes())

    def _live_graph_bytes(self):
        return _live_only(self.gate._graph_bytes())

    def _retained_in(self, directory):
        return _retained_in(directory)

    def _self_check(self, config=None):
        return retained_objects.collect(config if config is not None else self.gate.config)

    def _listed_paths(self, config=None):
        payload = self._self_check(config)
        return sorted(
            str(item["retained_path"])
            for item in payload["objects"]
            if item["state"] != "record_without_object"
        )

    def _assert_self_check_lists(self, expected, config=None):
        """丙：自检必须把每个保留物都列出来（名称规则 + 登记，两层互为校验）。"""
        payload = self._self_check(config)
        listed = self._listed_paths(config)
        for path in expected:
            self.assertIn(str(path), listed, payload)
        return payload

    # ------------------------------------------------- 受限方案丙（v1.5.5）夹具
    def _failing_publication(self):
        """一次真实的失败发布：笔记、目录与图输出都被本次写入，然后全部撤销。

        撤销会为每个被认领的原对象留下一个保留物（私有名字，同一个 inode），
        这正是自检要枚举的东西。
        """
        gate = self.gate
        task_id, _, approved = gate._candidate()
        gate._approve(task_id, approved, ("delivery", "final"))
        graph_dir = Path(gate.config.raw["memory"]["knowlp_graph_dir"])

        def partial_then_fail(bridge, vault):
            files = sorted(path for path in graph_dir.iterdir() if path.is_file())
            staging = tempfile.TemporaryDirectory()
            self.addCleanup(staging.cleanup)
            built = Path(staging.name)
            for path in files:
                (built / path.name).write_bytes(b"partial index update from this invocation\n")
            for path in files:
                bridge._install_index_output(graph_dir, built / path.name)
            raise RuntimeError("injected failure after several index outputs")

        with patch.object(MemoryBridge, "_rebuild_and_verify_index", partial_then_fail):
            code, payload = gate._cli(["publish-memory", "--task-id", task_id])

        self.assertEqual(1, code, payload)
        self.assertEqual("reconciliation_required", payload["status"])
        self.assertEqual(0, gate._events(task_id, "memory.published"))
        return task_id, payload

    def _expected_retained(self):
        """自检扫描范围内能看到的每一个保留物：`{realpath: 解析出的字段}`。"""
        found = {}
        for directory in retained_objects.retained_scan_dirs(self.gate.config):
            for entry in _retained_in(directory).values():
                found[str(entry["path"])] = entry
        return found

    def _self_check_in_new_process(self, config):
        """换一个进程（另一个 PID）、只看磁盘重跑自检：跨重启枚举的验证方式。"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(IMPLEMENTATION_ROOT / "src")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "self-check.json"
            process = subprocess.Popen(
                [sys.executable, "-B", "-c", _NEW_PROCESS_SELF_CHECK, str(config.path), str(output)],
                cwd=str(IMPLEMENTATION_ROOT),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout, stderr = process.communicate(timeout=120)
            self.assertEqual(0, process.returncode, f"{stdout}\n{stderr}")
            self.assertNotEqual(os.getpid(), process.pid, "自检必须在另一个进程里运行")
            return json.loads(output.read_text(encoding="utf-8"))

    def _capture_check_in_new_process(self, config, paths):
        """换一个进程只看磁盘：枚举误捕获对象，并对这些路径给出检索排除判定。"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(IMPLEMENTATION_ROOT / "src")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture-check.json"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    _NEW_PROCESS_CAPTURE_CHECK,
                    str(config.path),
                    str(output),
                    *[str(path) for path in paths],
                ],
                cwd=str(IMPLEMENTATION_ROOT),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout, stderr = process.communicate(timeout=120)
            self.assertEqual(0, process.returncode, f"{stdout}\n{stderr}")
            self.assertNotEqual(os.getpid(), process.pid, "枚举与排除必须在另一个进程里发生")
            return json.loads(output.read_text(encoding="utf-8"))


class TransactionExitRecoveryTest(_GateFixture):
    """缺口 1：真实 context exit（先提交、后维护）失败不得降级已提交的发布。"""

    def _inject_maintenance_failure_after_commit(self, task_id):
        original = EventStore._secure_permissions
        observed = {"injected": False}

        def permissions(store):
            original(store)
            if store.path == self.gate.store.path and not observed["injected"]:
                with store.read_only() as conn:
                    row = conn.execute(
                        "SELECT status FROM memory_candidates WHERE task_id=?", (task_id,)
                    ).fetchone()
                if row and row[0] == "published":
                    observed["injected"] = True
                    raise OSError("injected maintenance failure after the actual commit")

        return patch.object(EventStore, "_secure_permissions", permissions), observed

    def test_a_committed_publication_survives_a_failure_in_the_transaction_exit(self):
        gate = self.gate
        task_id, _, approved = gate._candidate()
        gate._approve(task_id, approved, ("delivery", "final"))
        injector, observed = self._inject_maintenance_failure_after_commit(task_id)

        with injector:
            code, payload = gate._cli(["publish-memory", "--task-id", task_id])

        self.assertTrue(observed["injected"], "探针必须在数据库真正提交之后触发")
        self.assertEqual(0, code, payload)
        self.assertEqual("published", payload["status"])
        self.assertEqual("published", gate.store.get_memory_candidate(task_id)["status"])
        self.assertEqual(1, gate._events(task_id, "memory.published"))
        self.assertEqual(
            0, gate._events(task_id, "memory.publication_failed"),
            "已提交的合法发布不得被降级成失败",
        )
        self.assertEqual(1, len(gate._notes(gate._vault_bytes(), BODY_APPROVED)))

    def test_the_shared_review_entry_keeps_a_committed_publication(self):
        gate = self.gate
        task_id, _, approved = gate._candidate()
        gate._approve(task_id, approved, ("delivery", "final"))
        injector, observed = self._inject_maintenance_failure_after_commit(task_id)

        with injector:
            code, payload = execute_review(gate.config, gate.store, task_id, "final", "approve", "")

        self.assertTrue(observed["injected"])
        self.assertEqual(0, code, payload)
        self.assertEqual("published", gate.store.get_memory_candidate(task_id)["status"])
        self.assertEqual(0, gate._events(task_id, "memory.publication_failed"))

    def test_an_unreadable_outcome_is_reconciled_without_demoting_the_candidate(self):
        gate = self.gate
        task_id, _, approved = gate._candidate()
        gate._approve(task_id, approved, ("delivery", "final"))
        original_get = EventStore.get_memory_candidate
        calls = {"count": 0}

        def flaky_get(store, tid):
            calls["count"] += 1
            # 第一次是命令行的前置读取；读回阶段必须失败，模拟"读不出来"。
            if calls["count"] >= 2:
                raise sqlite3.OperationalError("database is locked")
            return original_get(store, tid)

        def failing_settlement(store, conn, tid, path):
            raise sqlite3.OperationalError("disk I/O error")

        with patch.object(EventStore, "get_memory_candidate", flaky_get), patch.object(
            EventStore, "_mark_memory_published_tx", failing_settlement
        ):
            code, payload = gate._cli(["publish-memory", "--task-id", task_id])

        self.assertEqual(1, code, payload)
        self.assertEqual("reconciliation_required", payload["status"])
        self.assertEqual("memory_publication_uncertain", payload["operation"])
        row = gate.store.get_memory_candidate(task_id)
        self.assertEqual(
            "pending_final_review", row["status"],
            "读回未知不能自动等同未提交，不得把候选降级",
        )
        self.assertEqual(0, gate._events(task_id, "memory.publication_failed"))


class IndexRecoveryTest(_GateFixture):
    """缺口 2：索引输出写到一半失败时，恢复边界必须一并覆盖图文件。"""

    def _graph_dir(self):
        return Path(self.gate.config.raw["memory"]["knowlp_graph_dir"])

    def _built_outputs(self, contents):
        """一份"构建产物"临时目录：真实重建就是逐个安装这些文件。"""
        staging = tempfile.TemporaryDirectory()
        self.addCleanup(staging.cleanup)
        built = Path(staging.name)
        for name, payload in contents.items():
            (built / name).write_bytes(payload)
        return built

    def test_a_midway_index_failure_restores_every_output_it_touched(self):
        gate = self.gate
        task_id, _, approved = gate._candidate()
        gate._approve(task_id, approved, ("delivery", "final"))
        vault_before, graph_before = gate._vault_bytes(), gate._graph_bytes()
        graph_dir = self._graph_dir()
        touched = {}

        def partial_then_fail(bridge, vault):
            files = sorted(p for p in graph_dir.iterdir() if p.is_file())
            self.assertTrue(files, "夹具需要已有的图输出来制造半更新")
            touched["files"] = [str(p.relative_to(graph_dir)) for p in files]
            # 真实重建逐个安装构建产物；这里用同一个安装助手，所以替换记录存在，
            # 归属可证——"不同即我的"那种判据已经不允许再用。
            built = self._built_outputs({
                **{path.name: b"partial index update from this invocation\n" for path in files},
                "partial_extra_output.json": b"partial output that did not exist before\n",
            })
            for path in files:
                bridge._install_index_output(graph_dir, built / path.name)
            extra = graph_dir / "partial_extra_output.json"
            bridge._install_index_output(graph_dir, built / "partial_extra_output.json")
            touched["extra"] = extra
            raise RuntimeError("injected failure after several index outputs")

        with patch.object(MemoryBridge, "_rebuild_and_verify_index", partial_then_fail):
            code, payload = gate._cli(["publish-memory", "--task-id", task_id])

        self.assertEqual(1, code, payload)
        self.assertEqual("reconciliation_required", payload["status"])
        self.assertTrue(touched["files"])
        self.assertEqual(vault_before, self._live_vault_bytes(), "笔记与目录的活文件必须恢复到原字节")
        self.assertEqual(graph_before, self._live_graph_bytes(), "每个被写过的图输出的活文件都必须恢复")
        self.assertFalse(touched["extra"].exists(), "本次新建的图输出的活路径必须消失")
        # 丙：每个被认领的原对象都以保留物留在原目录里——同一个 inode 换名，不是
        # 复制品，所以旧 fd 写者之后的字节仍落在这些对象里（专门的 fd 控制组见下）。
        graph_retained = self._retained_in(graph_dir)
        self.assertEqual(
            sorted(touched["files"] + ["partial_extra_output.json"]),
            sorted(entry["original"] for entry in graph_retained.values()),
            "每个被认领的原对象都必须以保留物留下，谁都不能被 unlink 掉",
        )
        retained_extra = next(
            entry for entry in graph_retained.values() if entry["original"] == "partial_extra_output.json"
        )
        self.assertEqual(
            b"partial output that did not exist before\n",
            retained_extra["path"].read_bytes(),
            "保留物就是被认领的那个对象本身，内容必须还是它当时的内容",
        )
        vault = self.gate.vault
        memory_config = self.gate.config.raw["memory"]
        catalog = vault / memory_config["catalog_note"]
        expected_retained = (
            [entry["path"] for entry in graph_retained.values()]
            + [entry["path"] for entry in self._retained_in(vault / memory_config["obsidian_subdir"]).values()]
            + [entry["path"] for entry in self._retained_in(catalog.parent).values()]
        )
        report = self._assert_self_check_lists(expected_retained)
        self.assertEqual(0, report["anomaly_count"], report)
        self.assertTrue(report["enumeration_ok"], report)
        self.assertEqual(0, gate._events(task_id, "memory.published"))
        self.assertNotEqual("published", gate.store.get_memory_candidate(task_id)["status"])

    def test_the_shared_review_entry_also_restores_a_half_written_index(self):
        gate = self.gate
        task_id, _, approved = gate._candidate()
        gate._approve(task_id, approved, ("delivery", "final"))
        vault_before, graph_before = gate._vault_bytes(), gate._graph_bytes()
        graph_dir = self._graph_dir()

        def partial_then_fail(bridge, vault):
            files = sorted(p for p in graph_dir.iterdir() if p.is_file())
            built = self._built_outputs({
                path.name: b"partial index update from this invocation\n" for path in files
            })
            for path in files:
                bridge._install_index_output(graph_dir, built / path.name)
            raise RuntimeError("injected failure in the shared review entry")

        with patch.object(MemoryBridge, "_rebuild_and_verify_index", partial_then_fail):
            code, payload = execute_review(gate.config, gate.store, task_id, "final", "approve", "")

        self.assertEqual(1, code, payload)
        self.assertEqual("reconciliation_required", payload["status"])
        self.assertEqual(vault_before, self._live_vault_bytes())
        self.assertEqual(graph_before, self._live_graph_bytes())
        self.assertEqual(0, gate._events(task_id, "memory.published"))

    def test_a_recorded_output_that_cannot_be_put_back_is_reported_for_manual_action(self):
        gate = self.gate
        task_id, _, approved = gate._candidate()
        gate._approve(task_id, approved, ("delivery", "final"))
        graph_dir = self._graph_dir()
        damaged = {}

        def damage_then_fail(bridge, vault):
            files = sorted(p for p in graph_dir.iterdir() if p.is_file())
            damaged["path"] = files[0]
            built = self._built_outputs(
                {files[0].name: b"damage that this call will not manage to restore\n"}
            )
            bridge._install_index_output(graph_dir, built / files[0].name)
            raise RuntimeError("injected index failure")

        def failing_restore(bridge, path, content, mode=0o600):
            raise OSError("disk full while restoring the index output")

        with patch.object(MemoryBridge, "_rebuild_and_verify_index", damage_then_fail), patch.object(
            MemoryBridge, "_place_original", failing_restore
        ):
            code, payload = gate._cli(["publish-memory", "--task-id", task_id])

        self.assertEqual(1, code, payload)
        self.assertEqual("reconciliation_required", payload["status"])
        self.assertTrue(payload.get("manual_action_required"), payload)
        self.assertIn("could not be fully rolled back", json.dumps(payload, ensure_ascii=False))
        self.assertNotEqual("published", gate.store.get_memory_candidate(task_id)["status"])
        self.assertEqual(0, gate._events(task_id, "memory.published"))
        self.assertEqual(
            b"damage that this call will not manage to restore\n",
            damaged["path"].read_bytes(),
            "恢复失败时不得谎称已恢复：损坏字节原样留着，由人工处理",
        )

    def test_an_unrecorded_change_is_left_in_place_and_reported(self):
        gate = self.gate
        task_id, _, approved = gate._candidate()
        gate._approve(task_id, approved, ("delivery", "final"))
        graph_dir = self._graph_dir()
        foreign = {}

        def change_without_recording(bridge, vault):
            # 没有替换记录：无法证明是本次写入，必须原样保留并上报。
            path = sorted(p for p in graph_dir.iterdir() if p.is_file())[0]
            foreign["path"] = path
            path.write_bytes(b"change this call cannot prove it made\n")
            raise RuntimeError("injected index failure")

        with patch.object(MemoryBridge, "_rebuild_and_verify_index", change_without_recording):
            code, payload = gate._cli(["publish-memory", "--task-id", task_id])

        self.assertEqual(1, code, payload)
        self.assertEqual("reconciliation_required", payload["status"])
        self.assertTrue(payload.get("manual_action_required"), payload)
        self.assertEqual(
            b"change this call cannot prove it made\n",
            foreign["path"].read_bytes(),
            "归属未知的内容必须原样保留，不得当成自己的修改撤销",
        )
        self.assertEqual(0, gate._events(task_id, "memory.published"))

    def test_the_inner_rollback_leaves_an_output_that_changed_after_its_install(self):
        """内层（重建自身的）回滚也要讲归属：装完之后被别人改过就不得覆盖。"""
        gate = self.gate
        task_id, _, approved = gate._candidate()
        gate._approve(task_id, approved, ("delivery", "final"))
        graph_dir = self._graph_dir()
        real_install = MemoryBridge._install_index_output
        raced = {}

        def install_then_race(bridge, target_dir, source):
            real_install(bridge, target_dir, source)
            if "done" not in raced and source.name.endswith(".json"):
                target = target_dir / source.name
                target.write_bytes(b"independent writer bytes after our install\n")
                raced["done"] = target

        with patch.object(MemoryBridge, "_install_index_output", install_then_race):
            code, payload = gate._cli(["publish-memory", "--task-id", task_id])

        self.assertIsNotNone(raced.get("done"), "需要真实重建安装过至少一个图输出")
        self.assertEqual(1, code, payload)
        self.assertEqual("reconciliation_required", payload["status"])
        self.assertTrue(payload.get("manual_action_required"), payload)
        self.assertEqual(
            b"independent writer bytes after our install\n",
            raced["done"].read_bytes(),
            "内层回滚不得覆盖装完之后出现的他人内容",
        )
        self.assertEqual(0, gate._events(task_id, "memory.published"))


class IndependentWriterRecoveryTest(_GateFixture):
    """缺口 1：另一个真实进程在构建窗口内写下的四处内容都必须保留。"""

    _CHILD = r'''
import json, os, sys
from pathlib import Path
existing, new_file, catalog, note = map(Path, sys.argv[1:])
existing.write_bytes(b"USER_NEW_GRAPH_BYTES\n")
new_file.write_bytes(b"USER_NEW_FILE_BYTES\n")
with catalog.open("ab") as handle:
    handle.write(b"\nUSER_NEW_CATALOG_LINE\n")
note.write_bytes(b"USER_NEW_NOTE_BYTES\n")
print(json.dumps({
    "pid": os.getpid(),
    "existing_graph_bytes": existing.read_text(),
    "new_graph_file_bytes": new_file.read_text(),
    "catalog_has_user_line": b"USER_NEW_CATALOG_LINE" in catalog.read_bytes(),
    "note_bytes": note.read_text(),
}))
'''

    def _run_independent_writer(self, task_id):
        gate = self.gate
        graph_dir = Path(gate.config.raw["memory"]["knowlp_graph_dir"])
        catalog = gate.vault / gate.config.raw["memory"]["catalog_note"]
        existing = sorted(p for p in graph_dir.iterdir() if p.is_file())[0]
        new_file = graph_dir / "user-new-output.txt"
        observed = {}

        def fail_after_independent_write(bridge, vault):
            note = bridge.find_note(vault, task_id)
            self.assertIsNotNone(note, "发布必须先落下笔记，独立写入者才有可改的目标")
            observed["note"] = note
            child = subprocess.run(
                [sys.executable, "-B", "-c", self._CHILD,
                 str(existing), str(new_file), str(catalog), str(note)],
                text=True, capture_output=True, check=True,
            )
            observed["writer"] = json.loads(child.stdout)
            raise RuntimeError("injected index failure after an independent writer changed everything")

        with patch.object(MemoryBridge, "_rebuild_and_verify_index", fail_after_independent_write):
            code, payload = gate._cli(["publish-memory", "--task-id", task_id])
        observed.update({"code": code, "payload": payload, "existing": existing,
                         "new_file": new_file, "catalog": catalog})
        return observed

    def test_every_content_the_independent_writer_wrote_is_kept(self):
        gate = self.gate
        task_id, _, approved = gate._candidate()
        gate._approve(task_id, approved, ("delivery", "final"))
        observed = self._run_independent_writer(task_id)

        self.assertNotEqual(observed["writer"]["pid"], os.getpid(), "必须是另一个真实进程")
        self.assertEqual(1, observed["code"], observed["payload"])
        self.assertEqual("reconciliation_required", observed["payload"]["status"])
        self.assertTrue(observed["payload"].get("manual_action_required"), observed["payload"])
        self.assertEqual(b"USER_NEW_GRAPH_BYTES\n", observed["existing"].read_bytes())
        self.assertTrue(observed["new_file"].exists(), "独立写入者新建的图文件不得删除")
        self.assertIn(b"USER_NEW_CATALOG_LINE", observed["catalog"].read_bytes())
        self.assertTrue(observed["note"].exists(), "被独立写入者改过的笔记不得删除")
        self.assertEqual(b"USER_NEW_NOTE_BYTES\n", observed["note"].read_bytes())
        self.assertEqual(0, gate._events(task_id, "memory.published"))

    def test_a_publication_without_competition_still_restores_byte_for_byte(self):
        """对照：没有独立写入者时，本次自己的写入仍被完整撤销。

        丙下"完整撤销"的含义分两层：活文件逐字节回到原样；被认领的原对象不删除，
        而是作为保留物留在原目录并由自检列出。因此这条对照现在同时断言两件事，
        并把"必须报人工处理"也一并断言——留着保留物却宣称无事发生才是回归。
        """
        gate = self.gate
        task_id, _, approved = gate._candidate()
        gate._approve(task_id, approved, ("delivery", "final"))
        vault_before, graph_before = gate._vault_bytes(), gate._graph_bytes()
        graph_dir = Path(gate.config.raw["memory"]["knowlp_graph_dir"])
        touched = {}

        def partial_then_fail(bridge, vault):
            files = sorted(p for p in graph_dir.iterdir() if p.is_file())
            touched["files"] = [str(p.relative_to(graph_dir)) for p in files]
            staging = tempfile.TemporaryDirectory()
            self.addCleanup(staging.cleanup)
            built = Path(staging.name)
            for path in files:
                (built / path.name).write_bytes(b"partial index update\n")
                bridge._install_index_output(graph_dir, built / path.name)
            raise RuntimeError("injected index failure without any other writer")

        with patch.object(MemoryBridge, "_rebuild_and_verify_index", partial_then_fail):
            code, payload = gate._cli(["publish-memory", "--task-id", task_id])

        self.assertEqual(1, code, payload)
        self.assertEqual("reconciliation_required", payload["status"])
        self.assertEqual(vault_before, self._live_vault_bytes(), "活文件必须逐字节回到原样")
        self.assertEqual(graph_before, self._live_graph_bytes(), "活文件必须逐字节回到原样")
        graph_retained = self._retained_in(graph_dir)
        self.assertEqual(
            sorted(touched["files"]),
            sorted(entry["original"] for entry in graph_retained.values()),
            "被认领的原对象必须以保留物留下，且只留下这些",
        )
        report = self._assert_self_check_lists([entry["path"] for entry in graph_retained.values()])
        self.assertEqual(0, report["anomaly_count"], report)
        self.assertTrue(report["enumeration_ok"], report)
        self.assertGreater(report["retained_count"], 0, report)
        self.assertTrue(payload.get("manual_action_required"), payload)
        self.assertIn("kept at", json.dumps(payload, ensure_ascii=False), "必须报出保留物的真实位置")
        self.assertEqual(0, gate._events(task_id, "memory.published"))


class RollbackAfterClaimRaceTest(_GateFixture):
    """写入发生在归属判定之后的真实交错。

    归属判定与撤销现在是同一个原子步骤（`_claim_for_undo` 用 `os.replace`
    把对象挪到私有名字，再对**被挪走的**字节判归属）。这里的注入点就在那次
    原子挪动**之后**：另一个真实进程往原路径写新内容。旧实现会照旧删/覆盖，
    新实现必须保留该内容并报告冲突。
    """

    PEER_BYTES = b"USER_BYTES_WRITTEN_AFTER_THE_ATOMIC_CLAIM\n"
    _CHILD = r'''
import os, sys
from pathlib import Path
Path(sys.argv[1]).write_bytes(b"USER_BYTES_WRITTEN_AFTER_THE_ATOMIC_CLAIM\n")
print(os.getpid())
'''

    def _built_outputs(self, contents):
        staging = tempfile.TemporaryDirectory()
        self.addCleanup(staging.cleanup)
        built = Path(staging.name)
        for name, payload in contents.items():
            (built / name).write_bytes(payload)
        return built

    def _claim_then_peer_write(self, observed):
        """Wrap the atomic claim so a real second process writes right after it."""
        real_claim = MemoryBridge._claim_for_undo

        def claim_then_write(bridge, path):
            claim = real_claim(bridge, path)
            if (
                observed.get("target") is not None
                and path == observed["target"]
                and "peer_pid" not in observed
            ):
                child = subprocess.run(
                    [sys.executable, "-B", "-c", self._CHILD, str(path)],
                    text=True, capture_output=True, check=True,
                )
                observed["peer_pid"] = int(child.stdout)
            return claim

        return claim_then_write

    def _assert_peer_content_kept(self, observed, code, payload, expect_recreated=False):
        self.assertIn("peer_pid", observed, "必须由另一个真实进程在原子认领之后写入")
        self.assertNotEqual(observed["peer_pid"], os.getpid(), "必须是另一个真实进程")
        self.assertEqual(1, code, payload)
        self.assertEqual("reconciliation_required", payload["status"])
        self.assertTrue(payload.get("manual_action_required"), payload)
        self.assertTrue(observed["target"].exists(), "认领之后写入的内容不得被删除")
        self.assertEqual(
            self.PEER_BYTES, observed["target"].read_bytes(), "用户新字节必须原样保留"
        )
        self.assertEqual(0, self.gate._events(observed["task_id"], "memory.published"))
        if expect_recreated:
            # 丙改造后 "本调用新建的文件其写入被撤下" 一律留下保留物并报人工核对，
            # 于是反证 C（删掉重建冲突的逐字报告）不再被这一条掩盖：这里额外要求
            # 第三方重建活路径仍被单独点名。
            errors = json.dumps(payload.get("errors", []), ensure_ascii=False)
            self.assertIn(
                "recreated while this call was undoing it",
                errors,
                "活路径被第三方重建时必须逐字报告该冲突",
            )

    def test_a_note_written_after_the_claim_is_kept(self):
        gate = self.gate
        task_id, _, approved = gate._candidate()
        gate._approve(task_id, approved, ("delivery", "final"))
        observed = {"task_id": task_id}

        def fail_after_note(bridge, vault):
            observed["target"] = bridge.find_note(vault, task_id)
            self.assertIsNotNone(observed["target"], "发布必须先落下笔记")
            raise RuntimeError("injected index failure after the note landed")

        with patch.object(MemoryBridge, "_rebuild_and_verify_index", fail_after_note), patch.object(
            MemoryBridge, "_claim_for_undo", self._claim_then_peer_write(observed)
        ):
            code, payload = gate._cli(["publish-memory", "--task-id", task_id])

        self._assert_peer_content_kept(observed, code, payload, expect_recreated=True)

    def test_a_catalog_written_after_the_claim_is_kept(self):
        gate = self.gate
        task_id, _, approved = gate._candidate()
        gate._approve(task_id, approved, ("delivery", "final"))
        catalog = (gate.vault / gate.config.raw["memory"]["catalog_note"]).resolve()
        observed = {"task_id": task_id, "target": catalog}

        def fail_after_catalog(bridge, vault):
            raise RuntimeError("injected index failure after the catalog line landed")

        with patch.object(MemoryBridge, "_rebuild_and_verify_index", fail_after_catalog), patch.object(
            MemoryBridge, "_claim_for_undo", self._claim_then_peer_write(observed)
        ):
            code, payload = gate._cli(["publish-memory", "--task-id", task_id])

        self._assert_peer_content_kept(observed, code, payload)

    def test_an_existing_index_output_written_after_the_claim_is_kept(self):
        gate = self.gate
        task_id, _, approved = gate._candidate()
        gate._approve(task_id, approved, ("delivery", "final"))
        graph_dir = Path(gate.config.raw["memory"]["knowlp_graph_dir"]).resolve()
        observed = {"task_id": task_id}

        def install_then_fail(bridge, vault):
            files = sorted(p for p in graph_dir.iterdir() if p.is_file())
            self.assertTrue(files, "夹具需要已有的图输出")
            observed["target"] = files[0]
            built = self._built_outputs({files[0].name: b"index update from this invocation\n"})
            bridge._install_index_output(graph_dir, built / files[0].name)
            raise RuntimeError("injected index failure after installing an existing output")

        with patch.object(MemoryBridge, "_rebuild_and_verify_index", install_then_fail), patch.object(
            MemoryBridge, "_claim_for_undo", self._claim_then_peer_write(observed)
        ):
            code, payload = gate._cli(["publish-memory", "--task-id", task_id])

        self._assert_peer_content_kept(observed, code, payload)

    def test_a_new_index_output_written_after_the_claim_is_kept(self):
        gate = self.gate
        task_id, _, approved = gate._candidate()
        gate._approve(task_id, approved, ("delivery", "final"))
        graph_dir = Path(gate.config.raw["memory"]["knowlp_graph_dir"]).resolve()
        observed = {"task_id": task_id}

        def install_then_fail(bridge, vault):
            observed["target"] = graph_dir / "brand-new-output.json"
            built = self._built_outputs({"brand-new-output.json": b"new index output from this invocation\n"})
            bridge._install_index_output(graph_dir, built / "brand-new-output.json")
            raise RuntimeError("injected index failure after installing a new output")

        with patch.object(MemoryBridge, "_rebuild_and_verify_index", install_then_fail), patch.object(
            MemoryBridge, "_claim_for_undo", self._claim_then_peer_write(observed)
        ):
            code, payload = gate._cli(["publish-memory", "--task-id", task_id])

        self._assert_peer_content_kept(observed, code, payload, expect_recreated=True)


class StagingNameCollisionTest(unittest.TestCase):
    """控制组：认领/放回暂存名必须先原子取得专属位置。

    暂存名可以被上一次调用刻意保留的拷贝占用（``_put_back_claim`` 在原路径被占
    时就把用户内容留在 ``.{name}.{pid}.rollback``），所以这些名字都不保证空闲。
    覆盖式 ``os.replace`` 或盲写 ``write_bytes`` 会销毁那份内容；正确行为是：原子
    取得专属位置，被占用的名字一个字节都不动，并在报告里指出真实保留位置。
    """

    installed = b"bytes this call installed\n"
    user = b"USER_CONTENT_KEPT_BY_THE_FIRST_CALL\n"

    def _bridge(self):
        return object.__new__(MemoryBridge)  # 只调用静态/认领辅助，不需要 __init__

    def _fixture(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def _private(self, base, name, suffix):
        return base / f".{name}.{os.getpid()}.{suffix}"

    def _interleave_first_conflict(self, bridge, target, installed_sha, problems):
        """复刻真实序列：读后有人写用户内容，认领后原路径又被本调用重建。"""
        real_read = Path.read_bytes
        real_replace = os.replace
        observed = {}

        def read_then_write_user(path):
            data = real_read(path)
            if path == target and "changed" not in observed:
                observed["changed"] = True
                path.write_bytes(self.user)
            return data

        def claim_then_recreate(src, dst):
            result = real_replace(src, dst)
            if Path(src) == target and "claim" not in observed:
                observed["claim"] = Path(dst)
                target.write_bytes(self.installed)
            return result

        with patch.object(Path, "read_bytes", read_then_write_user), patch.object(
            os, "replace", claim_then_recreate
        ):
            bridge._undo_installed_file(target, installed_sha, None, problems)
        return observed["claim"]

    def test_a_second_undo_never_clobbers_the_copy_the_first_call_kept(self):
        base = self._fixture()
        target = base / "evidence.md"
        target.write_bytes(self.installed)
        installed_sha = hashlib.sha256(self.installed).hexdigest()
        bridge = self._bridge()

        first = []
        claim = self._interleave_first_conflict(bridge, target, installed_sha, first)
        self.assertEqual(self.user, claim.read_bytes(), "第一次调用必须把用户内容保留在认领名下")
        self.assertTrue(any(str(claim) in problem for problem in first), first)

        second = []
        bridge._undo_installed_file(target, installed_sha, None, second)

        self.assertEqual(
            self.user, claim.read_bytes(), "第二次调用不得覆盖或删除第一次保留的用户内容"
        )
        self.assertTrue(
            any(str(claim) in problem for problem in second),
            "第二次调用必须报出被占用名字的真实位置",
        )
        self.assertEqual(self.installed, target.read_bytes(), "认领未开始，活路径必须原样")
        self.assertEqual(
            sorted([claim.name, target.name]), sorted(path.name for path in base.iterdir())
        )

    def test_an_occupied_rollback_name_is_never_overwritten(self):
        base = self._fixture()
        target = base / "evidence.md"
        target.write_bytes(self.installed)
        installed_sha = hashlib.sha256(self.installed).hexdigest()
        occupied = self._private(base, "evidence.md", "rollback")
        occupied.write_bytes(self.user)
        problems = []

        self._bridge()._undo_installed_file(target, installed_sha, None, problems)

        self.assertEqual(self.user, occupied.read_bytes(), "被占用的名字里的内容必须原样留下")
        self.assertEqual(self.installed, target.read_bytes(), "认领未开始，活路径必须原样")
        self.assertTrue(any(str(occupied) in problem for problem in problems), problems)

    def test_an_occupied_restore_name_is_never_truncated(self):
        base = self._fixture()
        target = base / "evidence.md"
        target.write_bytes(self.installed)
        installed_sha = hashlib.sha256(self.installed).hexdigest()
        previous = b"bytes the note had before this call\n"
        occupied = self._private(base, "evidence.md", "restore")
        occupied.write_bytes(self.user)
        problems = []

        self._bridge()._undo_installed_file(target, installed_sha, (previous, 0o600), problems)

        self.assertEqual(self.user, occupied.read_bytes(), "盲写会截断的暂存名内容必须原样留下")
        self.assertEqual(
            self.installed, target.read_bytes(), "无法放回时必须把捕获对象放回，场景不变"
        )
        self.assertTrue(any(str(occupied) in problem for problem in problems), problems)
        self.assertEqual(
            sorted([occupied.name, target.name]),
            sorted(path.name for path in base.iterdir()),
        )

    def test_a_failed_put_back_names_the_real_kept_location(self):
        base = self._fixture()
        target = base / "evidence.md"
        target.write_bytes(self.installed)
        installed_sha = hashlib.sha256(self.installed).hexdigest()
        bridge = self._bridge()
        real_read = Path.read_bytes
        real_replace = os.replace
        observed = {}

        def read_then_write_user(path):
            data = real_read(path)
            if path == target and "changed" not in observed:
                observed["changed"] = True
                path.write_bytes(self.user)
            return data

        def claim_then_recreate(src, dst):
            result = real_replace(src, dst)
            if Path(src) == target and "claim" not in observed:
                observed["claim"] = Path(dst)
                target.write_bytes(self.installed)
            return result

        problems = []
        with patch.object(Path, "read_bytes", read_then_write_user), patch.object(
            os, "replace", claim_then_recreate
        ), patch.object(os, "link", side_effect=OSError("no space left on device")):
            bridge._undo_installed_file(target, installed_sha, None, problems)

        claim = observed["claim"]
        self.assertEqual(self.user, claim.read_bytes(), "保留的拷贝必须真的在报告的位置")
        self.assertTrue(
            any(str(claim) in problem and "kept at" in problem for problem in problems), problems
        )


class EvidenceRecoveryTest(unittest.TestCase):
    """缺口 3：证据读不到时不得当成不存在，还原不得吞掉失败或覆盖他人修改。"""

    def setUp(self):
        self.release = release_fixture.ConstraintReleaseCliTest()
        self.release.setUp()

    def tearDown(self):
        self.release.tearDown()

    def _held_task_with_evidence(self):
        f = self.release
        task = f.store.submit("held source", 1)
        f.store.hold_constraint_task(task, "replacement result", {"status": "unresolved_partial"})
        bridge = MemoryBridge(f.config)
        staged = bridge.stage_experience(task, "held source", "original evidence", [])
        approved = hashlib.sha256(staged.read_bytes()).hexdigest()
        f.store.stage_memory_candidate(task, str(staged), approved)
        return task, staged

    def test_an_unreadable_evidence_file_is_never_overwritten_or_removed(self):
        task, staged = self._held_task_with_evidence()
        original = staged.read_bytes()
        staged.chmod(0o200)  # 真实权限：文件存在，但本次进程读不出来

        code, payload = _run_cli(
            ["--config", str(self.release.config.path), "constraint-hold-release", task]
        )

        staged.chmod(0o600)
        self.assertEqual(1, code, payload)
        self.assertEqual("blocked", payload["status"])
        self.assertFalse(payload["retry_safe"], "无法保存原字节时不得宣称可重试")
        self.assertTrue(payload.get("manual_action_required"), payload)
        self.assertTrue(staged.exists(), "读不到的证据文件不得被当成不存在而删除")
        self.assertEqual(original, staged.read_bytes(), "原字节必须原样保留")
        self.assertEqual("pending", self.release.store.get_constraint_hold(task)["status"])

    def test_restore_refuses_to_overwrite_evidence_changed_by_someone_else(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.md"
            path.write_bytes(b"someone else's newer content\n")
            snapshot = (True, b"original evidence\n", 0o600)
            ours = hashlib.sha256(b"the write this command staged\n").hexdigest()

            with self.assertRaises(EvidenceRestoreError) as caught:
                _restore_evidence(path, snapshot, ours)

            self.assertIn("changed after this command staged it", str(caught.exception))
            self.assertEqual(b"someone else's newer content\n", path.read_bytes())

    def test_a_restore_that_cannot_finish_raises_instead_of_passing_silently(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.md"
            path.write_bytes(b"the write this command staged\n")
            snapshot = (True, b"original evidence\n", 0o600)
            ours = hashlib.sha256(b"the write this command staged\n").hexdigest()

            with patch.object(Path, "write_bytes", side_effect=OSError("disk full")):
                with self.assertRaises(EvidenceRestoreError) as caught:
                    _restore_evidence(path, snapshot, ours)

            self.assertIn("cannot restore", str(caught.exception))

    def test_restore_reinstates_the_original_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.md"
            path.write_bytes(b"the write this command staged\n")
            snapshot = (True, b"original evidence\n", 0o640)

            _restore_evidence(path, snapshot, hashlib.sha256(b"the write this command staged\n").hexdigest())

            self.assertEqual(b"original evidence\n", path.read_bytes())
            self.assertEqual(0o640, path.stat().st_mode & 0o777)

    def test_a_cli_restore_failure_is_reported_as_manual_action(self):
        task, staged = self._held_task_with_evidence()

        def failing_place(path, content, mode=0o600):
            raise OSError("disk full while restoring the evidence")

        def failing_release(*args, **kwargs):
            raise ValueError("injected refusal after staging")

        with patch("xiaoba_agentos.cli._place_evidence_bytes", failing_place), patch.object(
            EventStore, "release_constraint_hold", failing_release
        ):
            code, payload = _run_cli(
                ["--config", str(self.release.config.path), "constraint-hold-release", task]
            )

        self.assertEqual(1, code, payload)
        self.assertEqual("reconciliation_required", payload["status"])
        self.assertFalse(payload["retry_safe"])
        self.assertTrue(payload.get("manual_action_required"), payload)
        self.assertIn("restore_error", payload)

    def test_evidence_written_after_the_atomic_claim_is_kept_and_reported(self):
        """认领之后落下的内容：旧的 read-then-write 会覆盖它，这里必须保留并报错。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.md"
            path.write_bytes(b"the write this command staged\n")
            snapshot = (True, b"original evidence\n", 0o600)
            ours = hashlib.sha256(b"the write this command staged\n").hexdigest()
            real_replace = os.replace
            observed = {}

            def claim_then_peer_write(src, dst):
                result = real_replace(src, dst)
                if str(src) == str(path) and "done" not in observed:
                    path.write_bytes(b"someone else's newer content after the claim\n")
                    observed["done"] = True
                return result

            with patch("xiaoba_agentos.cli.os.replace", side_effect=claim_then_peer_write):
                with self.assertRaises(EvidenceRestoreError) as caught:
                    _restore_evidence(path, snapshot, ours)

            self.assertTrue(observed.get("done"), "必须在原子认领之后写入")
            self.assertEqual(
                b"someone else's newer content after the claim\n", path.read_bytes(),
                "认领之后写入的内容不得被覆盖",
            )
            self.assertIn("changed after this command staged it", str(caught.exception))

    def test_a_provably_uncommitted_release_still_restores_the_evidence(self):
        """可证明未提交时，还原照旧：活路径回到原字节，这是必须保留的正常行为。

        丙下这一步不再"干净"：被换掉的那个对象（本次自己写下的证据）以保留物
        留在原目录，并由自检列出，所以结果必须同时报 manual_action_required。
        旧断言"无 manual_action_required"正是丙要推翻的那条。
        """
        task, staged = self._held_task_with_evidence()
        original = staged.read_bytes()

        def failing_release(*args, **kwargs):
            raise ValueError("injected refusal after staging")

        with patch.object(EventStore, "release_constraint_hold", failing_release):
            code, payload = _run_cli(
                ["--config", str(self.release.config.path), "constraint-hold-release", task]
            )

        self.assertEqual(1, code, payload)
        self.assertEqual("blocked", payload["status"])
        self.assertTrue(payload["retry_safe"], "明确未提交且活路径已还原，仍可重试")
        self.assertEqual(original, staged.read_bytes(), "可证明未提交时必须还原活路径的原字节")
        self.assertEqual("pending", self.release.store.get_constraint_hold(task)["status"])
        # 丙：被换下来的对象没有被删掉，而是留在私有名下并可被自检找到。
        kept = _retained_in(staged.parent)
        self.assertEqual(
            [staged.name], [entry["original"] for entry in kept.values()],
            "被换下来的证据对象必须以保留物留在原目录，且只留这一个",
        )
        retained_path = next(iter(kept.values()))["path"]
        self.assertNotEqual(str(staged), str(retained_path))
        self.assertIn(b"replacement result", retained_path.read_bytes(), "保留的必须是这次自己写下的内容")
        self.assertTrue(payload.get("manual_action_required"), payload)
        self.assertIn("受限方案丙", payload.get("retained_note", ""))
        self.assertIn(
            str(retained_path),
            json.dumps(payload.get("retained_objects") or payload, ensure_ascii=False),
            "结果里必须给出保留物的真实位置",
        )
        report = retained_objects.collect(self.release.config)
        self.assertIn(str(retained_path), [item["retained_path"] for item in report["objects"]], report)
        self.assertEqual(0, report["anomaly_count"], report)

    def test_an_unknown_release_outcome_keeps_the_new_evidence_and_the_commit(self):
        """真实提交 + 维护退出报错 + 读回不可用：不得把旧字节写回去。"""
        f = self.release
        task = f.store.submit("held source", 1)
        f.store.hold_constraint_task(task, "replacement result", {"status": "unresolved_partial"})
        staged = MemoryBridge(f.config).stage_experience(task, "held source", "original evidence", [])
        original_bytes = staged.read_bytes()
        f.store.stage_memory_candidate(task, str(staged), hashlib.sha256(original_bytes).hexdigest())

        real_permissions = EventStore._secure_permissions
        real_hold_get = EventStore.get_constraint_hold
        real_candidate_get = EventStore.get_memory_candidate
        observed = {"injected_after_actual_commit": False}

        def permissions(store):
            real_permissions(store)
            if store.path == f.store.path and not observed["injected_after_actual_commit"]:
                with store.read_only() as connection:
                    row = connection.execute(
                        "SELECT status FROM constraint_holds WHERE task_id=?", (task,)
                    ).fetchone()
                if row and row[0] == "released":
                    observed["injected_after_actual_commit"] = True
                    raise OSError("maintenance error after the actual release commit")

        def unreadable_hold(store, task_id, *args, **kwargs):
            if observed["injected_after_actual_commit"]:
                raise sqlite3.OperationalError("injected readback unavailable")
            return real_hold_get(store, task_id, *args, **kwargs)

        def unreadable_candidate(store, task_id, *args, **kwargs):
            if observed["injected_after_actual_commit"]:
                raise sqlite3.OperationalError("injected readback unavailable")
            return real_candidate_get(store, task_id, *args, **kwargs)

        with patch.object(EventStore, "_secure_permissions", permissions), patch.object(
            EventStore, "get_constraint_hold", unreadable_hold
        ), patch.object(EventStore, "get_memory_candidate", unreadable_candidate):
            code, payload = _run_cli(
                ["--config", str(f.config.path), "constraint-hold-release", task]
            )

        self.assertTrue(observed["injected_after_actual_commit"], "探针必须在真正提交之后触发")
        self.assertEqual(1, code, payload)
        self.assertEqual("reconciliation_required", payload["status"])
        self.assertFalse(payload["retry_safe"])
        self.assertTrue(payload.get("manual_action_required"), payload)
        self.assertIn("readback_error", payload)

        candidate = f.store.get_memory_candidate(task)
        self.assertEqual("released", f.store.get_constraint_hold(task)["status"])
        self.assertEqual(
            candidate["evidence_sha256"], hashlib.sha256(staged.read_bytes()).hexdigest(),
            "提交未知时不得把旧字节写回：数据库与文件必须仍然一致",
        )
        self.assertNotEqual(original_bytes, staged.read_bytes(), "不得恢复旧证据字节")

    def test_an_occupied_rollback_name_keeps_its_content_and_names_the_location(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path = base / "evidence.md"
            path.write_bytes(b"the write this command staged\n")
            ours = hashlib.sha256(b"the write this command staged\n").hexdigest()
            occupied = base / f".evidence.md.{os.getpid()}.rollback"
            occupied.write_bytes(b"an earlier command's kept copy\n")

            with self.assertRaises(EvidenceRestoreError) as caught:
                _restore_evidence(path, (False, None, None), ours)

            self.assertIn(
                str(occupied), str(caught.exception), "异常正文必须指出被占用名字的真实位置"
            )
            self.assertEqual(b"an earlier command's kept copy\n", occupied.read_bytes())
            self.assertEqual(b"the write this command staged\n", path.read_bytes(), "认领未开始，证据必须原样")

    def test_an_occupied_restore_name_is_never_truncated_and_the_capture_is_put_back(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path = base / "evidence.md"
            path.write_bytes(b"the write this command staged\n")
            ours = hashlib.sha256(b"the write this command staged\n").hexdigest()
            occupied = base / f".evidence.md.{os.getpid()}.restore"
            occupied.write_bytes(b"an earlier command's kept copy\n")

            with self.assertRaises(EvidenceRestoreError) as caught:
                _restore_evidence(path, (True, b"original evidence\n", 0o600), ours)

            self.assertIn(
                str(occupied), str(caught.exception), "异常正文必须指出被占用暂存名的真实位置"
            )
            self.assertEqual(b"an earlier command's kept copy\n", occupied.read_bytes())
            self.assertEqual(
                b"the write this command staged\n",
                path.read_bytes(),
                "放不回去时必须把捕获对象放回，场景不变",
            )

    def test_a_failed_evidence_put_back_names_the_kept_location(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path = base / "evidence.md"
            path.write_bytes(b"the write this command staged\n")
            ours = hashlib.sha256(b"the write this command staged\n").hexdigest()
            real_read = Path.read_bytes
            real_replace = os.replace
            observed = {}

            def read_then_swap(path_under_test):
                data = real_read(path_under_test)
                if path_under_test == path and "swapped" not in observed:
                    observed["swapped"] = True
                    path.write_bytes(b"someone else's newer content\n")
                return data

            def claim_then_recreate(src, dst):
                result = real_replace(src, dst)
                if Path(src) == path and "claim" not in observed:
                    observed["claim"] = Path(dst)
                    path.write_bytes(b"the write this command staged\n")
                return result

            with patch.object(Path, "read_bytes", read_then_swap), patch(
                "xiaoba_agentos.cli.os.replace", side_effect=claim_then_recreate
            ):
                with self.assertRaises(EvidenceRestoreError) as caught:
                    _restore_evidence(path, (False, None, None), ours)

            claim = observed["claim"]
            self.assertIn(
                f"the captured original object is kept at {claim}",
                str(caught.exception),
                "放回失败不得静默：异常正文必须含真实保留位置",
            )
            self.assertEqual(b"someone else's newer content\n", claim.read_bytes())

    def test_a_second_evidence_restore_never_clobbers_the_first_kept_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path = base / "evidence.md"
            path.write_bytes(b"the write this command staged\n")
            ours = hashlib.sha256(b"the write this command staged\n").hexdigest()
            real_read = Path.read_bytes
            real_replace = os.replace
            observed = {}

            def read_then_swap(path_under_test):
                data = real_read(path_under_test)
                if path_under_test == path and "swapped" not in observed:
                    observed["swapped"] = True
                    path.write_bytes(b"someone else's newer content\n")
                return data

            def claim_then_recreate(src, dst):
                result = real_replace(src, dst)
                if Path(src) == path and "claim" not in observed:
                    observed["claim"] = Path(dst)
                    path.write_bytes(b"the write this command staged\n")
                return result

            with patch.object(Path, "read_bytes", read_then_swap), patch(
                "xiaoba_agentos.cli.os.replace", side_effect=claim_then_recreate
            ):
                with self.assertRaises(EvidenceRestoreError):
                    _restore_evidence(path, (False, None, None), ours)

            claim = observed["claim"]
            self.assertEqual(b"someone else's newer content\n", claim.read_bytes())

            with self.assertRaises(EvidenceRestoreError) as caught:
                _restore_evidence(path, (False, None, None), ours)

            self.assertEqual(
                b"someone else's newer content\n",
                claim.read_bytes(),
                "第二次恢复不得覆盖或删除第一次保留的拷贝",
            )
            self.assertIn(str(claim), str(caught.exception), "必须报出被占用名字的真实位置")
            self.assertEqual(b"the write this command staged\n", path.read_bytes())


class RetainedFdControlGroupTest(_GateFixture):
    """缺口 6：认领前就持有 fd 的写者，其字节必须仍在同一个原对象里。

    真实序列：独立进程在认领**之前** `os.open(path, O_RDWR)` 拿到 fd 并一直持有；
    本实现读活路径判归属 → `os.replace` 把对象认领到私有名字 → 写者借旧 fd 写入
    新内容并 fsync → 本实现返回并上报保留位置。丙不再 unlink 被认领的对象，所以
    这些字节落在保留物里，而不是随 inode 消失。子进程自报 inode，用来证明保留物
    就是那个原对象本身，不是复制瞬时字节的副本。

    "跨重启"这里指的是普通重启（换进程、换 PID 重新枚举，已经验证）；未做断电
    持久性验证——`fsync` 只是提交给内核/设备，真断电下的持久性本轮不声称。
    """

    NEW = b"NEW_CONTENT_WRITTEN_VIA_THE_OLD_FD\n"
    MORE = b"MORE_BYTES_APPENDED_AFTER_THE_RECOVERY_RETURNED\n"

    def _start_fd_writer(self, path):
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", _FD_WRITER_CHILD, str(path), self.NEW.hex(), self.MORE.hex()],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._stop_fd_writer, process)
        line = process.stdout.readline().strip()
        self.assertTrue(line.startswith("READY "), line)
        return process, int(line.split()[1])

    def _stop_fd_writer(self, process):
        try:
            if process.poll() is None:
                process.stdin.write("QUIT\n")
                process.stdin.flush()
                process.wait(timeout=30)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=30)
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except OSError:
                pass

    def _command(self, process, command, expected):
        process.stdin.write(command + "\n")
        process.stdin.flush()
        line = process.stdout.readline().strip()
        self.assertEqual(expected, line)

    def test_a_writer_holding_the_fd_keeps_its_bytes_in_the_retained_object(self):
        gate = self.gate
        graph_dir = Path(gate.config.raw["memory"]["knowlp_graph_dir"])
        target = graph_dir / "fd_control.json"
        previous = b"the graph output that was there before this call\n"
        target.write_bytes(previous)
        staging = tempfile.TemporaryDirectory()
        self.addCleanup(staging.cleanup)
        source = Path(staging.name) / "fd_control.json"
        source.write_bytes(b"the bytes this call installed over it\n")
        bridge = gate.bridge
        bridge._install_index_output(graph_dir, source)
        record = bridge._index_installs["fd_control.json"]

        process, writer_inode = self._start_fd_writer(target)
        observed = {}
        real_place = MemoryBridge._place_original

        def place_then_write(bridge_self, path, content, mode=0o600):
            real_place(path, content, mode)
            if "written" not in observed:
                observed["written"] = True
                self._command(process, "WRITE", "WROTE")

        bridge._retention_context = {"task_id": FD_CONTROL_TASK}
        problems = []
        with patch.object(MemoryBridge, "_place_original", place_then_write):
            bridge._undo_installed_file(
                target,
                record["installed"],
                record["original"],
                problems,
                operation="memory.index_output",
            )
        self.assertTrue(observed.get("written"), "写者必须在认领之后、本实现返回之前写入")
        self._command(process, "APPEND", "APPENDED")

        self.assertEqual(previous, target.read_bytes(), "活路径必须回到原字节")
        kept = _retained_in(graph_dir)
        self.assertEqual(1, len(kept), kept)
        entry = next(iter(kept.values()))
        claim = entry["path"]
        self.assertEqual("fd_control.json", entry["original"])
        self.assertEqual(
            self.NEW + self.MORE,
            claim.read_bytes(),
            "旧 fd 在认领之后写入与追加的字节必须落在保留物里",
        )
        self.assertEqual(
            writer_inode,
            claim.stat().st_ino,
            "保留物必须是同一个原对象（同一个 inode），不是复制瞬时字节的副本",
        )
        self.assertTrue(
            any("kept at" in problem and claim.name in problem for problem in problems), problems
        )

        report = self._assert_self_check_lists([claim])
        self.assertEqual(0, report["anomaly_count"], report)
        self.assertTrue(report["enumeration_ok"], report)
        item = next(entry for entry in report["objects"] if entry["state"] != "record_without_object")
        self.assertEqual(FD_CONTROL_TASK, item["task_id"])
        self.assertEqual("memory.index_output", item["operation"])
        self.assertEqual("matched", item["journal"])
        self.assertEqual(
            len(self.NEW + self.MORE),
            item["size_bytes"],
            "自检报的大小必须反映旧 fd 后来写进去的字节",
        )
        self.assertFalse(item["content_frozen"])
        self.assertFalse(item["content_copied"])
        self.assertEqual("not_recorded", item["content_hash"])
        self.assertNotEqual(
            item["written_bytes_sha256"],
            hashlib.sha256(claim.read_bytes()).hexdigest(),
            "登记的是本次调用写下的字节，不能当成对象当前内容的冻结值",
        )
        self.assertEqual(
            0,
            report["anomaly_count"],
            "旧 fd 让对象内容漂移之后，自检不得把它当成记录失配",
        )

        restarted = self._self_check_in_new_process(gate.config)
        restarted_item = next(
            entry for entry in restarted["objects"] if entry["retained_path"] == str(claim)
        )
        self.assertEqual(len(self.NEW + self.MORE), restarted_item["size_bytes"], restarted_item)

    def test_a_writer_holding_the_fd_keeps_its_bytes_in_the_retained_evidence(self):
        gate = self.gate
        path = gate.bridge.outbox / "fd_evidence_control.md"
        previous = b"the evidence that existed before this command staged its write\n"
        staged_write = b"the write this command staged\n"
        path.write_bytes(staged_write)
        path.chmod(0o600)
        ours = hashlib.sha256(staged_write).hexdigest()

        process, writer_inode = self._start_fd_writer(path)
        observed = {}
        real_place = cli_module._place_evidence_bytes

        def place_then_write(path_under_test, content, mode=0o600):
            real_place(path_under_test, content, mode)
            if "written" not in observed:
                observed["written"] = True
                self._command(process, "WRITE", "WROTE")

        with patch.object(cli_module, "_place_evidence_bytes", place_then_write):
            retained = _restore_evidence(
                path,
                (True, previous, 0o600),
                ours,
                config=gate.config,
                task_id=FD_CONTROL_TASK,
                operation="evidence.staged",
            )
        self.assertTrue(observed.get("written"), "写者必须在认领之后、本命令返回之前写入")
        self._command(process, "APPEND", "APPENDED")

        kept = _retained_in(path.parent)
        self.assertEqual(1, len(kept), kept)
        claim = next(iter(kept.values()))["path"]
        self.assertEqual(
            [str(claim)],
            [os.path.realpath(item) for item in retained],
            "命令必须把保留物的真实位置交出来",
        )
        self.assertEqual(previous, path.read_bytes(), "活路径必须回到原字节")
        self.assertEqual(self.NEW + self.MORE, claim.read_bytes())
        self.assertEqual(writer_inode, claim.stat().st_ino)

        report = self._self_check(gate.config)
        item = next(entry for entry in report["objects"] if entry["retained_path"] == str(claim))
        self.assertEqual(FD_CONTROL_TASK, item["task_id"])
        self.assertEqual("evidence.staged", item["operation"])
        self.assertEqual(0, report["anomaly_count"], report)
        self.assertTrue(report["enumeration_ok"], report)


class CrossProcessSelfCheckTest(_GateFixture):
    """缺口 3/4：自检必须列出保留物与全部异常，并且跨重启仍然算数。

    验证方式是新进程（不同 PID）只看磁盘重跑一遍；记录缺失/失配、对象缺失、
    不可读四类异常都不得被静默漏掉，而记录本身只存元数据、不复制正文。
    """

    def test_a_new_process_enumerates_every_retained_object_across_a_restart(self):
        self._failing_publication()
        expected = self._expected_retained()
        self.assertTrue(expected, "夹具必须真的留下保留物")

        payload = self._self_check_in_new_process(self.gate.config)

        listed = sorted(
            item["retained_path"]
            for item in payload["objects"]
            if item["state"] != "record_without_object"
        )
        self.assertEqual(sorted(expected), listed)
        self.assertEqual(0, payload["anomaly_count"], payload)
        self.assertTrue(payload["enumeration_ok"], payload)
        self.assertEqual(len(expected), payload["retained_count"], payload)
        self.assertFalse(payload["ok"], payload)
        self.assertTrue(payload["manual_action_required"], payload)
        for item in payload["objects"]:
            self.assertTrue(item["retained_path"].startswith("/"), item)
            self.assertEqual("pending", item["manual_verification"], item)
            self.assertFalse(item["content_frozen"], item)
            self.assertFalse(item["content_copied"], item)
            self.assertEqual("matched", item["journal"], item)
            self.assertIn(item["operation"], ("memory.note", "memory.catalog", "memory.index_output"))
            self.assertNotEqual(
                "not_recorded",
                item["written_bytes_sha256"],
                "被替换内容的摘要必须登记，供人工核对替换是否属实",
            )
            self.assertEqual("not_recorded", item["content_hash"], item)
        self.assertFalse(payload["cleanup"]["automatic"], payload)
        self.assertIsNone(payload["cleanup"]["expires"], payload)
        self.assertIn("never deleted automatically", payload["cleanup"]["note"])

    def test_a_missing_discovery_record_is_reported_and_the_objects_stay_listed(self):
        self._failing_publication()
        expected = self._expected_retained()
        journal = retained_objects.journal_path(self.gate.config)
        self.assertTrue(journal.is_file(), journal)
        journal.unlink()

        payload = self._self_check_in_new_process(self.gate.config)

        self.assertEqual(
            sorted(expected),
            sorted(item["retained_path"] for item in payload["objects"]),
            "记录没了，磁盘上的保留物也必须照旧列出来",
        )
        self.assertEqual(
            ["record_missing"] * len(expected),
            sorted(anomaly["kind"] for anomaly in payload["anomalies"]),
            payload,
        )
        self.assertFalse(payload["enumeration_ok"], "记录缺失必须让自检说它没数清")
        self.assertTrue(payload["manual_action_required"], payload)
        for item in payload["objects"]:
            self.assertEqual("missing", item["journal"], item)
            self.assertEqual("unknown", item["task_id"], "没有记录时不得编造任务号")
            self.assertEqual("unknown", item["operation"], item)

    def test_a_mismatched_record_is_reported_instead_of_trusted(self):
        self._failing_publication()
        journal = retained_objects.journal_path(self.gate.config)
        rows = [
            json.loads(line)
            for line in journal.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        first = rows[0]
        forged = dict(first)
        forged["original_path"] = str(Path(first["original_path"]).with_name("a-different-original.json"))
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(forged, ensure_ascii=False, sort_keys=True) + "\n")

        payload = self._self_check(self.gate.config)

        self.assertIn("record_mismatch", [anomaly["kind"] for anomaly in payload["anomalies"]], payload)
        self.assertIn("2 records name the same retained path", json.dumps(payload, ensure_ascii=False))
        self.assertFalse(payload["enumeration_ok"], payload)
        self.assertTrue(payload["manual_action_required"], payload)
        mismatched = next(
            item
            for item in payload["objects"]
            if item["retained_path"] == os.path.realpath(first["retained_path"])
        )
        self.assertEqual("mismatched", mismatched["journal"], mismatched)
        self.assertFalse(mismatched["readable"] is None)

    def test_a_record_without_an_object_is_reported_not_silently_dropped(self):
        self._failing_publication()
        expected = self._expected_retained()
        for entry in expected.values():
            entry["path"].unlink()

        payload = self._self_check_in_new_process(self.gate.config)

        orphans = [item for item in payload["objects"] if item["state"] == "record_without_object"]
        self.assertEqual(len(expected), len(orphans), payload)
        self.assertEqual(sorted(expected), sorted(item["retained_path"] for item in orphans))
        self.assertEqual(
            ["record_without_object"] * len(expected),
            sorted(anomaly["kind"] for anomaly in payload["anomalies"]),
            payload,
        )
        self.assertEqual(0, payload["retained_count"], payload)
        self.assertFalse(payload["enumeration_ok"], payload)
        self.assertFalse(payload["ok"], payload)
        for item in orphans:
            self.assertFalse(item["readable"], item)

    def test_an_unreadable_retained_object_is_listed_with_a_problem(self):
        self._failing_publication()
        expected = self._expected_retained()
        victim = sorted(expected.values(), key=lambda entry: str(entry["path"]))[0]
        victim["path"].chmod(0o000)
        try:
            payload = self._self_check(self.gate.config)

            item = next(
                entry for entry in payload["objects"] if entry["retained_path"] == str(victim["path"])
            )
            self.assertFalse(item["readable"], item)
            self.assertTrue(
                any("cannot be opened" in problem for problem in item["problems"]), item
            )
            self.assertEqual("pending", item["manual_verification"], item)
            self.assertTrue(
                any(str(victim["path"]) in problem for problem in payload["problems"]), payload
            )
            self.assertFalse(
                payload["enumeration_ok"], "读不到的对象必须让自检说它没数清，而不是漏掉"
            )
            self.assertEqual(
                len(expected), payload["retained_count"], "对象仍必须被列出，不得静默遗漏"
            )
            self.assertTrue(payload["manual_action_required"], payload)
        finally:
            try:
                victim["path"].chmod(0o600)
            except FileNotFoundError:
                pass

    def test_the_discovery_record_holds_metadata_only_and_never_the_content(self):
        task_id, _ = self._failing_publication()
        journal = retained_objects.journal_path(self.gate.config)
        raw = journal.read_text(encoding="utf-8")
        claims = list(self._expected_retained().values())
        self.assertTrue(
            any(BODY_APPROVED.encode("utf-8") in entry["path"].read_bytes() for entry in claims),
            "夹具必须留下含真实笔记正文的保留物",
        )
        self.assertIn(task_id, raw, "记录必须带上任务号，否则人工无从核对")

        for line in raw.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            self.assertEqual(1, row["schema"])
            self.assertEqual("retained_object", row["kind"])
            self.assertFalse(row["content_frozen"])
            self.assertFalse(row["content_copied"])
            self.assertNotIn("content", row)
            self.assertIn("reason", row)
            self.assertIn("operation", row)
            self.assertEqual("metadata only", row["content_note"].split(":")[0])
        self.assertNotIn(BODY_APPROVED, raw, "记录里绝不能出现笔记正文")
        self.assertNotIn("partial index update from this invocation", raw, "记录里绝不能出现图输出正文")

    def test_an_unwritable_report_keeps_every_object_and_its_real_location(self):
        self._failing_publication()
        expected = self._expected_retained()
        directory = retained_objects.journal_path(self.gate.config).parent
        directory.chmod(0o500)
        try:
            payload = self._self_check(self.gate.config)
            written, error = retained_objects.write_report(self.gate.config, payload)

            self.assertIsNone(written, written)
            self.assertIsNotNone(error)
            self.assertIn(str(retained_objects.report_path(self.gate.config)), error)
            self.assertFalse(retained_objects.report_path(self.gate.config).exists())
            self.assertEqual(
                sorted(expected),
                self._listed_paths(),
                "报告写不出来不影响保留物本身，位置照旧逐个列出",
            )
            self.assertTrue(payload["manual_action_required"], payload)
        finally:
            directory.chmod(0o700)


class RetrievalExclusionTest(_GateFixture):
    """缺口 5：保留物里有真实笔记/候选内容，检索与索引入口必须隔离它们。

    判定不能只靠后缀：这里把被认领的对象改名成它原本的 `.md` 名字（正文是完整
    有效的笔记：status active + source_task_id），只留自检记录说明它是保留物。
    随后用真实构建器与真实读取入口验证它进不去；去掉记录后同一个文件立刻被接受
    ——证明拦住它的正是保留物清单，而不是后缀或内容特征。最后验证成功发布既不
    删除旧保留物、也不新增保留物。
    """

    def _rename_a_retained_note(self, task_id):
        gate = self.gate
        directory = gate.vault / gate.config.raw["memory"]["obsidian_subdir"]
        claims = [
            entry
            for entry in self._expected_retained().values()
            if b"source_task_id" in entry["path"].read_bytes()
        ]
        self.assertTrue(claims, "夹具必须留下含笔记正文的保留物")
        claim = claims[0]
        original_name = retained_objects.parse_retained_name(claim["path"].name)["original"]
        renamed = directory / original_name
        self.assertEqual(".md", renamed.suffix)
        os.replace(claim["path"], renamed)
        journal = retained_objects.journal_path(gate.config)
        try:
            journal.unlink()
        except FileNotFoundError:
            pass
        error = retained_objects.record_retained(
            gate.config,
            retained_path=renamed,
            original_path=renamed,
            operation="memory.note",
            reason="the user renamed this retained object by hand, so only the record names it",
            task_id=task_id,
        )
        self.assertIsNone(error, error)
        return renamed

    def _built_index(self, extra_args=()):
        staging = tempfile.TemporaryDirectory()
        self.addCleanup(staging.cleanup)
        graph = Path(staging.name)
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(IMPLEMENTATION_ROOT / "scripts/build_knowlp_index.py"),
                "--vault",
                str(self.gate.vault),
                "--graph-dir",
                str(graph),
                *extra_args,
            ],
            capture_output=True,
            text=True,
            cwd=str(IMPLEMENTATION_ROOT),
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads((graph / "meta_index.json").read_text(encoding="utf-8"))

    def test_a_renamed_retained_object_is_excluded_by_its_record_not_by_its_suffix(self):
        task_id, _ = self._failing_publication()
        renamed = self._rename_a_retained_note(task_id)
        self.assertFalse(retained_objects.is_retained_name(renamed.name), "名字本身不再像保留物")
        self.assertTrue(
            retained_objects.is_retained_path(self.gate.config, renamed),
            "登记门必须认出它",
        )

        bridge = self.gate.bridge
        self.assertIsNone(
            bridge._verified_source({"path": str(renamed)}),
            "哪怕是完整有效的笔记，被认领的对象也不得成为可检索来源",
        )
        self.assertIsNone(bridge.find_note(self.gate.vault, task_id), "被撤销的笔记不得被找回")

        paths = [item["path"] for item in self._built_index([
            "--retained-ledger", str(retained_objects.journal_path(self.gate.config))
        ])]
        self.assertTrue(any("经验索引" in path for path in paths), paths)
        self.assertFalse(any(renamed.name in path for path in paths), paths)

        # 正对照：去掉记录、并把它当回正常笔记（补上父链接）后，它就该进索引。
        retained_objects.journal_path(self.gate.config).unlink()
        source = bridge._verified_source({"path": str(renamed)})
        self.assertIsNotNone(source, "去掉记录后同一个文件是合法来源")
        self.assertEqual(task_id, source["source_task_id"])
        catalog = self.gate.vault / self.gate.config.raw["memory"]["catalog_note"]
        link = "- [[" + str(renamed.relative_to(self.gate.vault).with_suffix("")) + "]]"
        catalog.write_text(
            catalog.read_text(encoding="utf-8").rstrip() + "\n" + link + "\n", encoding="utf-8"
        )
        paths = [item["path"] for item in self._built_index()]
        self.assertTrue(any(renamed.name in path for path in paths), paths)

    def test_the_index_fingerprint_ignores_a_renamed_retained_object(self):
        self._failing_publication()
        gate = self.gate
        graph_dir = Path(gate.config.raw["memory"]["knowlp_graph_dir"])
        kept = _retained_in(graph_dir)
        self.assertTrue(kept, "夹具必须有图保留物")
        entry = next(iter(kept.values()))
        renamed = graph_dir / "renamed_retained_output.json"
        os.replace(entry["path"], renamed)
        journal = retained_objects.journal_path(gate.config)
        journal.unlink()
        error = retained_objects.record_retained(
            gate.config,
            retained_path=renamed,
            original_path=renamed,
            operation="memory.index_output",
            reason="the user renamed this retained object by hand",
        )
        self.assertIsNone(error, error)

        expected = hashlib.sha256()
        for path in sorted(graph_dir.iterdir()):
            if path.name.startswith(".") or path.is_symlink() or not path.is_file():
                continue
            if path == renamed:
                continue
            expected.update(path.name.encode("utf-8"))
            expected.update(b"\0")
            expected.update(path.read_bytes())
        self.assertEqual(
            expected.hexdigest(),
            gate.bridge.index_sha256(),
            "被改名的保留物不得进入索引指纹",
        )

    def test_a_later_successful_publication_keeps_the_retained_objects_and_adds_none(self):
        self._failing_publication()
        before = {
            key: entry["path"].read_bytes() for key, entry in self._expected_retained().items()
        }
        self.assertTrue(before, "夹具必须留下保留物")

        task_id, _, approved = self.gate._candidate()
        self.gate._approve(task_id, approved, ("delivery", "final"))
        code, payload = self.gate._cli(["publish-memory", "--task-id", task_id])

        self.assertEqual(0, code, payload)
        self.assertEqual("published", payload["status"])
        after = self._expected_retained()
        self.assertEqual(sorted(before), sorted(after), "成功发布不得删除或改名任何保留物")
        for key, entry in after.items():
            self.assertEqual(before[key], entry["path"].read_bytes(), f"{key} 的字节不得被改动")
        report = self._self_check()
        self.assertFalse(report["ok"], report)
        self.assertTrue(report["manual_action_required"], report)
        self.assertEqual(len(before), report["retained_count"], report)

    def test_a_clean_publication_leaves_no_retained_object_behind(self):
        gate = self.gate
        task_id, _, approved = gate._candidate()
        gate._approve(task_id, approved, ("delivery", "final"))
        code, payload = gate._cli(["publish-memory", "--task-id", task_id])

        self.assertEqual(0, code, payload)
        self.assertEqual("published", payload["status"])
        self.assertFalse(payload.get("manual_action_required"), payload)
        report = self._self_check()
        self.assertTrue(report["ok"], report)
        self.assertEqual(0, report["retained_count"], report)
        self.assertEqual([], report["objects"], report)
        self.assertEqual([], report["problems"], report)
        self.assertTrue(report["enumeration_ok"], report)
        for directory in retained_objects.retained_scan_dirs(gate.config):
            self.assertEqual({}, _retained_in(directory), directory)


class RepeatedRecoveryTest(_GateFixture):
    """缺口 3：同一次失败的内层+外层回滚、以及重复恢复，都不得销毁已有保留物。"""

    def test_an_inner_and_an_outer_rollback_leave_one_retained_object_per_output(self):
        gate = self.gate
        task_id, _, approved = gate._candidate()
        gate._approve(task_id, approved, ("delivery", "final"))
        graph_dir = Path(gate.config.raw["memory"]["knowlp_graph_dir"])
        before = dict(gate._graph_bytes())

        def inner_rollback_then_fail(bridge, vault):
            files = sorted(path for path in graph_dir.iterdir() if path.is_file())
            staging = tempfile.TemporaryDirectory()
            self.addCleanup(staging.cleanup)
            built = Path(staging.name)
            for path in files:
                (built / path.name).write_bytes(b"first-layer index update\n")
            for path in files:
                bridge._install_index_output(graph_dir, built / path.name)
            # 内层回滚（重建自身的边界）已经把它们认领过了；外层随后还会再来一次。
            bridge._rollback_index_outputs(graph_dir, bridge._index_output_snapshot(graph_dir))
            raise RuntimeError("injected failure after the inner rollback ran")

        with patch.object(MemoryBridge, "_rebuild_and_verify_index", inner_rollback_then_fail):
            code, payload = gate._cli(["publish-memory", "--task-id", task_id])

        self.assertEqual(1, code, payload)
        self.assertEqual("reconciliation_required", payload["status"])
        self.assertEqual(before, self._live_graph_bytes(), "活文件必须回到原字节")
        live = sorted(self._live_graph_bytes())
        kept = _retained_in(graph_dir)
        self.assertEqual(len(live), len(kept), "每个被写过的输出恰好留下一个保留物")
        self.assertEqual(live, sorted(entry["original"] for entry in kept.values()))
        for entry in kept.values():
            self.assertEqual(
                b"first-layer index update\n",
                entry["path"].read_bytes(),
                "保留物必须是被内层认领的那个对象本身",
            )
        report = self._self_check()
        self.assertEqual(0, report["anomaly_count"], report)
        self.assertTrue(report["enumeration_ok"], report)
        self.assertEqual(
            len(live) + 2,
            report["retained_count"],
            "自检要数上所有被替换过的对象：图输出之外还有这篇笔记和索引笔记",
        )
        self.assertEqual(
            {"memory.note", "memory.catalog", "memory.index_output"},
            {item["operation"] for item in report["objects"]},
            report,
        )
        self.assertEqual(0, gate._events(task_id, "memory.published"))


class RetainedEvidenceReleaseTest(unittest.TestCase):
    """证据侧：正常成功不留残留；重复恢复不得销毁第一次留下的保留物。"""

    def setUp(self):
        self.release = release_fixture.ConstraintReleaseCliTest()
        self.release.setUp()

    def tearDown(self):
        self.release.tearDown()

    def _held_task_with_evidence(self, result="replacement result"):
        f = self.release
        task = f.store.submit("held source", 1)
        f.store.hold_constraint_task(task, result, {"status": "unresolved_partial"})
        bridge = MemoryBridge(f.config)
        staged = bridge.stage_experience(task, "held source", "original evidence", [])
        approved = hashlib.sha256(staged.read_bytes()).hexdigest()
        f.store.stage_memory_candidate(task, str(staged), approved)
        return task, staged

    def test_a_successful_release_leaves_no_retained_object(self):
        task, staged = self._held_task_with_evidence()
        original = staged.read_bytes()

        code, payload = _run_cli(
            ["--config", str(self.release.config.path), "constraint-hold-release", task]
        )

        self.assertEqual(0, code, payload)
        # 成功释放时约束保持写入本来就是用户要发布的替换结果：证据留在活路径。
        self.assertIn(b"replacement result", staged.read_bytes())
        self.assertFalse(payload.get("manual_action_required"), payload)
        report = retained_objects.collect(self.release.config)
        self.assertTrue(report["ok"], report)
        self.assertEqual([], report["objects"], report)
        self.assertEqual({}, _retained_in(staged.parent))
        self.assertTrue(
            all(not retained_objects.is_retained_name(item.name) for item in staged.parent.iterdir()),
            sorted(item.name for item in staged.parent.iterdir()),
        )

    def test_a_repeated_recovery_never_destroys_the_first_retained_copy(self):
        task, staged = self._held_task_with_evidence()
        original = staged.read_bytes()

        def failing_release(*args, **kwargs):
            raise ValueError("injected refusal after staging")

        with patch.object(EventStore, "release_constraint_hold", failing_release):
            first_code, first = _run_cli(
                ["--config", str(self.release.config.path), "constraint-hold-release", task]
            )
        self.assertEqual(1, first_code, first)
        kept = _retained_in(staged.parent)
        self.assertEqual(1, len(kept), kept)
        claim = next(iter(kept.values()))["path"]
        self.assertIn(b"replacement result", claim.read_bytes())
        before_inode = claim.stat().st_ino
        before_mtime = claim.stat().st_mtime_ns
        self.assertEqual(original, staged.read_bytes(), "第一次恢复必须还原活路径")

        # 第二次恢复：证据被重新写下（内容不同），恢复再次失败，且保留名已被占。
        self.release.store.hold_constraint_task(task, "second replacement", {"status": "unresolved_partial"})
        with patch.object(EventStore, "release_constraint_hold", failing_release):
            second_code, second = _run_cli(
                ["--config", str(self.release.config.path), "constraint-hold-release", task]
            )

        self.assertEqual(1, second_code, second)
        self.assertEqual("reconciliation_required", second["status"], second)
        self.assertTrue(second.get("manual_action_required"), second)
        self.assertIn(
            str(claim),
            json.dumps(second, ensure_ascii=False),
            "第二次恢复必须报出被占用名字的真实位置",
        )
        self.assertEqual(before_inode, claim.stat().st_ino, "已有保留物不得被换掉")
        self.assertEqual(before_mtime, claim.stat().st_mtime_ns, "已有保留物不得被改写")
        self.assertIn(b"replacement result", claim.read_bytes(), "第一次保留下来的内容必须还在")
        self.assertIn(b"second replacement", staged.read_bytes(), "无法还原时活路径保留本次写入")
        kept = _retained_in(staged.parent)
        self.assertEqual(1, len(kept), "重复恢复不得制造或销毁保留物")
        report = retained_objects.collect(self.release.config)
        self.assertEqual(0, report["anomaly_count"], report)
        self.assertTrue(report["enumeration_ok"], report)


class RetainedListCliTest(_GateFixture):
    """自检入口：memory-retained-list 必须在有保留物时说清楚，并尽量写出报告。"""

    def test_a_clean_tree_reports_ok_and_exits_zero(self):
        code, payload = self.gate._cli(["memory-retained-list"])

        self.assertEqual(0, code, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual([], payload["objects"], payload)
        self.assertEqual(0, payload["retained_count"], payload)
        self.assertTrue(payload["enumeration_ok"], payload)

    def test_the_self_check_command_lists_objects_and_writes_the_report(self):
        self._failing_publication()
        expected = self._expected_retained()

        code, payload = self.gate._cli(["memory-retained-list", "--write-report"])

        self.assertEqual(1, code, payload)
        self.assertFalse(payload["ok"], payload)
        self.assertTrue(payload["manual_action_required"], payload)
        self.assertEqual(
            sorted(expected),
            sorted(
                item["retained_path"]
                for item in payload["objects"]
                if item["state"] != "record_without_object"
            ),
        )
        self.assertEqual(str(retained_objects.journal_path(self.gate.config)), payload["journal_path"])
        report_file = Path(payload["report_written"])
        self.assertTrue(report_file.is_file(), payload)
        written = json.loads(report_file.read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(expected),
            sorted(
                item["retained_path"]
                for item in written["objects"]
                if item["state"] != "record_without_object"
            ),
        )
        for item in payload["objects"]:
            self.assertTrue(item["retained_path"].startswith("/"), item)
            self.assertEqual("pending", item["manual_verification"], item)
            self.assertNotEqual("unknown", item["operation"], item)

    def test_a_report_that_cannot_be_written_still_lists_every_object(self):
        self._failing_publication()
        expected = self._expected_retained()
        directory = retained_objects.journal_path(self.gate.config).parent
        directory.chmod(0o500)
        try:
            code, payload = self.gate._cli(["memory-retained-list", "--write-report"])
        finally:
            directory.chmod(0o700)

        self.assertEqual(1, code, payload)
        self.assertIsNone(payload["report_written"], payload)
        self.assertIn(str(retained_objects.report_path(self.gate.config)), payload["report_error"])
        self.assertEqual(
            sorted(expected),
            sorted(
                item["retained_path"]
                for item in payload["objects"]
                if item["state"] != "record_without_object"
            ),
            "报告写不出来也必须把每个保留物的真实位置列出来",
        )


class DoctorRetainedCheckTest(_GateFixture):
    """自检必须接进整体健康：有保留物时可见、不隐瞒，但不阻断接单（v1.5.5 用户裁定）。

    旧断言"有保留物 → checks['ok'] False / ready_for_external_tasks False"来自
    v1.5.4 的门禁设计。v1.5.5 返修卡 B 改为：待核对保留物是给用户的提醒，不是
    系统故障，不得让它把整个部署染红、不得阻断接单/切换/发布。改动的理由与
    边界写在回执 §11：模块自身的枚举错误仍由 retained_objects 与
    retained_manual_review 两个位置如实上报。
    """

    def setUp(self):
        super().setUp()
        # doctor 还要看这几条路径；夹具配置里只保留自检需要的最小内容。
        gate = self.gate
        gate.config.raw["memory"].update(
            {"knowlp_python": "/usr/bin/python3", "knowlp_module": "/usr/bin/python3"}
        )
        gate.config.raw["integrations"].setdefault("node_command", "/usr/bin/python3")
        # 技能与价值文件索引：给空索引即可通过 verify，不需要任何条目。
        skills_index = gate.root / "skills-index.json"
        skills_index.write_text(
            json.dumps({"schema_version": 1, "entries": []}, ensure_ascii=False), encoding="utf-8"
        )
        knowledge_index = gate.root / "knowledge-index.json"
        knowledge_index.write_text(
            json.dumps({"schema_version": 1, "entries": []}, ensure_ascii=False), encoding="utf-8"
        )
        gate.config.raw["skills"] = {
            "root_dir": str(gate.root / "skills"),
            "index_path": str(skills_index),
        }
        gate.config.raw["knowledge"] = {
            "index_path": str(knowledge_index),
            "allowed_roots": [str(gate.vault)],
        }
        gate.config.path.write_text(
            json.dumps(gate.config.raw, ensure_ascii=False), encoding="utf-8"
        )

    def test_doctor_reports_the_retained_objects_check(self):
        clean = doctor(self.gate.config, self.gate.store)
        self.assertTrue(clean["retained_objects"]["ok"], clean["retained_objects"])
        self.assertEqual(0, clean["retained_objects"]["retained_count"], clean["retained_objects"])
        self.assertFalse(clean["retained_objects"]["manual_action_required"], clean)
        self.assertFalse(clean["retained_manual_review"]["required"], clean["retained_manual_review"])
        self.assertEqual(0, clean["retained_manual_review"]["pending_count"], clean)

        self._failing_publication()

        checks = doctor(self.gate.config, self.gate.store)
        retained = checks["retained_objects"]
        self.assertFalse(retained["ok"], retained)
        self.assertTrue(retained["manual_action_required"], retained)
        self.assertGreater(retained["retained_count"], 0, retained)
        # 提醒必须清楚可见：数量 + 列表命令 + 逐个处理命令。
        review = checks["retained_manual_review"]
        self.assertTrue(review["required"], review)
        self.assertEqual(retained["retained_count"], review["pending_count"], review)
        self.assertTrue(review["enumeration_ok"], review)
        self.assertEqual("memory-retained-list", review["list_command"], review)
        self.assertIn("--trash", review["resolve_command"], review)
        # v1.5.5 用户裁定：待核对不阻断接单/切换/发布。
        self.assertTrue(checks["ok"], "待人工核对的保留物不得把整体健康染红")
        self.assertTrue(checks["ready_for_external_tasks"], "待核对不得阻断接单")

    def test_doctor_survives_a_broken_self_check(self):
        with patch(
            "xiaoba_agentos.health.retained_check",
            side_effect=RuntimeError("the retention journal is corrupted"),
        ):
            checks = doctor(self.gate.config, self.gate.store)

        retained = checks["retained_objects"]
        self.assertFalse(retained["ok"], retained)
        self.assertTrue(retained["manual_action_required"], retained)
        self.assertTrue(
            any("could not run" in problem for problem in retained["problems"]), retained
        )
        # 模块自身的故障不能被伪装成"普通待办"：enumeration_ok 必须是 False，
        # 提醒里也必须如实带出来。
        self.assertFalse(retained["enumeration_ok"], retained)
        self.assertFalse(checks["retained_manual_review"]["enumeration_ok"], checks)
        self.assertTrue(checks["retained_manual_review"]["required"], checks)
        # 但按用户裁定，保留物模块自身的错误也不扩成全系统接单/切换禁令：
        # 真实门禁（活文件/索引/配置/事件链）保持原样，不被这一项牵连。
        self.assertTrue(checks["ok"], "保留物模块的错误不得扩成全系统禁令")
        self.assertTrue(checks["ready_for_external_tasks"], checks)

    def test_a_real_fault_still_turns_the_whole_deployment_red(self):
        """健康控制组：把保留物摘出门禁之后，真实故障必须照旧被挡住。"""
        with patch.object(
            EventStore,
            "verify_event_chain",
            side_effect=RuntimeError("the event chain is broken"),
        ):
            checks = doctor(self.gate.config, self.gate.store)

        self.assertFalse(checks["event_chain"]["ok"], checks["event_chain"])
        self.assertFalse(checks["ok"], "真实的故障必须照旧把整体健康挡住")
        self.assertFalse(checks["ready_for_external_tasks"], checks)

    def test_a_pending_review_never_blocks_the_switch_command(self):
        """切换只读 doctor['ok']（candidate/scripts/switch_release.py），它必须还是 True。"""
        self._failing_publication()

        code, payload = self.gate._cli(["doctor"])

        self.assertEqual(0, code, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertTrue(payload["retained_manual_review"]["required"], payload)
        self.assertGreater(payload["retained_manual_review"]["pending_count"], 0, payload)

    def test_the_successful_session_start_still_carries_the_reminder(self):
        """"doctor 整体 ok" 与 "有文件待人工核对" 必须同时出现在成功正文里。"""
        clean_code, clean = self.gate._cli(["session-start"])
        self.assertEqual(0, clean_code, clean)
        self.assertTrue(clean["doctor_ok"], clean)
        self.assertFalse(clean["retained_manual_review"]["required"], clean)

        self._failing_publication()

        code, payload = self.gate._cli(["session-start"])

        self.assertEqual(0, code, payload)
        self.assertEqual("ready", payload["status"], payload)
        self.assertTrue(payload["doctor_ok"], payload)
        self.assertTrue(payload["ready_for_external_tasks"], payload)
        # 真实的活体探针照旧跑了（不是跳过探针换来的成功）。
        self.assertIn("session_mode", payload["live_probe"], payload)
        review = payload["retained_manual_review"]
        self.assertTrue(review["required"], payload)
        self.assertGreater(review["pending_count"], 0, review)
        self.assertEqual("memory-retained-list", review["list_command"], review)


class RetainedResolveCliTest(_GateFixture):
    """A（v1.5.5 用户新增要求）：一次一个保留物的用户终端命令。

    两个互斥动作——移入系统废纸篓、确认用户已手动删除。没有批量、没有过期、
    没有自动清理；命令只碰命令行里点名的那一个对象。任何失败都必须如实返回
    状态、真实位置与重试条件，绝不假报成功，也不接收任意路径就删除。
    """

    def setUp(self):
        super().setUp()
        # 隔离夹具：真实系统废纸篓只用专门生成的测试文件核验。这里把废纸篓指向
        # 配置里指定的同设备临时目录，绝不移动用户的真实保留物，也不清空废纸篓。
        self.trash = self.gate.root / "fixture-trash"
        self.trash.mkdir(mode=0o700)
        self.gate.config.raw["memory"]["retained_trash_dir"] = str(self.trash)
        self.gate.config.path.write_text(
            json.dumps(self.gate.config.raw, ensure_ascii=False), encoding="utf-8"
        )

    def _resolve(self, path, *flags):
        return self.gate._cli(["memory-retained-resolve", str(path), *flags])

    def _one_pending(self):
        payload = self._self_check()
        pending = [
            item
            for item in payload["objects"]
            if item["state"] in retained_objects.RETAINED_OBJECT_STATES
        ]
        self.assertTrue(pending, payload)
        return payload, pending

    def _listed(self):
        payload = self._self_check()
        return payload, {item["retained_path"]: item for item in payload["objects"]}

    def _built_index(self, extra_args=()):
        staging = tempfile.TemporaryDirectory()
        self.addCleanup(staging.cleanup)
        graph = Path(staging.name)
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(IMPLEMENTATION_ROOT / "scripts/build_knowlp_index.py"),
                "--vault",
                str(self.gate.vault),
                "--graph-dir",
                str(graph),
                *extra_args,
            ],
            capture_output=True,
            text=True,
            cwd=str(IMPLEMENTATION_ROOT),
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads((graph / "meta_index.json").read_text(encoding="utf-8"))

    def test_the_user_moves_exactly_one_named_object_into_the_trash(self):
        self._failing_publication()
        before, pending = self._one_pending()
        chosen = pending[0]
        path = Path(chosen["retained_path"])
        bytes_before = path.read_bytes()
        inode_before = path.stat().st_ino

        code, outcome = self._resolve(path, "--trash")

        self.assertEqual(0, code, outcome)
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual("moved_to_trash", outcome["status"], outcome)
        self.assertFalse(path.exists(), "原位置必须真的不再有对象")
        trash_path = Path(outcome["trash_path"])
        self.assertTrue(trash_path.is_file(), outcome)
        self.assertTrue(str(trash_path).startswith(str(self.trash)), outcome)
        self.assertEqual(bytes_before, trash_path.read_bytes(), "移入废纸篓必须保留原对象本身")
        self.assertEqual(
            inode_before, trash_path.stat().st_ino, "必须是移动（同一个 inode），不是复制后删除"
        )

        # 动作日志：只有真实成功之后才追加，且带可核对的标识与真实位置。
        resolutions, problems = retained_objects.read_resolutions(self.gate.config)
        self.assertEqual([], problems, problems)
        self.assertEqual(1, len(resolutions), resolutions)
        record = resolutions[0]
        self.assertEqual("moved_to_trash", record["action"], record)
        self.assertEqual(str(path), record["retained_path"], record)
        self.assertEqual(str(trash_path), record["trash_path"], record)
        self.assertTrue(record["confirmed_by_user"], record)
        self.assertTrue(record["object_verified_absent"], record)
        self.assertEqual(inode_before, record["object_identity"]["ino"], record)
        self.assertTrue(record["resolved_at"], record)
        self.assertTrue(
            record["explained_record_digests"], "被解释的登记行必须留下摘要，才能说清处理的是哪一条记录"
        )

        # 新进程（只看磁盘）不再把同一对象算待办；其它保留物一个都没被动。
        after, listed = self._listed()
        self.assertEqual(1, after["resolved_count"], after)
        self.assertNotIn(str(path), listed, after)
        self.assertEqual(before["retained_count"] - 1, after["retained_count"], after)
        self.assertGreater(after["retained_count"], 0, "其它保留物必须照旧列出来")
        for other in pending[1:]:
            self.assertIn(other["retained_path"], listed, after)

        # 清单默认突出待核对；已处理历史必须显式查看。
        code, hidden = self.gate._cli(["memory-retained-list"])
        self.assertEqual(1, code, hidden)
        self.assertNotIn("resolved_objects", hidden, "已处理历史默认不显示")
        self.assertEqual(1, hidden["resolved_objects_hidden"], hidden)
        code, shown = self.gate._cli(["memory-retained-list", "--show-resolved"])
        self.assertEqual(1, code, shown)
        self.assertEqual(1, len(shown["resolved_objects"]), shown)
        self.assertEqual(str(path), shown["resolved_objects"][0]["retained_path"], shown)

    def test_a_repeated_call_reports_already_resolved_and_changes_nothing(self):
        self._failing_publication()
        _, pending = self._one_pending()
        path = Path(pending[0]["retained_path"])
        code, first = self._resolve(path, "--trash")
        self.assertEqual(0, code, first)

        code, second = self._resolve(path, "--trash")

        self.assertEqual(0, code, second)
        self.assertTrue(second["ok"], second)
        self.assertEqual("already_resolved", second["status"], second)
        self.assertEqual(first["trash_path"], second["trash_path"], second)
        resolutions, _ = retained_objects.read_resolutions(self.gate.config)
        self.assertEqual(1, len(resolutions), "重复调用不得再追加一条记录")
        self.assertTrue(Path(first["trash_path"]).is_file(), "重复调用不得动废纸篓里的对象")

    def test_a_symlink_standing_at_the_recorded_path_is_refused(self):
        self._failing_publication()
        _, pending = self._one_pending()
        path = Path(pending[0]["retained_path"])
        victim = self.gate.root / "user-document.txt"
        victim.write_bytes(b"the user's own document\n")
        path.unlink()
        path.symlink_to(victim)

        code, outcome = self._resolve(path, "--trash")

        self.assertEqual(1, code, outcome)
        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual("refused", outcome["status"], outcome)
        self.assertTrue(any("symlink" in issue for issue in outcome["issues"]), outcome)
        self.assertEqual(
            b"the user's own document\n", victim.read_bytes(), "软链指向的对象绝不能被移动或改写"
        )
        self.assertTrue(path.is_symlink(), outcome)
        self.assertFalse(list(self.trash.iterdir()), "被拒绝的调用不得在废纸篓里留下任何东西")
        self.assertFalse(retained_objects.resolutions_path(self.gate.config).exists(), outcome)

    def test_confirm_deleted_refuses_while_the_object_is_still_there(self):
        self._failing_publication()
        _, pending = self._one_pending()
        path = Path(pending[0]["retained_path"])
        bytes_before = path.read_bytes()

        code, outcome = self._resolve(path, "--confirm-deleted")

        self.assertEqual(1, code, outcome)
        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual("object_still_present", outcome["status"], outcome)
        self.assertEqual(bytes_before, path.read_bytes(), "对象仍在时不得删它，也不得记成已删除")
        self.assertFalse(retained_objects.resolutions_path(self.gate.config).exists(), outcome)

    def test_confirm_deleted_records_the_users_own_deletion(self):
        self._failing_publication()
        _, pending = self._one_pending()
        path = Path(pending[0]["retained_path"])
        path.unlink()

        code, outcome = self._resolve(path, "--confirm-deleted")

        self.assertEqual(0, code, outcome)
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual("confirmed_deleted", outcome["status"], outcome)
        self.assertIsNone(outcome["trash_path"], outcome)
        resolutions, problems = retained_objects.read_resolutions(self.gate.config)
        self.assertEqual([], problems, problems)
        self.assertEqual("confirmed_deleted", resolutions[0]["action"], resolutions)
        self.assertTrue(resolutions[0]["object_verified_absent"], resolutions)
        self.assertTrue(resolutions[0]["confirmed_by_user"], resolutions)
        after, listed = self._listed()
        self.assertEqual(1, after["resolved_count"], after)
        self.assertNotIn(str(path), listed, after)
        self.assertFalse(
            any(path.name in json.dumps(item, ensure_ascii=False) for item in after["anomalies"]),
            after["anomalies"],
        )

    def test_an_ordinary_file_is_never_moved_by_this_command(self):
        note = self.gate.vault / "70_复利规则" / "经验" / "ordinary-note.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("# 普通笔记\n", encoding="utf-8")

        code, outcome = self._resolve(note, "--trash")

        self.assertEqual(1, code, outcome)
        self.assertEqual("refused", outcome["status"], outcome)
        self.assertTrue(any("ordinary file" in issue for issue in outcome["issues"]), outcome)
        self.assertTrue(note.is_file(), "普通文件必须原样留在原地")
        self.assertEqual("# 普通笔记\n", note.read_text(encoding="utf-8"), outcome)
        self.assertFalse(list(self.trash.iterdir()), outcome)

    def test_an_object_outside_the_discovery_scope_is_refused(self):
        outside = self.gate.root / "outside-the-scope"
        outside.mkdir()
        object_path = outside / ".note.md.4242.rollback"
        object_path.write_bytes(b"outside the scope\n")

        code, outcome = self._resolve(object_path, "--trash")

        self.assertEqual(1, code, outcome)
        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual("out_of_scope", outcome["status"], outcome)
        self.assertEqual(b"outside the scope\n", object_path.read_bytes(), outcome)
        self.assertFalse(list(self.trash.iterdir()), outcome)

    def test_a_move_that_cannot_finish_reports_the_real_location_and_records_nothing(self):
        self._failing_publication()
        _, pending = self._one_pending()
        path = Path(pending[0]["retained_path"])
        bytes_before = path.read_bytes()
        self.trash.chmod(0o500)
        try:
            code, outcome = self._resolve(path, "--trash")
        finally:
            self.trash.chmod(0o700)

        self.assertEqual(1, code, outcome)
        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual("move_failed", outcome["status"], outcome)
        self.assertTrue(any(str(path) in issue for issue in outcome["issues"]), outcome)
        self.assertTrue(path.is_file(), "移动失败时对象必须还在原保留位置")
        self.assertEqual(bytes_before, path.read_bytes(), outcome)
        self.assertTrue(outcome["retry"], outcome)
        self.assertFalse(
            retained_objects.resolutions_path(self.gate.config).exists(),
            "移动没成功就绝不能记成已处理",
        )
        self.assertFalse(list(self.trash.iterdir()), outcome)

    def test_a_move_that_succeeds_but_cannot_be_recorded_reports_the_real_trash_path(self):
        self._failing_publication()
        _, pending = self._one_pending()
        path = Path(pending[0]["retained_path"])
        bytes_before = path.read_bytes()

        with patch.object(
            retained_objects, "record_resolution", return_value="injected metadata failure"
        ):
            code, outcome = self._resolve(path, "--trash")

        self.assertEqual(1, code, outcome)
        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual("moved_but_not_recorded", outcome["status"], outcome)
        self.assertTrue(
            any("injected metadata failure" in issue for issue in outcome["issues"]), outcome
        )
        self.assertFalse(path.exists(), outcome)
        trash_path = Path(outcome["trash_path"])
        self.assertTrue(trash_path.is_file(), "真实位置必须如实返回，不能因为记录失败就谎报")
        self.assertEqual(bytes_before, trash_path.read_bytes(), outcome)
        self.assertTrue(outcome["retry"], outcome)

    def test_a_corrupted_record_is_reported_instead_of_acting_on_a_guess(self):
        self._failing_publication()
        _, pending = self._one_pending()
        path = Path(pending[0]["retained_path"])
        retained_objects.journal_path(self.gate.config).write_text("{not json\n", encoding="utf-8")

        code, outcome = self._resolve(path, "--trash")

        self.assertEqual(1, code, outcome)
        self.assertEqual("records_unreadable", outcome["status"], outcome)
        self.assertTrue(path.is_file(), "记录读不出来时不得移动任何东西")
        self.assertTrue(outcome["issues"], outcome)
        self.assertTrue(outcome["retry"], outcome)

    def test_a_replacement_at_an_already_handled_path_is_refused_and_stays_visible(self):
        self._failing_publication()
        _, pending = self._one_pending()
        path = Path(pending[0]["retained_path"])
        code, first = self._resolve(path, "--trash")
        self.assertEqual(0, code, first)

        path.write_bytes(b"a different object at the same path\n")

        # 同路径后来出现的新对象不得被旧记录压掉：它必须以"没有登记记录"的待办出现。
        payload, listed = self._listed()
        self.assertIn(str(path), listed, payload)
        entry = listed[str(path)]
        self.assertTrue(
            any("no discovery record" in problem for problem in entry["problems"]), entry
        )
        self.assertTrue(
            any(item["kind"] == "record_missing" for item in payload["anomalies"]),
            payload["anomalies"],
        )

        code, outcome = self._resolve(path, "--trash")

        self.assertEqual(1, code, outcome)
        self.assertEqual("object_replaced", outcome["status"], outcome)
        self.assertEqual(
            b"a different object at the same path\n", path.read_bytes(), "被替换的对象不得被移动"
        )

        # 文档化的出口：用户自己删掉它，再如实登记（不是自动清理）。
        path.unlink()
        code, confirmed = self._resolve(path, "--confirm-deleted")
        self.assertEqual(0, code, confirmed)
        self.assertEqual("confirmed_deleted", confirmed["status"], confirmed)
        after, listed_after = self._listed()
        self.assertNotIn(str(path), listed_after, after)
        resolutions, _ = retained_objects.read_resolutions(self.gate.config)
        self.assertEqual(2, len(resolutions), resolutions)

    def test_a_handled_object_never_enters_the_index_and_is_not_adopted(self):
        self._failing_publication()
        _, pending = self._one_pending()
        path = Path(pending[0]["retained_path"])
        fingerprint_before = self.gate.bridge.index_sha256()

        code, outcome = self._resolve(path, "--trash")
        self.assertEqual(0, code, outcome)

        self.assertEqual(
            fingerprint_before,
            self.gate.bridge.index_sha256(),
            "保留物本来就不在索引里，处理它不得改变索引指纹",
        )
        paths = [
            item["path"]
            for item in self._built_index(
                ["--retained-ledger", str(retained_objects.journal_path(self.gate.config))]
            )
        ]
        self.assertFalse(any(path.name in item for item in paths), paths)
        self.assertFalse(
            any(retained_objects.RESOLUTIONS_FILENAME in item for item in paths),
            "动作日志不得成为可检索来源",
        )
        self.assertIsNone(
            self.gate.bridge._verified_source(
                {"path": str(retained_objects.resolutions_path(self.gate.config))}
            ),
            "已处理日志不得被当成经验采纳",
        )


class RetainedReportWriteTest(_GateFixture):
    """C（必须修）：报告写入的专属位置与对象身份校验。

    旧实现直接往 `.{报告名}.{pid}.tmp` 写（会截断/跟随这个位置上别人的对象），
    然后 os.replace 到报告路径。这里用正反控制组证明：预占的普通文件、软链、
    以及用户故意设成只读的目录都不会被这个调用破坏。
    """

    def _payload(self):
        return retained_objects.collect(self.gate.config)

    def test_a_preoccupied_temporary_name_is_never_overwritten(self):
        self._failing_publication()
        payload = self._payload()
        target = retained_objects.report_path(self.gate.config)
        user_bytes = b"the user's own file, sitting at a name this call would like to use\n"
        occupied = [
            target.parent / f".{target.name}.{os.getpid()}.tmp",
            target.parent / f".{target.name}.{os.getpid()}.0.tmp",
        ]
        for path in occupied:
            path.write_bytes(user_bytes)

        written, error = retained_objects.write_report(self.gate.config, payload)

        self.assertIsNone(error, error)
        self.assertEqual(str(target), written, written)
        for path in occupied:
            self.assertEqual(user_bytes, path.read_bytes(), f"{path} 上的对象必须原样保留")
        report = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["objects"]), len(report["objects"]), report)
        self.assertFalse(target.is_symlink(), report)
        self.assertEqual(0o600, stat.S_IMODE(target.stat().st_mode), report)

    def test_a_symlinked_temporary_name_never_redirects_the_write(self):
        self._failing_publication()
        payload = self._payload()
        target = retained_objects.report_path(self.gate.config)
        victim = self.gate.root / "victim-document.json"
        victim.write_bytes(b"do not touch\n")
        user_link = target.parent / f".{target.name}.{os.getpid()}.0.tmp"
        user_link.symlink_to(victim)

        written, error = retained_objects.write_report(self.gate.config, payload)

        self.assertIsNone(error, error)
        self.assertEqual(str(target), written, written)
        self.assertEqual(b"do not touch\n", victim.read_bytes(), "软链指向的对象绝不能被写穿")
        self.assertTrue(user_link.is_symlink(), "调用不得删除或替换别人的软链")
        self.assertTrue(target.is_file(), target)
        self.assertFalse(target.is_symlink(), "最终报告必须是普通文件")
        report = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["objects"]), len(report["objects"]), report)

    def test_the_report_path_is_refused_when_it_is_not_a_regular_file(self):
        self._failing_publication()
        payload = self._payload()
        target = retained_objects.report_path(self.gate.config)
        victim = self.gate.root / "victim-document.json"
        victim.write_bytes(b"do not touch\n")
        target.symlink_to(victim)

        written, error = retained_objects.write_report(self.gate.config, payload)

        self.assertIsNone(written, written)
        self.assertIn(str(target), error)
        self.assertEqual(b"do not touch\n", victim.read_bytes(), "报告路径是软链时绝不能写穿")
        self.assertTrue(target.is_symlink(), target)

    def test_a_read_only_metadata_directory_is_never_reopened(self):
        self._failing_publication()
        payload = self._payload()
        target = retained_objects.report_path(self.gate.config)
        directory = target.parent
        directory.chmod(0o500)
        try:
            written, error = retained_objects.write_report(self.gate.config, payload)
            mode = stat.S_IMODE(directory.stat().st_mode)
        finally:
            directory.chmod(0o700)

        self.assertIsNone(written, written)
        self.assertIn(str(target), error)
        self.assertEqual(0o500, mode, "用户把目录设成只读时，这个调用不得擅自放开权限")
        self.assertEqual(
            len(payload["objects"]),
            len(retained_objects.collect(self.gate.config)["objects"]),
            "报告写不出来也必须照旧列全每个保留物",
        )
        self.assertFalse(list(directory.glob(".*.tmp")), "失败的调用不得留下自己的临时文件")

    def test_a_previous_report_is_replaced_by_the_new_one(self):
        first, error = retained_objects.write_report(self.gate.config, self._payload())
        self.assertIsNone(error, error)
        target = Path(first)
        first_bytes = target.read_bytes()

        self._failing_publication()
        payload = self._payload()
        written, error = retained_objects.write_report(self.gate.config, payload)

        self.assertIsNone(error, error)
        self.assertEqual(str(target), written, written)
        self.assertNotEqual(first_bytes, target.read_bytes(), "新报告必须真的写进去")
        self.assertEqual(
            len(payload["objects"]), len(json.loads(target.read_text(encoding="utf-8"))["objects"])
        )
        self.assertEqual(0o600, stat.S_IMODE(target.stat().st_mode), target)


class JournalOnDiskVerificationTest(_GateFixture):
    """D（必须修）：日志登记的对象必须按精确路径 lstat 核对，不能把存在的报成不存在。

    主力探针 journal_named_object 的形状：日志按路径登记了改名后的保留物（名字
    规则已不匹配），旧实现在合并阶段只看扫描结果，于是把它报成
    record_without_object / readable=false / retained_count=0。
    """

    def _graph_dir(self):
        return Path(self.gate.config.raw["memory"]["knowlp_graph_dir"])

    def _listed(self):
        payload = retained_objects.collect(self.gate.config)
        return payload, {item["retained_path"]: item for item in payload["objects"]}

    def _kinds(self, payload):
        return [item["kind"] for item in payload["anomalies"]]

    def test_a_recorded_object_that_really_exists_is_listed_with_its_real_identity(self):
        directory = self._graph_dir()
        renamed = directory / "renamed_retained_output.json"
        content = b'{"renamed": "the retained output the user renamed by hand"}\n'
        renamed.write_bytes(content)
        error = retained_objects.record_retained(
            self.gate.config,
            retained_path=renamed,
            original_path=directory / "index.json",
            operation="memory.index_output",
            reason="the user renamed this retained object by hand",
            writer_pid=4242,
            written_bytes_sha256=hashlib.sha256(content).hexdigest(),
        )
        self.assertIsNone(error, error)

        payload, listed = self._listed()

        key = str(Path(os.path.realpath(str(renamed))))
        self.assertIn(key, listed, payload)
        entry = listed[key]
        self.assertEqual("record_matched_on_disk", entry["journal"], entry)
        self.assertEqual("record", entry["discovered_by"], entry)
        self.assertEqual("retained_object", entry["state"], entry)
        self.assertTrue(entry["readable"], entry)
        self.assertEqual(len(content), entry["size_bytes"], entry)
        self.assertGreater(entry["ino"], 0, entry)
        self.assertGreater(entry["dev"], 0, entry)
        self.assertFalse(entry["problems"], entry)
        self.assertEqual(1, payload["retained_count"], payload)
        self.assertTrue(payload["manual_action_required"], payload)
        self.assertNotIn("record_without_object", self._kinds(payload), payload["anomalies"])

    def test_a_record_whose_object_is_really_gone_is_still_reported(self):
        directory = self._graph_dir()
        missing = directory / ".gone.md.5150.rollback"
        error = retained_objects.record_retained(
            self.gate.config,
            retained_path=missing,
            original_path=directory / "gone.md",
            operation="memory.note",
            reason="the object was deleted by hand",
            writer_pid=5150,
        )
        self.assertIsNone(error, error)

        payload, listed = self._listed()

        key = str(Path(os.path.realpath(str(missing))))
        self.assertIn(key, listed, "真缺失的记录也必须被列出来，不能静默丢弃")
        entry = listed[key]
        self.assertEqual("record_without_object", entry["state"], entry)
        self.assertFalse(entry["readable"], entry)
        self.assertIn("record_without_object", self._kinds(payload), payload["anomalies"])
        self.assertEqual(0, payload["retained_count"], payload)
        self.assertTrue(payload["manual_action_required"], payload)

    def test_a_record_outside_the_discovery_scope_is_reported_not_followed(self):
        outside = self.gate.root / "outside-the-scope"
        outside.mkdir()
        object_path = outside / ".note.md.6161.rollback"
        object_path.write_bytes(b"outside the discovery scope\n")
        error = retained_objects.record_retained(
            self.gate.config,
            retained_path=object_path,
            original_path=outside / "note.md",
            operation="memory.note",
            reason="this object was moved out of the discovery scope by hand",
            writer_pid=6161,
        )
        self.assertIsNone(error, error)

        payload, listed = self._listed()

        key = str(Path(os.path.realpath(str(object_path))))
        self.assertIn(key, listed, payload)
        entry = listed[key]
        self.assertEqual("record_outside_scope", entry["state"], entry)
        self.assertFalse(entry["readable"], entry)
        self.assertIn("record_outside_scope", self._kinds(payload), payload["anomalies"])
        self.assertEqual(
            b"outside the discovery scope\n", object_path.read_bytes(), "范围外的对象不得被读取或改动"
        )
        self.assertEqual(0, payload["retained_count"], payload)

    def test_a_recorded_symlink_is_unverifiable_and_never_followed(self):
        directory = self._graph_dir()
        recorded = directory / "linked_output.json"
        recorded.write_bytes(b"the retained output\n")
        error = retained_objects.record_retained(
            self.gate.config,
            retained_path=recorded,
            original_path=directory / "output.json",
            operation="memory.index_output",
            reason="the user replaced this retained object with a symlink",
            writer_pid=7171,
        )
        self.assertIsNone(error, error)
        victim = self.gate.root / "victim-note.md"
        victim.write_bytes(b"the user's own note\n")
        recorded.unlink()
        recorded.symlink_to(victim)

        payload, listed = self._listed()

        # 记录点名的就是这条路径本身（不是软链指向的地方）：日志只按精确路径核对。
        key = str(Path(os.path.realpath(str(directory))) / recorded.name)
        self.assertIn(key, listed, payload)
        entry = listed[key]
        self.assertEqual("unverifiable_object", entry["state"], entry)
        self.assertFalse(entry["readable"], entry)
        self.assertIn("record_object_unverifiable", self._kinds(payload), payload["anomalies"])
        self.assertEqual(b"the user's own note\n", victim.read_bytes(), "软链不得被跟随")
        self.assertTrue(recorded.is_symlink(), recorded)
        self.assertEqual(1, payload["retained_count"], payload)
        self.assertNotIn(
            str(Path(os.path.realpath(str(victim)))),
            listed,
            "软链指向的对象绝不能因为一条记录经过它而被列成保留物",
        )


class _RetainedTrashFixture(_GateFixture):
    """隔离夹具：废纸篓指向同设备的临时目录，绝不移动用户的真实保留物。"""

    def setUp(self):
        super().setUp()
        self.trash = self.gate.root / "fixture-trash"
        self.trash.mkdir(mode=0o700)
        self.gate.config.raw["memory"]["retained_trash_dir"] = str(self.trash)
        self.gate.config.path.write_text(
            json.dumps(self.gate.config.raw, ensure_ascii=False), encoding="utf-8"
        )

    # ------------------------------------------------------------------ 辅助
    def _resolve(self, path, *flags):
        return self.gate._cli(["memory-retained-resolve", str(path), *flags])

    def _graph_dir(self):
        return Path(self.gate.config.raw["memory"]["knowlp_graph_dir"])

    def _key(self, path):
        return str(Path(os.path.realpath(str(path))))

    def _registered_object(
        self,
        name=".note.md.4242.rollback",
        content=b"CLAIMED_ORIGINAL\n",
        operation="memory.note",
        reason="the recorded retained object",
    ):
        """登记一个真实保留物：日志记录现在自己带物理身份（dev/ino）。"""
        parsed = retained_objects.parse_retained_name(name)
        directory = self._graph_dir()
        path = directory / name
        path.write_bytes(content)
        error = retained_objects.record_retained(
            self.gate.config,
            retained_path=path,
            original_path=directory / parsed["original"],
            operation=operation,
            reason=reason,
            writer_pid=parsed["pid"],
            written_bytes_sha256=hashlib.sha256(content).hexdigest(),
        )
        self.assertIsNone(error, error)
        return Path(os.path.realpath(str(path)))

    def _listed(self):
        payload = self._self_check()
        return payload, {item["retained_path"]: item for item in payload["objects"]}

    def _resolutions(self):
        rows, problems = retained_objects.read_resolutions(self.gate.config)
        self.assertFalse(problems, problems)
        return rows

    def _resolutions_exist(self):
        return retained_objects.resolutions_path(self.gate.config).exists()

    def _trash_entries(self):
        return sorted(self.trash.iterdir())

    def _staging_leftovers(self):
        return sorted(path.name for path in self._graph_dir().glob("*.moving"))

    def _announce(self, path, identity, note="announced before the move"):
        """写下一条移动公告：崩溃窗口里留下的就是这一行。"""
        parsed = retained_objects.parse_retained_name(path.name) or {}
        return retained_objects.record_move_intent(
            self.gate.config,
            {
                "schema": retained_objects.INTENT_SCHEMA,
                "kind": "move_intent",
                "recorded_at": "2026-01-01T00:00:00+0800",
                "retained_path": str(path),
                "original_path": str(path.parent / (parsed.get("original") or path.name)),
                "suffix": parsed.get("suffix") or "unknown",
                "writer_pid": parsed.get("pid") or "unknown",
                "task_id": "unknown",
                "operation": "memory.note",
                "reason": "the recorded retained object",
                "object_identity": identity,
                "identity_source": "the discovery record that names this path",
                "trash_dir": str(self.trash),
                "note": note,
            },
        )

    def _identity(self, info):
        return {
            "dev": int(info.st_dev),
            "ino": int(info.st_ino),
            "size_bytes": int(info.st_size),
            "mtime_ns": int(info.st_mtime_ns),
            "mode": int(info.st_mode),
        }


class RetainedIdentityBindingTest(_RetainedTrashFixture):
    """缺口 1（必须修）：清单里核对过的对象必须一路绑定到执行时。

    记录把物理身份（dev/ino）带在身上，它就是权威；命令行给的显式身份只能
    在记录没带身份时补位，绝不能顶掉记录。因此"查看清单之后把原文件移走、
    同位置放另一个对象"这种替换，无论新对象是谁，都不得被当成已核对对象移动。
    """

    def test_an_object_replaced_before_the_first_call_is_never_moved_as_verified(self):
        path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        original_ino = path.stat().st_ino
        saved = self.gate.root / "the-original-object"
        os.replace(path, saved)  # 用户在清单核对之后把原对象挪走
        other = b"OTHER_WRITER_OBJECT\n"
        path.write_bytes(other)
        other_ino = path.stat().st_ino
        self.assertNotEqual(original_ino, other_ino)

        code, outcome = self._resolve(path, "--trash")

        self.assertEqual(1, code, outcome)
        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual("object_replaced", outcome["status"], outcome)
        self.assertEqual(other, path.read_bytes(), "同位置的替身对象必须原地不动")
        self.assertEqual(other_ino, path.stat().st_ino, outcome)
        self.assertEqual([], self._trash_entries(), "绝不允许把替身移进废纸篓")
        self.assertFalse(self._resolutions_exist(), "没动对对象就绝不能记成已处理")
        self.assertEqual(b"CLAIMED_ORIGINAL\n", saved.read_bytes(), "被移走的原对象不得受影响")

        # 命令行显式身份也不能顶掉记录：记录点名的对象才是"已核对"的那一个。
        info = path.stat()
        code, forced = self._resolve(
            path, "--trash", "--expect-dev", str(info.st_dev), "--expect-ino", str(other_ino)
        )
        self.assertEqual(1, code, forced)
        self.assertEqual("object_replaced", forced["status"], forced)
        self.assertEqual(other, path.read_bytes(), forced)
        self.assertEqual([], self._trash_entries(), forced)

    def test_a_registered_path_with_a_public_name_is_bound_to_its_identity_too(self):
        directory = self._graph_dir()
        path = directory / "renamed_retained_output.json"
        path.write_bytes(b"the retained output the user renamed by hand\n")
        error = retained_objects.record_retained(
            self.gate.config,
            retained_path=path,
            original_path=directory / "index.json",
            operation="memory.index_output",
            reason="the user renamed this retained object by hand",
            writer_pid=4242,
        )
        self.assertIsNone(error, error)
        original_ino = path.stat().st_ino
        saved = self.gate.root / "renamed-away.json"
        os.replace(path, saved)
        other = b"a different object that now carries that public name\n"
        path.write_bytes(other)

        code, outcome = self._resolve(path, "--trash")

        self.assertEqual(1, code, outcome)
        self.assertEqual("object_replaced", outcome["status"], outcome)
        self.assertEqual(other, path.read_bytes(), outcome)
        self.assertEqual([], self._trash_entries(), outcome)
        self.assertFalse(self._resolutions_exist(), outcome)
        self.assertEqual(original_ino, saved.stat().st_ino, outcome)

    def test_a_record_without_identity_is_never_called_verified(self):
        with patch.object(
            retained_objects,
            "_observed_identity",
            return_value=(None, "unavailable", "this record carries no object identity"),
        ):
            path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        info = path.stat()

        code, outcome = self._resolve(path, "--trash")

        self.assertEqual(1, code, outcome)
        self.assertEqual("identity_unverified", outcome["status"], outcome)
        self.assertTrue(path.is_file(), "缺身份依据时不得移动任何东西")
        self.assertEqual([], self._trash_entries(), outcome)
        self.assertFalse(self._resolutions_exist(), outcome)
        self.assertIn("--expect-dev", outcome["retry"], "必须给出安全明确的重新核对办法")

        # 清单如实给出 dev/ino，用户核对后把它带到执行时：同一个对象才被移动。
        payload, listed = self._listed()
        entry = listed[self._key(path)]
        self.assertFalse(entry["identity_verified"], entry)
        self.assertEqual(info.st_ino, entry["ino"], entry)
        self.assertEqual(info.st_dev, entry["dev"], entry)

        code, moved = self._resolve(
            path, "--trash", "--expect-dev", str(info.st_dev), "--expect-ino", str(info.st_ino)
        )
        self.assertEqual(0, code, moved)
        self.assertEqual("moved_to_trash", moved["status"], moved)
        self.assertEqual(info.st_ino, Path(moved["trash_path"]).stat().st_ino, moved)
        self.assertFalse(path.exists(), moved)

    def test_half_an_explicit_identity_is_refused(self):
        path = self._registered_object()
        info = path.stat()

        code, outcome = self._resolve(path, "--trash", "--expect-ino", str(info.st_ino))

        self.assertEqual(1, code, outcome)
        self.assertFalse(outcome["ok"], outcome)
        self.assertTrue(any("both" in issue for issue in outcome["issues"]), outcome)
        self.assertTrue(path.is_file(), outcome)
        self.assertEqual([], self._trash_entries(), outcome)
        self.assertFalse(self._resolutions_exist(), outcome)

    def test_an_open_writer_holding_the_same_object_is_not_a_replacement(self):
        """丙旧 fd 控制组：对象还在被认领前的写者追加时，身份没变，移动必须照常成功。"""
        path = self._registered_object(content=b"first write\n")
        inode = path.stat().st_ino

        with open(path, "ab") as handle:
            handle.write(b"more bytes from the writer that still holds the old fd\n")
            handle.flush()
            os.fsync(handle.fileno())
            code, outcome = self._resolve(path, "--trash")

        self.assertEqual(0, code, outcome)
        self.assertEqual("moved_to_trash", outcome["status"], outcome)
        trash_path = Path(outcome["trash_path"])
        self.assertEqual(inode, trash_path.stat().st_ino, "必须是同一个 inode")
        self.assertIn(b"more bytes from the writer that still holds the old fd", trash_path.read_bytes())
        self.assertFalse(path.exists(), outcome)


class RetainedMoveGapTest(_RetainedTrashFixture):
    """缺口 1/2（必须修）：整条移动链只有一次排他目录项移动，且一步都不删东西。

    修前这条链先查一个私有中转名字不存在、再 rename 到它（于是覆盖期间占用该名字
    的对象），成功后又按路径 unlink 那个中转名字（于是删掉期间被换到该名字上的
    对象）。新结构把「拿走记录点名的那个名字」直接交给内核的排他目录项移动
    （renamex_np 配 RENAME_EXCL）：名字已被占用时内核以 EEXIST 拒绝而不是覆盖，
    所以既不需要中转名字，也没有任何一步 unlink;移动实际捕获到的若不是被记录的
    对象，就用第二次同样的排他移动把它放回去，放不回去时留在废纸篓按身份记录
    （captured_object），绝不销毁、绝不假报成功。
    """

    def test_the_move_is_one_exclusive_directory_entry_move_and_never_unlinks(self):
        """正：成功移动全程恰好一次排他移动（带 RENAME_EXCL），且不调用 link/rename/unlink。"""
        path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        inode = path.stat().st_ino
        real_exclusive = retained_objects._EXCLUSIVE_MOVE
        self.assertIsNotNone(real_exclusive, "本平台必须提供排他目录项移动原语")
        calls = {"exclusive": [], "flags": [], "link": 0, "rename": 0, "unlink": []}
        real_link, real_rename, real_unlink = os.link, os.rename, os.unlink

        def exclusive(source, target, flags):
            calls["exclusive"].append(
                (self._key(os.fsdecode(source)), self._key(os.fsdecode(target)))
            )
            calls["flags"].append(flags)
            return real_exclusive(source, target, flags)

        def counting_link(source, target, **kwargs):
            calls["link"] += 1
            return real_link(source, target, **kwargs)

        def counting_rename(source, target, **kwargs):
            calls["rename"] += 1
            return real_rename(source, target, **kwargs)

        def counting_unlink(target, **kwargs):
            calls["unlink"].append(self._key(target))
            return real_unlink(target, **kwargs)

        with patch.object(
            retained_objects, "_EXCLUSIVE_MOVE", side_effect=exclusive
        ), patch.object(retained_objects.os, "link", side_effect=counting_link), patch.object(
            retained_objects.os, "rename", side_effect=counting_rename
        ), patch.object(retained_objects.os, "unlink", side_effect=counting_unlink):
            code, outcome = self._resolve(path, "--trash")

        self.assertEqual(0, code, outcome)
        self.assertEqual("moved_to_trash", outcome["status"], outcome)
        self.assertEqual(1, len(calls["exclusive"]), calls)
        self.assertEqual([retained_objects._RENAME_EXCL], calls["flags"], "必须是排他移动")
        self.assertEqual(
            (self._key(path), self._key(Path(outcome["trash_path"]))), calls["exclusive"][0], calls
        )
        self.assertEqual(0, calls["link"], "这条链不再需要 link 中转，也不再预先复制对象")
        self.assertEqual(0, calls["rename"], "绝不用会覆盖目标的普通 rename")
        self.assertEqual([], calls["unlink"], "这条链一步都不按路径 unlink 任何名字")
        self.assertEqual(inode, Path(outcome["trash_path"]).stat().st_ino, outcome)
        self.assertEqual([], self._staging_leftovers(), "没有中转名字可留，成功也不留物")

    def test_an_object_that_appears_just_before_the_move_is_put_back_untouched(self):
        """反：排他移动真的会捕获后来占住记录点名的替身，且替身被原样放回、不销毁。"""
        path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        saved = self.gate.root / "moved-away-object"
        other = b"OTHER_WRITER_OBJECT\n"
        observed = {}
        real_exclusive = retained_objects._EXCLUSIVE_MOVE
        swapped = {"done": False}

        def swap_then_move(source, target, flags):
            source_path = Path(os.fsdecode(source))
            if not swapped["done"] and self._key(source_path) == self._key(path):
                # 另一个写者在排他移动之前换掉了记录点名的名字：替身占住了这个名字
                swapped["done"] = True
                os.replace(source_path, saved)
                source_path.write_bytes(other)
                observed["ino"] = source_path.stat().st_ino
            return real_exclusive(source, target, flags)

        with patch.object(retained_objects, "_EXCLUSIVE_MOVE", side_effect=swap_then_move):
            code, outcome = self._resolve(path, "--trash")

        self.assertEqual(1, code, outcome)
        self.assertEqual("move_conflict", outcome["status"], outcome)
        conflict = outcome["conflict"]
        self.assertEqual("recorded_path_changed_before_move", conflict["kind"], outcome)
        self.assertTrue(conflict["captured_object_restored"], conflict)
        self.assertIsNone(conflict["recorded_object_preserved_at"], conflict)
        self.assertEqual(self._key(path), self._key(conflict["captured_object_path"]), conflict)
        self.assertEqual(other, path.read_bytes(), "误捕获的对象必须原样放回")
        self.assertEqual(observed["ino"], path.stat().st_ino, "放回必须是同一个 inode，不是副本")
        self.assertEqual([], self._trash_entries(), "放回成功后废纸篓里不留物")
        self.assertEqual([], self._staging_leftovers(), "放回成功后不得留下自己的私有名字")
        self.assertFalse(self._resolutions_exist(), "冲突时绝不能记成已处理")
        self.assertTrue(outcome["retry"], outcome)
        self.assertEqual(
            b"CLAIMED_ORIGINAL\n", saved.read_bytes(), "换走它的那一步不是本命令干的，字节必须还在"
        )

    def test_an_object_that_cannot_be_put_back_is_preserved_recorded_and_listed(self):
        """反：替身放不回去时必须留在废纸篓、按身份持久登记、跨重启列出且不进检索。"""
        path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        other = b"OTHER_WRITER_OBJECT\n"
        also_other = b"A_NEW_OBJECT_TOOK_THE_PATH\n"
        saved = self.gate.root / "moved-away-object"
        observed = {}
        real_exclusive = retained_objects._EXCLUSIVE_MOVE

        def swap_then_take(source, target, flags):
            if self._key(os.fsdecode(target)).startswith(self._key(self.trash)):
                # 排他移动之前：记录点名的名字被换走，替身顶住这个名字
                source_path = Path(os.fsdecode(source))
                os.replace(source_path, saved)
                source_path.write_bytes(other)
                observed["ino"] = source_path.stat().st_ino
            elif self._key(os.fsdecode(target)) == self._key(path):
                # 放回时记录点名的名字又被占用了：排他移动必须被内核拒绝，不得覆盖
                Path(os.fsdecode(target)).write_bytes(also_other)
            return real_exclusive(source, target, flags)

        with patch.object(retained_objects, "_EXCLUSIVE_MOVE", side_effect=swap_then_take):
            code, outcome = self._resolve(path, "--trash")

        self.assertEqual(1, code, outcome)
        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual("move_conflict", outcome["status"], outcome)
        conflict = outcome["conflict"]
        self.assertEqual("recorded_path_changed_before_move", conflict["kind"], outcome)
        self.assertFalse(conflict["captured_object_restored"], conflict)
        self.assertIsNone(conflict["recorded_object_preserved_at"], conflict)
        kept = Path(conflict["captured_object_path"])
        self.assertTrue(kept.is_file(), conflict)
        self.assertEqual(other, kept.read_bytes(), "放不回去时也必须完好保留，绝不销毁")
        self.assertEqual(observed["ino"], kept.stat().st_ino, conflict)
        self.assertIn(str(kept), conflict["detail"], conflict)
        self.assertEqual(b"CLAIMED_ORIGINAL\n", saved.read_bytes(), outcome)
        self.assertEqual(also_other, path.read_bytes(), "后来占住记录点名的对象也不得被覆盖")
        self.assertFalse(self._resolutions_exist(), outcome)
        self.assertEqual([], self._staging_leftovers(), "不得留下自己的私有名字")

        # 跨重启：另一个进程只看磁盘重跑自检，必须按身份列出这个误捕获对象和它的真实位置。
        payload = self._self_check_in_new_process(self.gate.config)
        self.assertEqual(1, len(payload["captured_objects"]), payload["captured_objects"])
        captured = payload["captured_objects"][0]
        self.assertEqual(self._key(kept), self._key(captured["captured_path"]), captured)
        self.assertEqual(self._key(path), self._key(captured["recorded_path"]), captured)
        self.assertEqual(observed["ino"], captured["object_identity"]["ino"], captured)
        self.assertEqual("still_at_recorded_path", captured["location_now"], captured)
        self.assertIn("taken again", captured["reason"], captured)
        self.assertIn(str(kept), captured["detail"], captured)
        self.assertIn(
            "captured_object_kept",
            [item["kind"] for item in payload["anomalies"]],
            payload["anomalies"],
        )
        self.assertTrue(payload["manual_action_required"], payload)
        # 登记的是真实保存位置，所以检索必须按记录（而不是按私有名字规则）排掉它。
        self.assertFalse(
            retained_objects.is_retained_name(kept.name), "废纸篓里的名字不带私有名字规则"
        )
        self.assertTrue(retained_objects.is_retained_path(self.gate.config, kept), kept)

    def test_a_captured_object_whose_record_cannot_be_written_is_found_after_a_restart(self):
        """反：连留痕都写不出时，对象也必须留在可被后续运行复核的确定性位置上并被重启列出。

        记录写不出不等于可以把对象丢掉，也不等于可以当无事发生：移动之前写下的公告
        还在，命令据此把误捕获对象放在一个既带公告身份、又带该对象自身 inode 的废纸篓
        名字上。名字本身可复核：后续运行只有在该对象真的带着名字声明的 inode 时才认它。
        本次报告点出它的精确位置和记录写不成的真实原因，重启后的自检必须再找到它、按
        身份和原因列出它，并让检索排除这个位置。
        """
        path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        announced_ino = path.stat().st_ino
        other = b"OTHER_WRITER_OBJECT\n"
        also_other = b"A_NEW_OBJECT_TOOK_THE_PATH\n"
        saved = self.gate.root / "moved-away-object"
        observed = {}
        real_exclusive = retained_objects._EXCLUSIVE_MOVE
        failure = "injected: the journal directory is not writable"

        def swap_then_take(source, target, flags):
            # 只在"记录点名的那个名字被排他移动走"的那一次动手（与主力探针同一注入点）：
            # 记录下注入点是否真的触发，之后为保住对象而做的排他移动不得再被改写。
            if self._key(os.fsdecode(source)) == self._key(path) and not observed.get("swapped"):
                source_path = Path(os.fsdecode(source))
                os.replace(source_path, saved)
                source_path.write_bytes(other)
                observed["swapped"] = True
                observed["ino"] = source_path.stat().st_ino
            elif self._key(os.fsdecode(target)) == self._key(path):
                Path(os.fsdecode(target)).write_bytes(also_other)
            return real_exclusive(source, target, flags)

        with patch.object(retained_objects, "_EXCLUSIVE_MOVE", side_effect=swap_then_take):
            with patch.object(
                retained_objects, "record_captured_object", return_value=failure
            ):
                code, outcome = self._resolve(path, "--trash")

        self.assertEqual(1, code, outcome)
        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual("move_conflict", outcome["status"], outcome)
        conflict = outcome["conflict"]
        self.assertFalse(conflict["captured_object_restored"], conflict)
        self.assertTrue(conflict["captured_object_kept_under_derived_name"], conflict)
        kept = Path(conflict["captured_object_path"])
        self.assertEqual(self._key(self.trash), self._key(kept.parent), conflict)
        self.assertEqual(
            f"{retained_objects.CAPTURED_KEPT_PREFIX}{announced_ino}.{observed['ino']}.note.md",
            kept.name,
            "位置必须由公告身份和捕获对象自身 inode 共同决定，后续运行据此复核对象身份",
        )
        # 名字里声明的 inode 必须就是对象自己的 inode：这是重启侧能复核的持久依据。
        self.assertEqual(
            int(kept.name.split(".")[3]),
            kept.stat().st_ino,
            "名字声明的 inode 与对象实际 inode 必须一致",
        )
        self.assertEqual(other, kept.read_bytes(), "写不出记录也绝不能销毁对象")
        self.assertEqual(observed["ino"], kept.stat().st_ino, conflict)
        self.assertEqual(failure, conflict["record_error"], conflict)
        issues = " ".join(outcome["issues"])
        self.assertIn("could not be recorded", issues, outcome["issues"])
        self.assertIn(str(kept), issues, outcome["issues"])
        self.assertIn("the next run", issues, outcome["issues"])
        self.assertEqual(b"CLAIMED_ORIGINAL\n", saved.read_bytes(), outcome)
        self.assertEqual(also_other, path.read_bytes(), "后来占住记录点名的对象也不得被覆盖")
        self.assertFalse(self._resolutions_exist(), "冲突时绝不能记成已处理")

        # 跨重启：另一个进程只看磁盘，必须复核这个名字、按身份列出它，并排除检索。
        checked = self._capture_check_in_new_process(self.gate.config, [kept])
        payload = checked["payload"]
        self.assertEqual(1, len(payload["captured_objects"]), payload["captured_objects"])
        captured = payload["captured_objects"][0]
        self.assertEqual(self._key(kept), self._key(captured["captured_path"]), captured)
        self.assertEqual(self._key(path), self._key(captured["recorded_path"]), captured)
        self.assertEqual(observed["ino"], captured["object_identity"]["ino"], captured)
        self.assertEqual("in_trash", captured["location_now"], captured)
        self.assertIn("could not write the record", captured["reason"], captured)
        self.assertIn(str(kept), captured["detail"], captured)
        self.assertIn(
            "captured_object_kept",
            [item["kind"] for item in payload["anomalies"]],
            payload["anomalies"],
        )
        self.assertTrue(payload["manual_action_required"], payload)
        self.assertFalse(
            retained_objects.is_retained_name(kept.name), "废纸篓里的名字不带私有名字规则"
        )
        self.assertTrue(checked["excluded"][str(kept)], "没有记录时也必须排除检索")

    def _capture_with_preoccupied_names(self, count, content=b"CLAIMED_ORIGINAL\n"):
        """真实跑一次「留痕写失败」的移动，并先占住前 count 个派生名。

        占位者是真写在废纸篓里的普通文件，身份与被捕获对象不同；占位发生在真正的保命
        移动之前（与主力探针同一注入点、同一时序），移动原语与登记失败都是真实的。
        返回本次移动的路径、字节、观察值和结果。
        """
        path = self._registered_object(content=content)
        announced_ino = path.stat().st_ino
        other = b"OTHER_WRITER_OBJECT\n"
        also_other = b"A_NEW_OBJECT_TOOK_THE_PATH\n"
        saved = self.gate.root / "moved-away-object"
        observed = {"placeholders": []}
        real_exclusive = retained_objects._EXCLUSIVE_MOVE
        failure = "injected: the journal directory is not writable"

        def swap_then_take(source, target, flags):
            if self._key(os.fsdecode(source)) == self._key(path) and not observed.get("swapped"):
                source_path = Path(os.fsdecode(source))
                os.replace(source_path, saved)
                source_path.write_bytes(other)
                observed["swapped"] = True
                observed["ino"] = source_path.stat().st_ino
                # 被捕获对象的 inode 到这里才确定，于是照着实现的命名公式占住它的第一个
                # （以及随后的）候选名：占位者是普通文件，不带名字声明的那个 inode。
                primary = (
                    f"{retained_objects.CAPTURED_KEPT_PREFIX}{announced_ino}."
                    f"{observed['ino']}.note.md"
                )
                for index in range(count):
                    placeholder = self.trash / (primary if index == 0 else f"{primary}.{index}")
                    placeholder.write_bytes(b"PREEXISTING_UNRELATED_OBJECT\n")
                    observed["placeholders"].append(
                        {"path": placeholder, "ino": placeholder.stat().st_ino}
                    )
            elif self._key(os.fsdecode(target)) == self._key(path):
                Path(os.fsdecode(target)).write_bytes(also_other)
            return real_exclusive(source, target, flags)

        with patch.object(retained_objects, "_EXCLUSIVE_MOVE", side_effect=swap_then_take):
            with patch.object(
                retained_objects, "record_captured_object", return_value=failure
            ):
                code, outcome = self._resolve(path, "--trash")
        return {
            "path": path,
            "announced_ino": announced_ino,
            "other": other,
            "also_other": also_other,
            "saved": saved,
            "observed": observed,
            "code": code,
            "outcome": outcome,
            "failure": failure,
        }

    def _craft_kept_name(self, announced_ino, base):
        """造一个自带可复核 inode 的废纸篓名字：名字里的 inode 就是文件自己的 inode。"""
        staged = self.trash / "crafted-object"
        staged.write_bytes(b"CRAFTED_OBJECT\n")
        target = self.trash / (
            f"{retained_objects.CAPTURED_KEPT_PREFIX}{announced_ino}."
            f"{staged.stat().st_ino}.{base}"
        )
        os.rename(staged, target)
        return target

    def test_a_preoccupied_derived_name_does_not_hide_the_object_kept_next_to_it(self):
        """反：第一个派生名被普通文件占住时，被保留的对象仍必须被找到并被排除。

        占位者只是恰好占住这个名字的普通文件，它不携带名字声明的 inode，因此既不能被
        报成误捕获对象，也不能让真正被保留的对象从清单里消失；对象被排他移动到下一个
        候选名，名字依旧可复核，真实新进程必须列出真正的对象、排除它，并放过占位者。
        """
        fixture = self._capture_with_preoccupied_names(1)
        conflict = fixture["outcome"]["conflict"]
        kept = Path(conflict["captured_object_path"])
        placeholder = fixture["observed"]["placeholders"][0]
        primary = placeholder["path"]
        self.assertEqual(f"{primary.name}.1", kept.name, conflict)
        self.assertEqual(fixture["other"], kept.read_bytes(), "写不出记录也绝不能销毁对象")
        self.assertEqual(fixture["observed"]["ino"], kept.stat().st_ino, conflict)

        checked = self._capture_check_in_new_process(self.gate.config, [kept, primary])
        listed = [item["captured_path"] for item in checked["payload"]["captured_objects"]]
        self.assertEqual(1, len(listed), checked["payload"]["captured_objects"])
        self.assertEqual(self._key(kept), self._key(listed[0]), listed)
        self.assertTrue(checked["excluded"][str(kept)], "真正被保留的对象必须被排除检索")
        self.assertFalse(checked["excluded"][str(primary)], "普通占位者不能被当成保留物")
        self.assertEqual(placeholder["ino"], primary.stat().st_ino, "占位者不得被动过")
        self.assertEqual(
            b"PREEXISTING_UNRELATED_OBJECT\n", primary.read_bytes(), "占位者字节不得被改"
        )
        self.assertEqual(fixture["also_other"], fixture["path"].read_bytes(), fixture)
        self.assertFalse(self._resolutions_exist(), "冲突时绝不能记成已处理")

    def test_two_preoccupied_derived_names_do_not_hide_the_object_kept_after_them(self):
        """反：连续两个候选名都被普通文件占住时，对象仍必须在第三个名字上被发现。

        续名不是"写侧往后挪、读侧只认第一个"：两个占位者都必须原样留着、都不被算作
        捕获对象，真正被保留的对象在第三个名字上，必须被真实新进程列出并按身份排除。
        """
        fixture = self._capture_with_preoccupied_names(2)
        conflict = fixture["outcome"]["conflict"]
        kept = Path(conflict["captured_object_path"])
        placeholders = [item["path"] for item in fixture["observed"]["placeholders"]]
        self.assertEqual(2, len(placeholders), fixture["observed"])
        self.assertEqual(f"{placeholders[0].name}.2", kept.name, conflict)
        self.assertEqual(fixture["other"], kept.read_bytes(), "写不出记录也绝不能销毁对象")
        self.assertEqual(fixture["observed"]["ino"], kept.stat().st_ino, conflict)

        checked = self._capture_check_in_new_process(self.gate.config, [kept, *placeholders])
        listed = [item["captured_path"] for item in checked["payload"]["captured_objects"]]
        self.assertEqual(1, len(listed), checked["payload"]["captured_objects"])
        self.assertEqual(self._key(kept), self._key(listed[0]), listed)
        self.assertTrue(checked["excluded"][str(kept)], "真正被保留的对象必须被排除检索")
        for item in fixture["observed"]["placeholders"]:
            self.assertFalse(checked["excluded"][str(item["path"])], "占位者不是保留物")
            self.assertEqual(item["ino"], item["path"].stat().st_ino, "占位者不得被动过")
            self.assertEqual(
                b"PREEXISTING_UNRELATED_OBJECT\n", item["path"].read_bytes(), "字节不得被改"
            )

    def test_leftover_names_are_attributed_by_the_announcement_they_belong_to(self):
        """反：先前冲突的残留与同一公告下的多个候选必须被区分，不得张冠李戴。

        别处移动留下的名字即使自带可复核 inode，也不属于本次公告；同一个公告下的多个
        自校验候选则必须各自按真实身份列出并排除——不能只认第一个，也不能把别人的
        名字算到本次移动头上。
        """
        fixture = self._capture_with_preoccupied_names(0)
        kept = Path(fixture["outcome"]["conflict"]["captured_object_path"])
        announced_ino = fixture["announced_ino"]
        foreign = self._craft_kept_name(announced_ino + 7, "leftover.md")
        sibling = self._craft_kept_name(announced_ino, "note.md")

        checked = self._capture_check_in_new_process(self.gate.config, [kept, foreign, sibling])
        listed = {
            self._key(item["captured_path"]): item
            for item in checked["payload"]["captured_objects"]
        }
        self.assertEqual(
            {self._key(kept), self._key(sibling)},
            set(listed),
            checked["payload"]["captured_objects"],
        )
        self.assertEqual(kept.stat().st_ino, listed[self._key(kept)]["object_identity"]["ino"])
        self.assertEqual(sibling.stat().st_ino, listed[self._key(sibling)]["object_identity"]["ino"])
        self.assertNotIn(self._key(foreign), listed, "别的公告留下的名字不得算到本次移动头上")
        self.assertTrue(checked["excluded"][str(kept)], "本次移动留下的对象必须被排除检索")
        self.assertTrue(checked["excluded"][str(sibling)], "同公告的候选也按位置排除检索")
        self.assertFalse(checked["excluded"][str(foreign)], "不属于本次公告的名字不得被排除")
        for path in (kept, foreign, sibling):
            self.assertTrue(path.exists(), path)

    def test_a_move_that_fails_retires_its_announcement_and_changes_nothing(self):
        path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        bytes_before = path.read_bytes()
        self.trash.chmod(0o500)
        try:
            code, outcome = self._resolve(path, "--trash")
        finally:
            self.trash.chmod(0o700)

        self.assertEqual(1, code, outcome)
        self.assertEqual("move_failed", outcome["status"], outcome)
        self.assertEqual(bytes_before, path.read_bytes(), "对象必须还在原保留位置")
        self.assertEqual([], self._trash_entries(), outcome)
        self.assertFalse(self._resolutions_exist(), outcome)

        payload = self._self_check()
        self.assertEqual(0, payload["pending_move_intent_count"], payload["move_intents"])
        self.assertNotIn(
            "move_intent_without_resolution",
            [item["kind"] for item in payload["anomalies"]],
            payload["anomalies"],
        )

    def test_a_platform_without_an_exclusive_move_refuses_instead_of_overwriting(self):
        """反：没有排他移动原语时绝不退回普通 rename，宁可什么都不动。"""
        path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        inode = path.stat().st_ino

        with patch.object(retained_objects, "_EXCLUSIVE_MOVE", None):
            code, outcome = self._resolve(path, "--trash")

        self.assertEqual(1, code, outcome)
        self.assertEqual("move_failed", outcome["status"], outcome)
        self.assertTrue(
            any("no exclusive directory-entry move" in issue for issue in outcome["issues"]),
            outcome["issues"],
        )
        self.assertTrue(
            any("never falls back" in issue for issue in outcome["issues"]), outcome["issues"]
        )
        self.assertEqual(b"CLAIMED_ORIGINAL\n", path.read_bytes(), "对象必须还在原保留位置")
        self.assertEqual(inode, path.stat().st_ino, outcome)
        self.assertEqual([], self._trash_entries(), outcome)
        self.assertFalse(self._resolutions_exist(), outcome)
        self.assertEqual([], self._staging_leftovers(), outcome)
        payload = self._self_check()
        self.assertEqual(0, payload["pending_move_intent_count"], payload["move_intents"])

    def test_a_successful_move_leaves_no_private_name_behind(self):
        path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        inode = path.stat().st_ino

        code, outcome = self._resolve(path, "--trash")

        self.assertEqual(0, code, outcome)
        self.assertEqual("moved_to_trash", outcome["status"], outcome)
        self.assertEqual(["note.md"], [entry.name for entry in self._trash_entries()], outcome)
        self.assertEqual(inode, Path(outcome["trash_path"]).stat().st_ino, outcome)
        self.assertEqual([], self._staging_leftovers(), "成功不留物：不得留下私有名字")
        resolutions = self._resolutions()
        self.assertEqual(1, len(resolutions), resolutions)
        self.assertEqual(inode, resolutions[0]["object_identity"]["ino"], resolutions)


class RetainedConcurrentResolveTest(_RetainedTrashFixture):
    """并发验收（任务卡 §13）：同一路径两进程各跑一次 --trash，对象只能被处理一次。

    对象本身由排他目录项移动保护（永不覆盖、永不销毁），但"这个对象是不是已经
    处理过了"必须由跨进程临界区保护：读记录、动手、追加记录这三步若分开，两个
    进程都会读到"还没处理"，于是都动手、都追加一条处理记录，后写的那条把一次
    没有发生的移动报成成功。下面第一个用例用一个真正持有的 flock 证明这个临界
    区存在；第二个用例用两个真实进程证明正常命令既不会重复处理，也不会互相卡死。
    """

    def _lock_path(self):
        return retained_objects.resolutions_path(self.gate.config).with_name(
            retained_objects.LOCK_FILENAME
        )

    def _hold_the_lock(self):
        """另一个 fd ⇒ 另一个打开文件描述：flock 在同一进程里同样互斥。"""
        import fcntl

        handle = os.open(str(self._lock_path()), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:  # pragma: no cover - the fixture lock is never contended
            os.close(handle)
            self.fail(f"夹具拿不到锁，控制组无效: {exc}")
        return handle

    def test_a_lock_held_elsewhere_makes_this_run_report_busy_and_change_nothing(self):
        path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        inode = path.stat().st_ino
        handle = self._hold_the_lock()
        try:
            with patch.object(retained_objects, "LOCK_WAIT_SECONDS", 0.05):
                code, outcome = self._resolve(path, "--trash")
        finally:
            os.close(handle)

        self.assertEqual(1, code, outcome)
        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual("move_busy", outcome["status"], outcome)
        self.assertEqual("busy", outcome["serialization"], outcome)
        self.assertIn("another process", " ".join(outcome["issues"]), outcome)
        self.assertEqual(b"CLAIMED_ORIGINAL\n", path.read_bytes(), "忙时不得动对象")
        self.assertEqual(inode, path.stat().st_ino, outcome)
        self.assertEqual([], self._trash_entries(), "忙时不得移进废纸篓")
        self.assertFalse(self._resolutions_exist(), "忙时不得追加处理记录")
        self.assertEqual([], self._staging_leftovers(), outcome)

        # 锁放开后同一条命令照常完成：忙只说明"这次没轮到"，不是对象有问题。
        code, second = self._resolve(path, "--trash")
        self.assertEqual(0, code, second)
        self.assertEqual("moved_to_trash", second["status"], second)
        self.assertEqual("locked", second["serialization"], second)
        self.assertEqual(inode, Path(second["trash_path"]).stat().st_ino, second)
        self.assertEqual(1, len(self._resolutions()), second)

    def test_two_processes_racing_for_one_path_handle_it_exactly_once(self):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(IMPLEMENTATION_ROOT / "src")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        recorded_inodes = []
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            for round_number in range(3):
                name = f".note.md.{4300 + round_number}.rollback"
                path = self._registered_object(name=name, content=b"CLAIMED_ORIGINAL\n")
                inode = path.stat().st_ino
                recorded_inodes.append(inode)
                barrier = scratch / f"start-{round_number}"
                outputs = [scratch / f"out-{round_number}-{index}.json" for index in range(2)]
                processes = [
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-B",
                            "-c",
                            _NEW_PROCESS_RESOLVE_TRASH,
                            str(self.gate.config.path),
                            str(path),
                            str(barrier),
                            str(output),
                        ],
                        cwd=str(IMPLEMENTATION_ROOT),
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    for output in outputs
                ]
                barrier.touch()
                outcomes = []
                for process, output in zip(processes, outputs):
                    stdout, stderr = process.communicate(timeout=120)
                    self.assertEqual(0, process.returncode, f"{stdout}\n{stderr}")
                    self.assertNotEqual(os.getpid(), process.pid, "并发验收必须在另一个进程里跑")
                    outcomes.append(json.loads(output.read_text(encoding="utf-8")))

                self.assertEqual(
                    ["already_resolved", "moved_to_trash"],
                    sorted(item["status"] for item in outcomes),
                    f"第 {round_number} 轮：必须恰好一个进程真的移动，另一个如实报已处理",
                )
                self.assertFalse(path.exists(), f"第 {round_number} 轮：对象必须已离开原名")
                self.assertEqual([], self._staging_leftovers(), "不留私有名字")
                resolutions = self._resolutions()
                self.assertEqual(round_number + 1, len(resolutions), resolutions)
                self.assertEqual(
                    round_number + 1,
                    len([row for row in resolutions if row["action"] == "moved_to_trash"]),
                    "每轮只能追加一条处理记录，重复的那次绝不能再记一遍",
                )

            entries = self._trash_entries()
            self.assertEqual(3, len(entries), entries)
            self.assertEqual(
                sorted(recorded_inodes),
                sorted(entry.stat().st_ino for entry in entries),
                "废纸篓里的三条必须是三个不同的原对象，谁也不得覆盖谁",
            )

    def _resolve_in_new_processes(self, path, tag, count=2):
        """count 个真实新进程跑同一条 --trash，门闩同步；返回各自的结果 JSON。"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(IMPLEMENTATION_ROOT / "src")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            barrier = scratch / f"start-{tag}"
            outputs = [scratch / f"out-{tag}-{index}.json" for index in range(count)]
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        _NEW_PROCESS_RESOLVE_TRASH,
                        str(self.gate.config.path),
                        str(path),
                        str(barrier),
                        str(output),
                    ],
                    cwd=str(IMPLEMENTATION_ROOT),
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for output in outputs
            ]
            barrier.touch()
            outcomes = []
            for process, output in zip(processes, outputs):
                stdout, stderr = process.communicate(timeout=120)
                self.assertEqual(0, process.returncode, f"{stdout}\n{stderr}")
                self.assertNotEqual(os.getpid(), process.pid, "并发验收必须在另一个进程里跑")
                outcomes.append(json.loads(output.read_text(encoding="utf-8")))
            return outcomes

    def test_a_lock_that_cannot_be_created_refuses_the_command_and_changes_nothing(self):
        """反：锁建立不了时（锁路径被真实占成目录），这条单对象命令必须整条停下。

        拿不到本命令的串行锁，就不能再自称"这个对象还没被处理"：移动、公告、补记、
        处理记录一样都不许发生。锁路径是真实预占的目录，不是 mock；正控制组证明同一
        对象、同一条命令在锁能建立时照常完成且只处理一次。
        """
        path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        inode = path.stat().st_ino
        lock_path = self._lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.mkdir(mode=0o700)
        try:
            code, outcome = self._resolve(path, "--trash")
        finally:
            lock_path.rmdir()

        self.assertEqual(1, code, outcome)
        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual("lock_unavailable", outcome["status"], outcome)
        self.assertEqual("unavailable", outcome["serialization"], outcome)
        issues = " ".join(outcome["issues"])
        self.assertIn(str(lock_path), issues, outcome["issues"])
        self.assertIn("not run", issues, outcome["issues"])
        self.assertTrue(outcome["retry"], outcome)
        self.assertIn(str(lock_path), outcome["retry"], outcome)
        self.assertEqual(b"CLAIMED_ORIGINAL\n", path.read_bytes(), "拿不到锁时不得动对象")
        self.assertEqual(inode, path.stat().st_ino, outcome)
        self.assertEqual([], self._trash_entries(), "拿不到锁时不得移进废纸篓")
        self.assertFalse(self._resolutions_exist(), "拿不到锁时不得追加处理记录")
        self.assertEqual([], self._staging_leftovers(), outcome)
        payload = self._self_check()
        self.assertEqual([], payload["move_intents"], "拿不到锁时连移动公告都不许写")
        self.assertEqual(0, payload["pending_move_intent_count"], payload["move_intents"])

        # 正控制：锁能建立时同一条命令照常完成，且只处理一次。
        code, second = self._resolve(path, "--trash")
        self.assertEqual(0, code, second)
        self.assertEqual("moved_to_trash", second["status"], second)
        self.assertEqual("locked", second["serialization"], second)
        self.assertEqual(inode, Path(second["trash_path"]).stat().st_ino, second)
        self.assertEqual(1, len(self._resolutions()), second)

    def test_a_platform_without_flock_refuses_instead_of_changing_anything(self):
        """反：平台没有 flock 时同样不许动手——没有串行化就没有这条命令。"""
        path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        inode = path.stat().st_ino

        with patch.object(retained_objects, "fcntl", None):
            code, outcome = self._resolve(path, "--trash")

        self.assertEqual(1, code, outcome)
        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual("lock_unavailable", outcome["status"], outcome)
        self.assertEqual("unavailable", outcome["serialization"], outcome)
        self.assertIn("no flock", " ".join(outcome["issues"]), outcome["issues"])
        self.assertEqual(b"CLAIMED_ORIGINAL\n", path.read_bytes(), "没有锁时不得动对象")
        self.assertEqual(inode, path.stat().st_ino, outcome)
        self.assertEqual([], self._trash_entries(), outcome)
        self.assertFalse(self._resolutions_exist(), outcome)
        self.assertEqual([], self._staging_leftovers(), outcome)
        self.assertEqual([], self._self_check()["move_intents"], "没有锁时不得写公告")

    def test_the_first_run_in_a_fresh_runtime_still_takes_its_lock(self):
        """正：元数据目录还不存在时必须能建锁，绝不能因为目录缺失退化成"没有锁"。"""
        directory = self._graph_dir()
        path = directory / ".fresh.md.4242.rollback"
        path.write_bytes(b"CLAIMED_ORIGINAL\n")
        inode = path.stat().st_ino
        self.assertFalse(
            self._lock_path().exists(), "控制组要求此刻还没有锁文件"
        )
        self.assertFalse(
            self._lock_path().parent.exists(), "控制组要求元数据目录此刻还不存在"
        )

        code, outcome = self._resolve(path, "--trash")

        self.assertEqual(0, code, outcome)
        self.assertEqual("moved_to_trash", outcome["status"], outcome)
        self.assertEqual("locked", outcome["serialization"], outcome)
        self.assertEqual(inode, Path(outcome["trash_path"]).stat().st_ino, outcome)
        self.assertTrue(self._lock_path().exists(), "首次运行必须真的建立了锁文件")
        self.assertEqual(1, len(self._resolutions()), outcome)

    def test_two_processes_without_a_lock_neither_move_nor_record_twice(self):
        """反+正（真实双进程、受控时序）：锁建立不了时两个进程都必须整条停下。

        两个真实新进程、锁路径被真实预占成目录：谁都拿不到锁，于是谁都不得移动、
        不得写公告、不得追加处理记录。锁恢复后同一条命令由真实新进程完成，且废纸篓
        里只有原对象一条、处理记录只有一条——不重复成功，也不重复登记。
        """
        path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        inode = path.stat().st_ino
        lock_path = self._lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.mkdir(mode=0o700)
        try:
            refused = self._resolve_in_new_processes(path, "no-lock")
        finally:
            lock_path.rmdir()

        self.assertEqual(
            ["lock_unavailable", "lock_unavailable"],
            sorted(item["status"] for item in refused),
            refused,
        )
        self.assertEqual(b"CLAIMED_ORIGINAL\n", path.read_bytes(), refused)
        self.assertEqual(inode, path.stat().st_ino, refused)
        self.assertEqual([], self._trash_entries(), "拿不到锁时两个真实进程都不得移动")
        self.assertFalse(self._resolutions_exist(), "拿不到锁时两个真实进程都不得追加记录")
        self.assertEqual([], self._self_check()["move_intents"], "拿不到锁时不得写公告")

        completed = self._resolve_in_new_processes(path, "lock-restored")
        self.assertEqual(
            ["already_resolved", "moved_to_trash"],
            sorted(item["status"] for item in completed),
            completed,
        )
        resolutions = self._resolutions()
        self.assertEqual(1, len(resolutions), resolutions)
        self.assertEqual(inode, Path(resolutions[0]["trash_path"]).stat().st_ino, resolutions)
        entries = self._trash_entries()
        self.assertEqual(1, len(entries), entries)
        self.assertEqual(inode, entries[0].stat().st_ino, entries)


class RetainedMovedButNotRecordedTest(_RetainedTrashFixture):
    """缺口 3（必须修）：移动已成功但记录失败时，补记必须可执行。

    移动之前先写下带身份的移动公告；记录失败时公告留在原处，下一次运行同一条
    命令凭公告里的精确身份在废纸篓里找到对象，补上记录并保留真实位置。找不到
    就不猜、不按同名认领，重复运行时也绝不重复补记。
    """

    def test_a_move_whose_record_failed_is_completed_by_the_next_run(self):
        path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        inode = path.stat().st_ino

        with patch.object(
            retained_objects, "record_resolution", return_value="injected metadata failure"
        ):
            code, first = self._resolve(path, "--trash")

        self.assertEqual(1, code, first)
        self.assertEqual("moved_but_not_recorded", first["status"], first)
        self.assertFalse(path.exists(), first)
        trash_path = Path(first["trash_path"])
        self.assertEqual(inode, trash_path.stat().st_ino, first)
        self.assertFalse(self._resolutions_exist(), first)

        # 清单此时必须如实报告：公告还在，对象真的在废纸篓里，只是没有记录。
        payload_before = self._self_check()
        views = [
            view for view in payload_before["move_intents"] if view["retained_path"] == str(path)
        ]
        self.assertEqual(1, len(views), payload_before["move_intents"])
        self.assertEqual("in_trash", views[0]["object_location"], views[0])
        self.assertEqual(str(trash_path), views[0]["object_location_path"], views[0])
        self.assertIn(
            "move_intent_without_resolution",
            [item["kind"] for item in payload_before["anomalies"]],
            payload_before["anomalies"],
        )
        self.assertEqual(1, payload_before["pending_move_intent_count"], payload_before)

        # 用户按回执指示恢复可写后重跑同一条命令：只补记，不重复移动。
        code, second = self._resolve(path, "--trash")

        self.assertEqual(0, code, second)
        self.assertTrue(second["ok"], second)
        self.assertEqual("moved_to_trash", second["status"], second)
        self.assertTrue(second.get("recorded_after_move"), second)
        self.assertEqual(str(trash_path), second["trash_path"], second)
        self.assertEqual(inode, trash_path.stat().st_ino, second)

        # 换一个新进程看磁盘：不再算待办，历史里保留真实废纸篓位置。
        payload_after = self._self_check_in_new_process(self.gate.config)
        self.assertEqual(0, payload_after["pending_count"], payload_after["objects"])
        self.assertEqual(0, payload_after["pending_move_intent_count"], payload_after)
        self.assertEqual(0, payload_after["anomaly_count"], payload_after["anomalies"])
        resolved = {item["retained_path"]: item for item in payload_after["resolved_objects"]}
        self.assertIn(str(path), resolved, payload_after["resolved_objects"])
        self.assertEqual(str(trash_path), resolved[str(path)]["trash_path"], resolved[str(path)])
        self.assertEqual(inode, resolved[str(path)]["object_identity"]["ino"], resolved[str(path)])

        # 再跑一次：已经是已处理，绝不重复补记。
        code, third = self._resolve(path, "--trash")
        self.assertEqual(0, code, third)
        self.assertEqual("already_resolved", third["status"], third)
        self.assertEqual(str(trash_path), third["trash_path"], third)
        self.assertEqual(1, len(self._resolutions()), "补记只能有一条")

    def test_the_backfill_reports_the_real_location_when_the_record_still_fails(self):
        path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        inode = path.stat().st_ino

        with patch.object(
            retained_objects, "record_resolution", return_value="injected metadata failure"
        ):
            code, first = self._resolve(path, "--trash")
            self.assertEqual(1, code, first)
            self.assertEqual("moved_but_not_recorded", first["status"], first)

            code, again = self._resolve(path, "--trash")

        self.assertEqual(1, code, again)
        self.assertEqual("moved_but_not_recorded", again["status"], again)
        self.assertTrue(
            any("injected metadata failure" in issue for issue in again["issues"]), again
        )
        self.assertEqual(first["trash_path"], again["trash_path"], "真实位置不得丢失")
        self.assertEqual(inode, Path(again["trash_path"]).stat().st_ino, again)
        self.assertTrue(again["retry"], again)
        self.assertFalse(self._resolutions_exist(), again)

        # 权限恢复后，同一条命令补上记录。
        code, done = self._resolve(path, "--trash")
        self.assertEqual(0, code, done)
        self.assertEqual("moved_to_trash", done["status"], done)
        self.assertTrue(done.get("recorded_after_move"), done)
        self.assertEqual(1, len(self._resolutions()), done)

    def test_a_duplicate_announcement_is_recorded_once(self):
        path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        identity = self._identity(path.stat())

        with patch.object(
            retained_objects, "record_resolution", return_value="injected metadata failure"
        ):
            code, first = self._resolve(path, "--trash")
            self.assertEqual(1, code, first)

        # 同一个对象被公告了两次（例如上一次运行在写公告后、记录前退出）
        self.assertIsNone(self._announce(path, identity, note="a second announcement"), "公告必须写得进去")

        code, second = self._resolve(path, "--trash")

        self.assertEqual(0, code, second)
        self.assertEqual("moved_to_trash", second["status"], second)
        self.assertEqual(1, len(self._resolutions()), "重复公告不得补记两次")
        payload = self._self_check()
        self.assertEqual(0, payload["pending_move_intent_count"], payload)

    def test_an_announcement_never_adopts_a_same_named_object_from_the_trash(self):
        path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        identity = self._identity(path.stat())
        # 崩溃窗口：对象不在原位，废纸篓里只有一个同名但不同身份的对象。
        stranger = b"an unrelated object that happens to carry the same name\n"
        (self.trash / "note.md").write_bytes(stranger)
        path.unlink()
        self.assertIsNone(self._announce(path, identity), "公告必须写得进去")

        code, outcome = self._resolve(path, "--trash")

        self.assertEqual(1, code, outcome)
        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual("object_absent", outcome["status"], outcome)
        self.assertTrue(
            any("no same-named object was adopted" in issue for issue in outcome["issues"]), outcome
        )
        self.assertEqual(stranger, (self.trash / "note.md").read_bytes(), "同名的别人对象绝不能被认领")
        self.assertFalse(self._resolutions_exist(), outcome)

        payload = self._self_check()
        self.assertEqual(1, payload["pending_move_intent_count"], payload["move_intents"])
        self.assertEqual("unaccounted", payload["move_intents"][0]["object_location"], payload)

    def test_confirm_deleted_never_stands_in_for_the_missing_trash_record(self):
        """用户谎称"我已手动删除"不能替代真实的移入废纸篓记录。"""
        path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        identity = self._identity(path.stat())
        self.assertIsNone(self._announce(path, identity), "公告必须写得进去")
        # 移动真的发生了：对象就在废纸篓里，只是它的记录没写成。
        os.replace(path, self.trash / "note.md")

        code, outcome = self._resolve(path, "--confirm-deleted")

        self.assertEqual(1, code, outcome)
        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual("move_announcement_pending", outcome["status"], outcome)
        self.assertEqual(str(self.trash / "note.md"), outcome["trash_path"], outcome)
        self.assertIn("--trash", outcome["retry"], outcome)
        self.assertFalse(self._resolutions_exist(), "确认被拒绝，不得写下任何记录")
        payload = self._self_check()
        self.assertEqual(1, payload["pending_move_intent_count"], payload["move_intents"])
        self.assertEqual("in_trash", payload["move_intents"][0]["object_location"], payload)

        # 回执指示的那条路真的能走通：同一条命令把缺的记录补上。
        code, done = self._resolve(path, "--trash")
        self.assertEqual(0, code, done)
        self.assertEqual("moved_to_trash", done["status"], done)
        self.assertEqual(1, len(self._resolutions()), done)

    def test_confirm_deleted_retires_an_announcement_whose_object_is_gone(self):
        """对象按身份哪里都找不到时，用户的确认被如实登记，公告作为"下落不明"退休。"""
        path = self._registered_object(content=b"CLAIMED_ORIGINAL\n")
        identity = self._identity(path.stat())
        self.assertIsNone(self._announce(path, identity), "公告必须写得进去")
        os.replace(path, self.gate.root / "handled-by-hand")

        code, outcome = self._resolve(path, "--confirm-deleted")

        self.assertEqual(0, code, outcome)
        self.assertEqual("confirmed_deleted", outcome["status"], outcome)
        resolutions = self._resolutions()
        self.assertEqual(1, len(resolutions), resolutions)
        self.assertEqual("confirmed_deleted", resolutions[0]["action"], resolutions)
        self.assertIsNone(resolutions[0]["trash_path"], "绝不允许把它改写成移入废纸篓")
        payload = self._self_check()
        self.assertEqual(0, payload["pending_move_intent_count"], payload["move_intents"])
        self.assertEqual(
            0,
            len(
                [
                    item
                    for item in payload["anomalies"]
                    if item["kind"] == "move_intent_without_resolution"
                ]
            ),
            payload["anomalies"],
        )


if __name__ == "__main__":
    unittest.main()
