"""D3 只读验收：显式 staging 实体根只在受控入口生效，默认仍核验集中声明的正式部署位置。

缺省正式定位的“缺实体失败/存在通过”两态、显式 staging 分支、构包门槛全部用临时 fixtures
与测试层受控声明模拟：不改生产代码、不加隐藏环境变量或配置键；需要子进程时经测试夹具
包装器注入替身声明。不依赖这台机器真实共享目录里装没装过实体。
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CANDIDATE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CANDIDATE / "src"))
sys.path.insert(0, str(CANDIDATE / "tests"))

from fixtures_review import _raw_config, _run_cli  # noqa: E402
from test_aesthetics import _physical_temp, simulated_formal_root, _script  # noqa: E402
from xiaoba_agentos.cli import build_parser  # noqa: E402
from xiaoba_agentos.skills import entity_source_path  # noqa: E402
from xiaoba_agentos.skills import SkillRegistry  # noqa: E402

SKILLS_ROOT = CANDIDATE / "tests" / "fixtures" / "content" / "skills"  # 发布副本根：staging 实体夹具从这里拷贝，不依赖真实共享目录

# 执行入口：不得接受实体根覆盖。基线 argv 本身必须合法，否则拒绝断言是空的。
EXECUTION_ARGV = (
    ["run"],
    ["session-start"],
    ["submit", "任务"],
    ["intake", "任务"],
    ["task-resume", "t1", "--freeze-hash", "0" * 64],
    ["bridge"],
    [
        "collaborate",
        "任务",
        "--context-file", "ctx.md",
        "--message-id", "msg-1",
        "--assignments-file", "assignments.json",
    ],
    [
        "collaborate-review",
        "任务",
        "--context-file", "ctx.md",
        "--message-id", "msg-1",
        "--work-task-id", "t1",
        "--artifact", "artifact.md",
    ],
)
READ_ONLY_ARGV = (
    ["doctor"],
    ["skill-verify"],
    ["skill-list"],
    ["skill-match", "帮我调色"],
)

# 测试夹具包装器（只存在于临时目录）：把集中声明的正式共享根指向注入根后进入 CLI。
# 生产代码不读取任何环境变量或配置键；替身声明只在这个包装器进程里生效。
CLI_FORMAL_WRAPPER = '''"""测试夹具包装器：替身正式声明 + CLI（仅测试进程）。"""
import sys
from pathlib import Path

candidate = Path(sys.argv[1])
sys.path.insert(0, str(candidate / "src"))

from xiaoba_agentos import skills as skills_module

skills_module.ENTITY_SHARED_ROOTS["aesthetics"] = sys.argv[2]
sys.argv = [sys.argv[0]] + sys.argv[3:]

from xiaoba_agentos.cli import main

raise SystemExit(main())
'''

# 同上，但目标脚本是索引核验（scripts/create_skill_index.py）。
INDEX_FORMAL_WRAPPER = '''"""测试夹具包装器：替身正式声明 + 索引核验（仅测试进程）。"""
import sys
from pathlib import Path

candidate = Path(sys.argv[1])
scripts = str(candidate / "scripts")
if scripts not in sys.path:
    sys.path.insert(0, scripts)

from xiaoba_agentos import skills as skills_module

skills_module.ENTITY_SHARED_ROOTS["aesthetics"] = sys.argv[2]
sys.argv = [sys.argv[0]] + sys.argv[3:]

from create_skill_index import main as index_main

raise SystemExit(index_main())
'''


def _write_wrapper(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.write_text(body, encoding="utf-8")
    return path


def _quiet_parse(argv):
    import io
    from contextlib import redirect_stderr

    with redirect_stderr(io.StringIO()):
        return build_parser().parse_args(argv)


def _write_config(root: Path, skills_root: Path = None, name: str = "config.json") -> Path:
    """schema_version=1 全 mock 配置：跳过发布策略校验，但仍提供 doctor 需要的全部键。

    skills_root 缺省指向真实候选发布副本；替身正式声明测试传入临时夹具根。
    """
    raw = _raw_config(root)
    skills_dir = Path(skills_root) if skills_root else SKILLS_ROOT
    raw["skills"] = {
        "root_dir": str(skills_dir),
        "index_path": str(skills_dir / "index.json"),
        "max_selected": 4,
    }
    raw["knowledge"] = {
        "index_path": str(CANDIDATE / "tests" / "fixtures" / "content" / "knowledge" / "index.json"),
        "allowed_roots": [str(CANDIDATE / "tests" / "fixtures" / "content" / "knowledge")],
    }
    raw["memory"].update(
        {
            "knowlp_python": str(root / "knowlp.py"),
            "knowlp_module": str(root / "knowlp_mcp.py"),
            "knowlp_enabled": False,
            "catalog_note": "经验索引.md",
        }
    )
    raw["integrations"] = {"node_command": str(root / "node")}
    path = root / name
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _skills_fixture(root: Path, name: str) -> Path:
    """临时 skills 夹具根：发布副本 + 只含 aesthetics 的 catalog（索引由调用方按替身声明生成）。"""
    skills = root / name
    skills.mkdir()
    shutil.copytree(SKILLS_ROOT / "aesthetics", skills / "aesthetics")
    catalog = json.loads((SKILLS_ROOT / "catalog.json").read_text(encoding="utf-8"))
    entry = copy.deepcopy(next(item for item in catalog["entries"] if item["id"] == "aesthetics"))
    (skills / "catalog.json").write_text(
        json.dumps({"schema_version": 1, "entries": [entry]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (skills.parent / "pyproject.toml").write_text(
        "[project]\nname = \"agentos-fixture\"\nversion = \"1.5.2\"\n", encoding="utf-8"
    )
    return skills


def _write_index(skills: Path, indexer, declared_root: Path, entity_root: Path) -> None:
    """在“替身正式声明=declared_root”下生成并写入夹具索引（与脚本 main 的渲染一致）。"""
    with simulated_formal_root(declared_root):
        generated = indexer.build(skills, entity_root=entity_root)
    (skills / "index.json").write_text(
        json.dumps(generated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class CliStagingAcceptanceTest(unittest.TestCase):
    """候选 CLI：缺省正式定位两态、显式 staging 通过，传参体现在自检结果里。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = _physical_temp(cls.tmp.name)
        cls.config = _write_config(cls.root)
        # staging 实体根：临时物理根 + 发布副本，独立于真实共享目录与工作区 artifacts。
        cls.entity_root = cls.root / "shared" / "skills"
        cls.entity_root.mkdir(parents=True)
        shutil.copytree(SKILLS_ROOT / "aesthetics", cls.entity_root / "aesthetics")
        # 缺省正式定位的两个替身：缺失态（空正式根）与存在态（合法实体根）。
        cls.missing_declared_root = cls.root / "formal-empty" / "skills"
        cls.missing_declared_root.mkdir(parents=True)
        cls.indexer = _script("create_skill_index")
        missing_skills = _skills_fixture(cls.root, "skills-missing")
        _write_index(missing_skills, cls.indexer, cls.missing_declared_root, cls.entity_root)
        cls.missing_config = _write_config(cls.root, skills_root=missing_skills, name="config-missing.json")
        installed_skills = _skills_fixture(cls.root, "skills-installed")
        _write_index(installed_skills, cls.indexer, cls.entity_root, cls.entity_root)
        cls.installed_config = _write_config(cls.root, skills_root=installed_skills, name="config-installed.json")
        cls.cli_wrapper = _write_wrapper(cls.root, "cli_formal_wrapper.py", CLI_FORMAL_WRAPPER)
        code, payload = _run_cli(["--config", str(cls.config), "init"])
        assert code == 0 and payload.get("status") == "initialized", payload

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_cli(self, argv, config=None):
        return _run_cli(["--config", str(config or self.config)] + argv)

    def test_default_mode_is_formal_without_any_override(self):
        """机器无关的形态断言：没有任何覆盖时，自检只报告正式模式与空实体根。"""
        _code, payload = self.run_cli(["skill-verify"])
        self.assertEqual("formal", payload["entity_root_mode"])
        self.assertIsNone(payload["entity_root"])

    def test_default_formal_missing_entity_fails(self):
        """替身正式根没有实体：缺省核验必须失败，且报的是 aesthetics 目录缺失。"""
        with simulated_formal_root(self.missing_declared_root):
            code, payload = self.run_cli(["skill-verify"], config=self.missing_config)
        self.assertEqual("formal", payload["entity_root_mode"])
        self.assertIsNone(payload["entity_root"])
        self.assertFalse(payload["ok"], payload["problems"])
        self.assertNotEqual(0, code)
        self.assertTrue(
            any(
                "共享实体目录缺失或为符号链接：aesthetics" in problem
                for problem in payload["problems"]
            ),
            payload["problems"],
        )

    def test_default_formal_installed_entity_passes(self):
        """替身正式根装有合法实体：缺省核验（不传任何参数）必须通过。"""
        with simulated_formal_root(self.entity_root):
            code, payload = self.run_cli(["skill-verify"], config=self.installed_config)
        self.assertEqual(0, code, payload)
        self.assertTrue(payload["ok"], payload["problems"])
        self.assertEqual("formal", payload["entity_root_mode"])
        self.assertIsNone(payload["entity_root"])

    def test_skill_verify_with_staging_override_passes(self):
        code, payload = self.run_cli(["skill-verify", "--entity-root", str(self.entity_root)])
        self.assertEqual(0, code, payload)
        self.assertTrue(payload["ok"], payload["problems"])
        self.assertEqual("staging", payload["entity_root_mode"])
        self.assertEqual(str(self.entity_root), payload["entity_root"])

    def test_skill_list_and_match_reflect_the_override(self):
        code, payload = self.run_cli(["skill-list", "--entity-root", str(self.entity_root)])
        self.assertEqual(0, code)
        self.assertIn("aesthetics", [skill["id"] for skill in payload["skills"]])
        code, payload = self.run_cli(
            ["skill-match", "这张海报排版留白不行", "--entity-root", str(self.entity_root)]
        )
        self.assertEqual(0, code)
        self.assertIn("aesthetics", [skill["id"] for skill in payload["matches"]])

    def test_doctor_reports_the_selected_entity_root(self):
        with simulated_formal_root(self.missing_declared_root):
            _code, payload = self.run_cli(["doctor"], config=self.missing_config)
        registry = payload["skill_registry"]
        self.assertEqual("formal", registry["entity_root_mode"])
        self.assertIsNone(registry["entity_root"])
        self.assertFalse(registry["ok"], registry["problems"])

        _code, payload = self.run_cli(["doctor", "--entity-root", str(self.entity_root)])
        registry = payload["skill_registry"]
        self.assertEqual("staging", registry["entity_root_mode"])
        self.assertEqual(str(self.entity_root), registry["entity_root"])
        self.assertTrue(registry["ok"], registry["problems"])

    def test_execution_entry_points_reject_the_override(self):
        for argv in EXECUTION_ARGV:
            _quiet_parse(argv)  # 基线合法
            with self.assertRaises(SystemExit, msg=str(argv)):
                _quiet_parse(argv + ["--entity-root", str(self.entity_root)])

    def test_read_only_entry_points_default_to_formal(self):
        for argv in READ_ONLY_ARGV:
            parsed = _quiet_parse(argv)
            self.assertIsNone(parsed.entity_root, str(argv))

    def test_override_is_not_discoverable_via_environment(self):
        """子进程经夹具包装器注入替身正式根：环境变量仍然必须被生产代码无视。"""
        env = dict(os.environ, AGENTOS_SKILL_ENTITY_ROOT=str(self.entity_root))
        completed = subprocess.run(
            [
                sys.executable, str(self.cli_wrapper), str(CANDIDATE), str(self.missing_declared_root),
                "--config", str(self.missing_config), "skill-verify",
            ],
            cwd=str(CANDIDATE), env=env, capture_output=True, text=True,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual("formal", payload["entity_root_mode"])
        self.assertIsNone(payload["entity_root"])
        self.assertFalse(payload["ok"], payload["problems"])
        self.assertNotEqual(0, completed.returncode)
        self.assertTrue(
            any("aesthetics" in problem for problem in payload["problems"]), payload["problems"]
        )


class MutatedStagingRootTest(unittest.TestCase):
    """staging 根被漂移、软链、扩文件后，同一显式参数路径必须失败；写入索引的仍是正式声明路径。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _physical_temp(self.tmp.name)
        self.entity_root = self.root / "shared" / "skills"
        shutil.copytree(SKILLS_ROOT / "aesthetics", self.entity_root / "aesthetics")
        self.config = _write_config(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def registry(self):
        return SkillRegistry(AppConfigLike(self.config), entity_root=self.entity_root)

    def problems(self):
        return self.registry().verify()["problems"]

    def test_copied_staging_root_passes(self):
        report = self.registry().verify()
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual("staging", report["entity_root_mode"])

    def test_index_metadata_keeps_the_declared_formal_path(self):
        entries = self.registry().entries()
        entry = next(item for item in entries if item["id"] == "aesthetics")
        self.assertEqual(entity_source_path("aesthetics"), entry["entity_source_path"])
        self.assertNotIn(str(self.entity_root), json.dumps(entries, ensure_ascii=False))

    def test_entity_directory_symlink_is_rejected(self):
        shutil.rmtree(self.entity_root / "aesthetics")
        elsewhere = self.root / "elsewhere" / "aesthetics"
        elsewhere.parent.mkdir()
        shutil.copytree(SKILLS_ROOT / "aesthetics", elsewhere)
        os.symlink(elsewhere, self.entity_root / "aesthetics", target_is_directory=True)
        self.assertTrue(
            any("共享实体目录" in problem and "符号链接" in problem for problem in self.problems()),
            self.problems(),
        )

    def test_entity_root_symlink_is_rejected(self):
        linked = self.root / "linked-root"
        os.symlink(self.entity_root, linked, target_is_directory=True)
        registry = SkillRegistry(AppConfigLike(self.config), entity_root=linked)
        problems = registry.verify()["problems"]
        self.assertTrue(any("共享实体根不得为符号链接" in problem for problem in problems), problems)

    def test_extra_file_in_entity_dir_is_rejected(self):
        (self.entity_root / "aesthetics" / "extra.md").write_text("额外\n", encoding="utf-8")
        self.assertTrue(
            any("文件集合不合规" in problem for problem in self.problems()), self.problems()
        )

    def test_entity_content_drift_is_rejected(self):
        skill = self.entity_root / "aesthetics" / "SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "\n漂移\n", encoding="utf-8")
        problems = self.problems()
        self.assertTrue(any("哈希不匹配" in problem for problem in problems), problems)
        self.assertTrue(any("发布副本不一致" in problem for problem in problems), problems)

    def test_missing_entity_dir_is_rejected(self):
        shutil.rmtree(self.entity_root / "aesthetics")
        problems = self.problems()
        self.assertTrue(
            any("共享实体目录" in problem and "aesthetics" in problem for problem in problems),
            problems,
        )


class AppConfigLike:
    """把测试配置暴露成 SkillRegistry 需要的三个属性，避免为验收再造一套 schema_version=2 配置。"""

    def __init__(self, path: Path):
        self._raw = json.loads(path.read_text(encoding="utf-8"))
        self.project_dir = Path(self._raw["project_dir"])
        self.agents = []
        self.database_path = Path("/nonexistent")

    @property
    def skills(self):
        return self._raw["skills"]

    @property
    def skills_root(self):
        return Path(self._raw["skills"]["root_dir"]).resolve()

    @property
    def skills_index(self):
        return Path(self._raw["skills"]["index_path"]).resolve()


class BuildReleaseGateTest(unittest.TestCase):
    """构包门槛：缺省核验替身正式位置（两态），显式参数只用于 staging 验收。"""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(CANDIDATE / "scripts"))
        import build_release  # noqa: E402

        cls.build_release = build_release
        # 临时构包夹具：独立 skills 根 + 替身正式声明，默认门槛不再依赖真实共享目录的现场状态。
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = _physical_temp(cls.tmp.name)
        cls.entity_root = cls.base / "shared" / "skills"
        cls.entity_root.mkdir(parents=True)
        shutil.copytree(SKILLS_ROOT / "aesthetics", cls.entity_root / "aesthetics")
        cls.empty_formal_root = cls.base / "formal-empty" / "skills"
        cls.empty_formal_root.mkdir(parents=True)
        cls.fixture_skills = _skills_fixture(cls.base, "skills")
        cls.index_wrapper = _write_wrapper(cls.base, "index_formal_wrapper.py", INDEX_FORMAL_WRAPPER)
        indexer = _script("create_skill_index")
        _write_index(cls.fixture_skills, indexer, cls.entity_root, cls.entity_root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_verify(self, argv, formal_root):
        """经测试夹具包装器执行索引核验：正式声明指向注入根，不触碰真实共享目录。"""
        return subprocess.run(
            [sys.executable, str(self.index_wrapper), str(CANDIDATE), str(formal_root)] + argv,
            cwd=str(CANDIDATE), capture_output=True, text=True,
        )

    def test_default_gate_fails_while_shared_entity_is_missing(self):
        completed = self.run_verify(
            ["--root", str(self.fixture_skills), "--verify"], self.empty_formal_root
        )
        self.assertNotEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("aesthetics", completed.stderr)

    def test_default_gate_passes_with_installed_entity(self):
        completed = self.run_verify(
            ["--root", str(self.fixture_skills), "--verify"], self.entity_root
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("verified=", completed.stdout)

    def test_staging_gate_passes_with_byte_identical_copy(self):
        completed = self.run_verify(
            ["--root", str(self.fixture_skills), "--verify", "--entity-root", str(self.entity_root)],
            self.entity_root,
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_mutated_staging_copy_fails_the_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            copied = _physical_temp(temporary) / "skills"
            shutil.copytree(SKILLS_ROOT / "aesthetics", copied / "aesthetics")
            (copied / "aesthetics" / "SKILL.md").write_text("篡改\n", encoding="utf-8")
            completed = self.run_verify(
                ["--root", str(self.fixture_skills), "--verify", "--entity-root", str(copied)],
                self.entity_root,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("aesthetics", completed.stderr)


if __name__ == "__main__":
    unittest.main()
