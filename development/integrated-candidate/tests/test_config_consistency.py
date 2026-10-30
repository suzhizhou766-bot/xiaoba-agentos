import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.health import config_consistency

from fixtures_review import _config


ENTRYPOINT = '''#!/usr/bin/env python3
from pathlib import Path
DEFAULT_CONFIG = Path("{config}")
VERIFY_ONLY = False
print("dispatch")
'''

CANDIDATE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "agentos.json"


class ConfigConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.home = Path(self.tmp.name) / "home"
        self.root.mkdir()
        self.config = _config(self.root, "config/agentos.json")
        self.override = self.root / "runtime" / "model-overrides" / "active.json"
        self.override.parent.mkdir(parents=True)
        self.override.write_bytes(self.config.path.read_bytes())
        self.reference = self.root / ".agents" / "skills" / "xiaoba-agentos" / "scripts"
        self.reference.mkdir(parents=True)
        self.installed = self.home / ".agents" / "skills" / "xiaoba-agentos" / "scripts"
        self.installed.mkdir(parents=True)
        (self.reference / "xiaoba").write_text(
            ENTRYPOINT.format(config=self.config.path), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _install(self, config_path: Path, extra: str = "") -> None:
        (self.installed / "xiaoba").write_text(
            ENTRYPOINT.format(config=config_path) + extra, encoding="utf-8")

    def _service(self, label: str, config_path: Path) -> None:
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        with (agents / f"{label}.plist").open("wb") as handle:
            plistlib.dump({"Label": label, "EnvironmentVariables": {"XIAOBA_CONFIG": str(config_path)}}, handle)

    def test_single_config_everywhere_is_clean(self):
        self._install(self.config.path)
        self._service("com.xiaoba.agentos", self.config.path)
        self._service("com.xiaoba.review-center", self.config.path)
        status = config_consistency(self.config, home=self.home)
        self.assertTrue(status["ok"], status)
        self.assertEqual([], status["warnings"])
        self.assertEqual(
            {"session", "installed_entrypoint:.agents", "worker_service", "review_center_service"},
            set(status["consumers"]),
        )

    def test_different_paths_with_equal_content_warn_about_drift(self):
        self._install(self.override)
        self._service("com.xiaoba.agentos", self.override)
        self._service("com.xiaoba.review-center", self.config.path)
        status = config_consistency(self.config, home=self.home)
        self.assertTrue(status["ok"], status)
        self.assertEqual(1, len(status["warnings"]))

    def _backend_state(self, config_path, pid=None) -> Path:
        state = self.config.runtime_dir / "review-center" / "backend-state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"pid": os.getpid() if pid is None else pid, "config_path": str(config_path)}),
                         encoding="utf-8")
        return state

    def _candidate_schema_two(self) -> tuple:
        """随包 schema 2 配置，和把 runtime_dir 改指临时目录的那一份。

        随包配置里的 runtime_dir/project_dir 是本机绝对路径，检查函数照着它们
        会读到真实的 .runtime 和 .agents（测试因此变成依赖本机状态的非隔离测试）。
        夹具只替换 runtime_dir，schema 2 的其余内容和 project_dir 原样不动。
        """
        shipped = AppConfig.load(CANDIDATE_CONFIG)
        isolated = AppConfig(shipped.path, {**shipped.raw, "runtime_dir": str(self.config.runtime_dir)})
        return shipped, isolated

    def _release_reference(self, release_dir: Path, config_path: Path) -> Path:
        scripts = release_dir / "agentos" / ".agents" / "skills" / "xiaoba-agentos" / "scripts"
        scripts.mkdir(parents=True)
        reference = scripts / "xiaoba"
        reference.write_text(ENTRYPOINT.format(config=config_path), encoding="utf-8")
        return reference

    @staticmethod
    def _under(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
        except ValueError:
            return False
        return True

    def _assert_reads_only_fixtures(self, status: dict, shipped: AppConfig, reference: Path) -> None:
        """检查报告里的每个路径都必须落在夹具里，不碰本机运行目录和已安装入口。

        Forbidden 根用配置里的原始字符串比较，不走 runtime_dir/project_dir 属性，
        免得断言自己对本机路径发起解析。
        """
        forbidden = (Path(shipped.raw["runtime_dir"]), Path(shipped.raw["project_dir"]) / ".agents",
                     Path(shipped.raw["project_dir"]) / ".codex",
                     Path.home() / ".agents", Path.home() / ".codex",
                     Path.home() / "Library" / "LaunchAgents")
        seen = [Path(entry["path"]) for entry in status["consumers"].values()]
        for entry in status["installed_entrypoints"].values():
            seen += [Path(entry["installed"]), Path(entry["reference"])]
        for path in seen:
            self.assertFalse(any(self._under(path, root) for root in forbidden), (path, seen))
        self.assertEqual(str(reference), status["installed_entrypoints"][".agents/xiaoba"]["reference"])
        self.assertEqual(str(CANDIDATE_CONFIG), status["consumers"]["review_backend"]["path"])

    def test_running_review_backend_is_a_consumer(self):
        self._install(self.config.path)
        self._service("com.xiaoba.agentos", self.config.path)
        self._backend_state(self.config.path)
        status = config_consistency(self.config, home=self.home)
        self.assertTrue(status["ok"], status)
        self.assertEqual([], status["warnings"])
        self.assertEqual(
            {"session", "installed_entrypoint:.agents", "worker_service", "review_backend"},
            set(status["consumers"]),
        )

    def test_running_review_backend_on_diverged_config_fails_closed(self):
        self._install(self.config.path)
        self._service("com.xiaoba.agentos", self.config.path)
        raw = json.loads(self.override.read_text(encoding="utf-8"))
        raw["agents"][0]["enabled"] = False
        self.override.write_text(json.dumps(raw), encoding="utf-8")
        self._backend_state(self.override)
        status = config_consistency(self.config, home=self.home)
        self.assertFalse(status["ok"])
        self.assertIn("review_backend", status["consumers"])

    def test_running_review_backend_on_other_path_with_equal_content_warns(self):
        self._install(self.config.path)
        self._service("com.xiaoba.agentos", self.config.path)
        self._backend_state(self.override)   # same bytes as the session config, different file
        status = config_consistency(self.config, home=self.home)
        self.assertTrue(status["ok"], status)
        self.assertEqual(1, len(status["warnings"]))
        self.assertIn("review_backend", status["consumers"])

    def test_review_backend_that_is_not_running_is_ignored(self):
        self._install(self.config.path)
        self._service("com.xiaoba.agentos", self.config.path)
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait()
        raw = json.loads(self.override.read_text(encoding="utf-8"))
        raw["agents"][0]["enabled"] = False
        self.override.write_text(json.dumps(raw), encoding="utf-8")
        self._backend_state(self.override, pid=finished.pid)   # stale record of an exited backend
        status = config_consistency(self.config, home=self.home)
        self.assertTrue(status["ok"], status)
        self.assertNotIn("review_backend", status["consumers"])
        for broken in ("not json", json.dumps({"pid": "x"}), json.dumps({"pid": 0, "config_path": str(self.override)}),
                       json.dumps({"pid": os.getpid(), "config_path": None}), json.dumps({"pid": os.getpid(), "config_path": 7}),
                       json.dumps({"pid": os.getpid(), "config_path": "  "}), json.dumps([os.getpid()])):
            self._backend_state(self.config.path).write_text(broken, encoding="utf-8")
            self.assertNotIn("review_backend", config_consistency(self.config, home=self.home)["consumers"])

    def test_diverged_content_fails_closed(self):
        self._install(self.override)
        self._service("com.xiaoba.agentos", self.override)
        raw = json.loads(self.override.read_text(encoding="utf-8"))
        raw["agents"][0]["enabled"] = False  # what agent-disable does to one file only
        self.override.write_text(json.dumps(raw), encoding="utf-8")
        status = config_consistency(self.config, home=self.home)
        self.assertFalse(status["ok"])
        self.assertIn("配置内容不一致", status["problems"][0])

    def test_installed_entrypoint_may_only_differ_in_default_config(self):
        self._install(self.override)
        status = config_consistency(self.config, home=self.home)
        self.assertTrue(status["installed_entrypoints"][".agents/xiaoba"]["ok"], status)

        self._install(self.override, extra="import os  # tampered\n")
        status = config_consistency(self.config, home=self.home)
        self.assertFalse(status["ok"])
        self.assertFalse(status["installed_entrypoints"][".agents/xiaoba"]["ok"])

    def test_missing_service_config_is_a_problem(self):
        self._service("com.xiaoba.agentos", self.root / "config" / "gone.json")
        status = config_consistency(self.config, home=self.home)
        self.assertFalse(status["ok"])

    def test_codex_entrypoint_is_covered_too(self):
        codex = self.home / ".codex" / "skills" / "xiaoba-agentos" / "scripts"
        codex.mkdir(parents=True)
        (codex / "xiaoba").write_text(ENTRYPOINT.format(config=self.override), encoding="utf-8")
        status = config_consistency(self.config, home=self.home)
        self.assertTrue(status["installed_entrypoints"][".codex/xiaoba"]["ok"], status)
        self.assertIn("installed_entrypoint:.codex", status["consumers"])

        (codex / "xiaoba").write_text(
            ENTRYPOINT.format(config=self.override) + "import os  # tampered\n", encoding="utf-8")
        self.assertFalse(config_consistency(self.config, home=self.home)["ok"])

    def test_unparseable_default_config_is_reported_not_skipped(self):
        (self.installed / "xiaoba").write_text(
            "#!/usr/bin/env python3\nDEFAULT_CONFIG = compute()\n", encoding="utf-8")
        status = config_consistency(self.config, home=self.home)
        self.assertFalse(status["ok"])
        self.assertFalse(status["installed_entrypoints"][".agents/xiaoba"]["ok"])

    def test_other_installations_are_ignored(self):
        elsewhere = Path(self.tmp.name) / "other-project" / "config.json"
        elsewhere.parent.mkdir()
        elsewhere.write_text("{}", encoding="utf-8")
        self._install(elsewhere, extra="# unrelated installation\n")
        self._service("com.xiaoba.agentos", elsewhere)
        status = config_consistency(self.config, home=self.home)
        self.assertTrue(status["ok"], status)
        self.assertEqual({"session"}, set(status["consumers"]))
        self.assertEqual({}, status["installed_entrypoints"])

    def test_schema_two_recovery_contract_consistency(self):
        shipped, isolated = self._candidate_schema_two()
        reference = self._release_reference(self.root / "release", CANDIDATE_CONFIG)
        self._install(CANDIDATE_CONFIG)
        self._service("com.xiaoba.agentos", CANDIDATE_CONFIG)
        self._backend_state(CANDIDATE_CONFIG)
        status = config_consistency(isolated, {"release_dir": str(self.root / "release")}, home=self.home)
        self.assertTrue(status["ok"], status)
        self.assertEqual([], status["warnings"])
        self.assertEqual(
            {"session", "installed_entrypoint:.agents", "worker_service", "review_backend"},
            set(status["consumers"]),
        )
        self._assert_reads_only_fixtures(status, shipped, reference)

    def test_candidate_fixture_still_detects_a_diverged_entrypoint_reference(self):
        # 反例：夹具里的入口参照被改动后，这项检查必须报错；
        # 报错就证明比对的是临时参照而不是本机 .agents，隔离没有把校验变成空跑。
        shipped, isolated = self._candidate_schema_two()
        reference = self._release_reference(self.root / "release", CANDIDATE_CONFIG)
        reference.write_text(
            ENTRYPOINT.format(config=CANDIDATE_CONFIG) + "import os  # diverged reference\n", encoding="utf-8")
        self._install(CANDIDATE_CONFIG)
        self._service("com.xiaoba.agentos", CANDIDATE_CONFIG)
        self._backend_state(CANDIDATE_CONFIG)
        status = config_consistency(isolated, {"release_dir": str(self.root / "release")}, home=self.home)
        self.assertFalse(status["ok"])
        self.assertTrue(any("已安装入口与发布入口不一致" in p for p in status["problems"]), status["problems"])
        self._assert_reads_only_fixtures(status, shipped, reference)


if __name__ == "__main__":
    unittest.main()
