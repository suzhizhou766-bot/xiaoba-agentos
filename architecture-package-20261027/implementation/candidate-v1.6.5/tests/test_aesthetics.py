"""aesthetics 指路技能：实体一致性、触发匹配与生成器验收（方案 A）。"""

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from xiaoba_agentos import skills as skills_module
from xiaoba_agentos.skills import (
    ENTITY_METADATA_KEYS,
    SkillRegistry,
    entity_file_for,
    entity_source_path,
)

CANDIDATE = Path(__file__).resolve().parents[1]
SKILLS_ROOT = CANDIDATE / "tests" / "fixtures" / "content" / "skills"
INDEX_PATH = SKILLS_ROOT / "index.json"
TEMPLATE_PATH = CANDIDATE / "docs" / "second_brain" / "v152_extra_nodes.json"
PUBLISHED_SKILL = SKILLS_ROOT / "aesthetics" / "SKILL.md"
STAGING_SKILL = CANDIDATE.parent / "artifacts" / "private-assistant" / "skills" / "aesthetics" / "SKILL.md"

SCRIPTS = {}


def _physical_temp(name: str) -> Path:
    """临时夹具的物理根。

    macOS 的 /private/var 与 /tmp 本身是平台软链，实体根守卫逐段拒绝任何软链段，
    所以合法 fixtures 必须建立在已知物理根上（resolve 一次，在构造时就把平台软链换成真身），
    而不是给这些平台软链开全局例外。
    """
    return Path(name).resolve()


@contextmanager
def simulated_formal_root(root: Path):
    """测试层替身：把集中声明的正式共享根指向临时物理根。

    只在本测试进程内替换 ENTITY_SHARED_ROOTS，验证“缺省即正式定位”这条规则的形态与拒绝面；
    生产代码不新增环境变量、配置键或默认回退，真实正式目录也不被写入或改动。
    """
    original = dict(skills_module.ENTITY_SHARED_ROOTS)
    skills_module.ENTITY_SHARED_ROOTS["aesthetics"] = str(root)
    try:
        yield root
    finally:
        skills_module.ENTITY_SHARED_ROOTS.clear()
        skills_module.ENTITY_SHARED_ROOTS.update(original)


@contextmanager
def working_directory(path: Path):
    """相对根的解析基准：临时切到物理夹具目录内，退出时还原。"""
    previous = Path(os.getcwd())
    os.chdir(path)
    try:
        yield path
    finally:
        os.chdir(previous)



def _script(name: str):
    if name not in SCRIPTS:
        path = CANDIDATE / "scripts" / f"{name}.py"
    scripts_dir = str(CANDIDATE / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    if name not in SCRIPTS:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        SCRIPTS[name] = module
    return SCRIPTS[name]


def _aesthetics_entry() -> dict:
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    return copy.deepcopy(next(e for e in index["entries"] if e["id"] == "aesthetics"))


class AestheticsRoutingTest(unittest.TestCase):
    """真实候选索引 + staging 实体：路径集中声明、双副本一致、触发匹配。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # macOS /var、/tmp 本身是平台软链，实体根守卫会逐段拒绝软链；合法夹具建在已知物理根上。
        self.entity_root = _physical_temp(self.tmp.name) / "shared" / "skills"
        self.entity_root.mkdir(parents=True)
        shutil.copytree(SKILLS_ROOT / "aesthetics", self.entity_root / "aesthetics")
        config = SimpleNamespace(
            skills_root=SKILLS_ROOT.resolve(),
            skills_index=INDEX_PATH.resolve(),
            skills={"max_selected": 4},
        )
        self.registry = SkillRegistry(config, entity_root=self.entity_root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_entity_paths_come_from_one_centered_declaration(self):
        self.assertEqual(
            entity_source_path("aesthetics"),
            "/Users/suzhizhou/Documents/私人助理/skills/aesthetics/SKILL.md",
        )
        self.assertEqual(
            entity_file_for("aesthetics", self.entity_root),
            self.entity_root / "aesthetics" / "SKILL.md",
        )

    def test_published_copy_and_staging_entity_are_byte_identical(self):
        self.assertEqual(
            hashlib.sha256(PUBLISHED_SKILL.read_bytes()).hexdigest(),
            hashlib.sha256(STAGING_SKILL.read_bytes()).hexdigest(),
        )

    def test_verify_accepts_byte_identical_pair(self):
        report = self.registry.verify()
        self.assertTrue(report["ok"], report["problems"])

    def test_positive_phrasings_select_callable_aesthetics(self):
        for prompt in ("审美", "aesthetics", "帮我调色", "做一个好看的网页", "这张海报排版留白不行"):
            selected = [entry for entry in self.registry.match(prompt)]
            self.assertIn("aesthetics", [entry["id"] for entry in selected], prompt)
            self.assertTrue(
                all(entry["callable"] is True for entry in selected if entry["id"] == "aesthetics"),
                prompt,
            )

    def test_verify_result_shows_which_entity_root_was_checked(self):
        # 缺省正式定位用测试层替身声明验证：真实索引条目声明的仍是正式声明路径，
        # 替身根下必须先报“实体 metadata 路径不合规”，证明缺省分支读的确实是集中声明，
        # 不依赖这台机器真实共享目录里装没装过 aesthetics。
        with tempfile.TemporaryDirectory() as substitute:
            formal_root = _physical_temp(substitute) / "skills"
            formal_root.mkdir(parents=True)
            with simulated_formal_root(formal_root):
                formal = SkillRegistry(self.registry.config).verify()
                self.assertEqual(
                    entity_source_path("aesthetics"),
                    str(formal_root / "aesthetics" / "SKILL.md"),
                )
        self.assertEqual("formal", formal["entity_root_mode"])
        self.assertIsNone(formal["entity_root"])
        self.assertTrue(
            any("实体 metadata 路径不合规" in problem for problem in formal["problems"]),
            formal["problems"],
        )
        staging = SkillRegistry(self.registry.config, entity_root=self.entity_root).verify()
        self.assertEqual("staging", staging["entity_root_mode"])
        self.assertEqual(str(self.entity_root), staging["entity_root"])
        self.assertTrue(staging["ok"], staging["problems"])

    def test_execution_entry_points_do_not_accept_entity_root(self):
        import io
        from contextlib import redirect_stderr
        from xiaoba_agentos.cli import build_parser

        def parse(argv):
            with redirect_stderr(io.StringIO()):
                return build_parser().parse_args(argv)

        for argv in (
            ["run"],
            ["session-start"],
            ["submit", "任务"],
            ["task-resume", "t1", "--freeze-hash", "h"],
            ["bridge"],
            ["intake", "任务"],
            [
                "collaborate",
                "任务",
                "--context-file",
                str(self.entity_root / "ctx.md"),
                "--message-id",
                "msg-1",
                "--assignments-file",
                str(self.entity_root / "assign.json"),
            ],
        ):
            parse(argv)  # 执行入口命令本身合法
            with self.assertRaises(SystemExit, msg=str(argv)):
                parse(argv + ["--entity-root", str(self.entity_root)])
        for command in ("doctor", "skill-verify", "skill-list", "skill-match"):
            argv = [command, "--entity-root", str(self.entity_root)]
            if command == "skill-match":
                argv.append("帮我调色")
            parsed = parse(argv)
            self.assertEqual(str(self.entity_root), str(parsed.entity_root))
        self.assertIsNone(parse(["skill-verify"]).entity_root)
        self.assertIsNone(parse(["doctor"]).entity_root)

    def test_negative_prompts_do_not_select_aesthetics(self):
        for prompt in ("设计重试算法", "修复后台认证 API", "上班了小八助理"):
            selected = [entry["id"] for entry in self.registry.match(prompt)]
            self.assertNotIn("aesthetics", selected, prompt)


class AestheticsEntityGuardTest(unittest.TestCase):
    """单方漂移、缺失、越界、symlink、额外文件与路径篡改都必须被拒绝。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _physical_temp(self.tmp.name) / "skills"
        self.root.mkdir(parents=True)
        shutil.copytree(SKILLS_ROOT / "aesthetics", self.root / "aesthetics")
        self.entity_root = _physical_temp(self.tmp.name) / "shared" / "skills"
        self.entity_root.mkdir(parents=True)
        shutil.copytree(SKILLS_ROOT / "aesthetics", self.entity_root / "aesthetics")
        self.entry = _aesthetics_entry()
        self._write_index()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_index(self):
        (self.root / "index.json").write_text(
            json.dumps(
                {"schema_version": 1, "release": "v1.5.2-test", "entries": [self.entry]},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _problems(self) -> list:
        config = SimpleNamespace(
            skills_root=self.root.resolve(),
            skills_index=(self.root / "index.json").resolve(),
            skills={"max_selected": 4},
        )
        return SkillRegistry(config, entity_root=self.entity_root).verify()["problems"]

    def test_matching_pair_passes(self):
        self.assertEqual([], self._problems())

    def test_entity_drift_is_rejected(self):
        entity = self.entity_root / "aesthetics" / "SKILL.md"
        entity.write_text(entity.read_text(encoding="utf-8") + "\n漂移内容\n", encoding="utf-8")
        problems = self._problems()
        self.assertTrue(any("共享实体哈希不匹配" in p for p in problems), problems)
        self.assertTrue(any("共享实体与发布副本不一致" in p for p in problems), problems)

    def test_published_copy_drift_is_rejected(self):
        published = self.root / "aesthetics" / "SKILL.md"
        published.write_text(published.read_text(encoding="utf-8") + "\n漂移内容\n", encoding="utf-8")
        problems = self._problems()
        self.assertTrue(any("共享实体与发布副本不一致" in p for p in problems), problems)

    def test_symlinked_skill_directory_is_rejected(self):
        os.symlink(self.root / "aesthetics", self.root / "linked-skill", target_is_directory=True)
        self.entry["directory"] = "linked-skill"
        self._write_index()
        self.assertTrue(
            any("linked-skill" in p and "不得经过符号链接" in p for p in self._problems()), self._problems()
        )

    def test_symlinked_skill_file_is_rejected(self):
        os.symlink(self.root / "aesthetics" / "SKILL.md", self.root / "aesthetics" / "alias.md")
        self.assertTrue(
            any("含符号链接文件" in p and "alias.md" in p for p in self._problems()), self._problems()
        )

    def test_symlinked_entity_root_is_rejected(self):
        linked = _physical_temp(self.tmp.name) / "linked-entity-root"
        os.symlink(self.entity_root, linked, target_is_directory=True)
        config = SimpleNamespace(
            skills_root=self.root.resolve(),
            skills_index=(self.root / "index.json").resolve(),
            skills={"max_selected": 4},
        )
        problems = SkillRegistry(config, entity_root=linked).verify()["problems"]
        self.assertTrue(any("共享实体根不得为符号链接" in p for p in problems), problems)

    def _registry_for(self, entity_root):
        return SkillRegistry(
            SimpleNamespace(
                skills_root=self.root.resolve(),
                skills_index=(self.root / "index.json").resolve(),
                skills={"max_selected": 4},
            ),
            entity_root=entity_root,
        )

    def test_symlinked_entity_root_parent_is_rejected(self):
        """上级目录本身是软链（alias/nested 形态）：必须在 alias 段被拦，不能先 resolve 消证据。"""
        real = self.entity_root.parent
        alias = _physical_temp(self.tmp.name) / "parent-alias"
        os.symlink(real, alias, target_is_directory=True)
        problems = self._registry_for(alias / "skills").verify()["problems"]
        self.assertTrue(
            any("共享实体根不得为符号链接" in p and "parent-alias" in p for p in problems), problems
        )

    def test_intermediate_symlinked_entity_root_is_rejected(self):
        """中间多级软链（alias/l1/l2 形态）：同样在 alias 段被拦。"""
        real = _physical_temp(self.tmp.name) / "mid" / "l1" / "l2" / "skills"
        real.mkdir(parents=True)
        shutil.copytree(SKILLS_ROOT / "aesthetics", real / "aesthetics")
        alias = _physical_temp(self.tmp.name) / "mid-alias"
        os.symlink(real.parent.parent, alias, target_is_directory=True)
        problems = self._registry_for(alias / "l2" / "skills").verify()["problems"]
        self.assertTrue(
            any("共享实体根不得为符号链接" in p and "mid-alias" in p for p in problems), problems
        )

    def test_dotdot_after_symlink_segment_is_rejected(self):
        """`..` 不能消掉软链证据：alias/../alias/skills 形态仍要在 alias 段被拦。"""
        alias = _physical_temp(self.tmp.name) / "dot-alias"
        os.symlink(self.entity_root, alias, target_is_directory=True)
        written = alias / ".." / "dot-alias" / "skills"
        problems = self._registry_for(written).verify()["problems"]
        self.assertTrue(any("共享实体根不得为符号链接" in p for p in problems), problems)

    def test_relative_physical_root_passes(self):
        """相对正常根：以临时物理目录为 cwd 时，相对根也要能通过。"""
        with working_directory(self.entity_root.parent):
            problems = self._registry_for(Path("skills")).verify()["problems"]
        self.assertEqual([], problems)

    def test_relative_symlinked_root_is_rejected(self):
        alias = self.entity_root.parent / "relative-alias"
        os.symlink(self.entity_root, alias, target_is_directory=True)
        with working_directory(self.entity_root.parent):
            problems = self._registry_for(Path("relative-alias/skills")).verify()["problems"]
        self.assertTrue(
            any("共享实体根不得为符号链接" in p and "relative-alias" in p for p in problems), problems
        )

    def test_default_formal_root_with_installed_entity_passes(self):
        """缺省即正式定位：测试层替身声明指向装有合法实体的临时物理根时，不传 entity_root 也能通过。"""
        with simulated_formal_root(self.entity_root):
            _file, sha = skills_module.verified_entity("aesthetics", self.entity_root)
            self.entry["entity_source_path"] = entity_source_path("aesthetics")
            self.entry["entity_source_sha256"] = sha
            self._write_index()
            report = self._registry_for(None).verify()
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual("formal", report["entity_root_mode"])

    def test_default_formal_root_alias_segment_is_rejected(self):
        """缺省正式定位同样逐段拒绝软链：替身声明为 alias/skills 形态时必须被拦。"""
        alias = _physical_temp(self.tmp.name) / "formal-alias"
        os.symlink(self.entity_root, alias, target_is_directory=True)
        with simulated_formal_root(alias / "skills"):
            self.entry["entity_source_path"] = entity_source_path("aesthetics")
            self._write_index()
            report = self._registry_for(None).verify()
        self.assertEqual("formal", report["entity_root_mode"])
        self.assertTrue(
            any("共享实体根不得为符号链接" in p and "formal-alias" in p for p in report["problems"]),
            report["problems"],
        )

    def test_missing_entity_dir_is_rejected(self):
        shutil.rmtree(self.entity_root / "aesthetics")
        self.assertTrue(any("共享实体目录缺失" in p for p in self._problems()))

    def test_missing_entity_entry_is_rejected(self):
        (self.entity_root / "aesthetics" / "SKILL.md").unlink()
        self.assertTrue(any("共享实体入口缺失" in p for p in self._problems()))

    def test_symlinked_entity_dir_is_rejected(self):
        shutil.rmtree(self.entity_root / "aesthetics")
        os.symlink(self.root / "aesthetics", self.entity_root / "aesthetics", target_is_directory=True)
        # 原始路径先查符号链接：软链实体目录报“不得经过符号链接”，而不是先 resolve 后当成正常目录。
        self.assertTrue(
            any("共享实体目录" in p and "符号链接" in p for p in self._problems()), self._problems()
        )

    def test_symlinked_entity_entry_is_rejected(self):
        """入口软链：实体目录里的 SKILL.md 指向别处，同样要在入口检查被拦。"""
        elsewhere = self.entity_root.parent / "entry-elsewhere"
        elsewhere.mkdir()
        shutil.copytree(SKILLS_ROOT / "aesthetics", elsewhere / "aesthetics")
        (self.entity_root / "aesthetics" / "SKILL.md").unlink()
        os.symlink(elsewhere / "aesthetics" / "SKILL.md", self.entity_root / "aesthetics" / "SKILL.md")
        problems = self._problems()
        self.assertTrue(
            any("共享实体入口缺失或为符号链接：aesthetics" in p for p in problems), problems
        )

    def test_extra_entity_file_is_rejected(self):
        (self.entity_root / "aesthetics" / "extra.md").write_text("额外文件\n", encoding="utf-8")
        self.assertTrue(any("共享实体目录文件集合不合规" in p for p in self._problems()))

    def test_unknown_entity_key_is_rejected(self):
        self.entry["entity_foo"] = "额外键"
        self._write_index()
        self.assertTrue(any("实体 metadata 不合规" in p for p in self._problems()))

    def test_missing_entity_key_is_rejected(self):
        self.entry.pop("entity_source_sha256")
        self._write_index()
        self.assertTrue(any("实体 metadata 缺少 entity_source_sha256" in p for p in self._problems()))

    def test_tampered_entity_path_is_rejected(self):
        self.entry["entity_source_path"] = "/Users/suzhizhou/Documents/私人助理/skills/evil/SKILL.md"
        self._write_index()
        self.assertTrue(any("实体 metadata 路径不合规" in p for p in self._problems()))

    def test_relative_escape_path_is_rejected(self):
        self.entry["entity_source_path"] = "../evil/SKILL.md"
        self._write_index()
        self.assertTrue(any("实体 metadata 路径不合规" in p for p in self._problems()))

    def test_entity_metadata_on_undeclared_skill_is_rejected(self):
        self.entry["id"] = "assets"
        self._write_index()
        problems = self._problems()
        self.assertTrue(any("实体 metadata 只允许技能" in p or "实体 metadata 不合规" in p for p in problems), problems)


class AestheticsGeneratorTest(unittest.TestCase):
    """create_skill_index：staging 实体入参、正式路径写入与确定性。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.entity_root = _physical_temp(self.tmp.name) / "shared" / "skills"
        self.entity_root.mkdir(parents=True)
        shutil.copytree(SKILLS_ROOT / "aesthetics", self.entity_root / "aesthetics")
        self.indexer = _script("create_skill_index")

    def tearDown(self):
        self.tmp.cleanup()

    def test_published_index_is_reproducible_with_staging_entity(self):
        generated = self.indexer.build(SKILLS_ROOT, entity_root=self.entity_root)
        rendered = json.dumps(generated, ensure_ascii=False, indent=2) + "\n"
        self.assertEqual(INDEX_PATH.read_text(encoding="utf-8"), rendered)

    def test_entity_metadata_points_to_declared_shared_location(self):
        generated = self.indexer.build(SKILLS_ROOT, entity_root=self.entity_root)
        entry = next(e for e in generated["entries"] if e["id"] == "aesthetics")
        self.assertEqual(entry["entity_source_path"], entity_source_path("aesthetics"))
        self.assertEqual(
            entry["entity_source_sha256"],
            hashlib.sha256(PUBLISHED_SKILL.read_bytes()).hexdigest(),
        )

    def test_entity_drift_blocks_generation(self):
        (self.entity_root / "aesthetics" / "SKILL.md").write_text(
            "---\nname: aesthetics\ndescription: drift\n---\n\n漂移\n", encoding="utf-8"
        )
        with self.assertRaises(SystemExit):
            self.indexer.build(SKILLS_ROOT, entity_root=self.entity_root)

    def test_missing_entity_blocks_generation(self):
        shutil.rmtree(self.entity_root)
        with self.assertRaises(SystemExit):
            self.indexer.build(SKILLS_ROOT, entity_root=self.entity_root)

    def test_catalog_may_not_carry_entity_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills"
            root.mkdir()
            shutil.copytree(SKILLS_ROOT / "aesthetics", root / "aesthetics")
            catalog = json.loads((SKILLS_ROOT / "catalog.json").read_text(encoding="utf-8"))
            entry = copy.deepcopy(next(e for e in catalog["entries"] if e["id"] == "aesthetics"))
            entry["entity_source_path"] = "/tmp/evil"
            (root / "catalog.json").write_text(
                json.dumps({"schema_version": catalog["schema_version"], "entries": [entry]}, ensure_ascii=False),
                encoding="utf-8",
            )
            (root.parent / "pyproject.toml").write_text(
                "[project]\nname = \"agentos-fixture\"\nversion = \"1.5.2\"\n", encoding="utf-8"
            )
            with self.assertRaises(SystemExit):
                self.indexer.build(root, entity_root=self.entity_root)

    def test_dynamic_handbook_change_leaves_index_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills"
            shutil.copytree(SKILLS_ROOT, root)
            (root.parent / "pyproject.toml").write_text(
                "[project]\nname = \"agentos-fixture\"\nversion = \"1.5.2\"\n", encoding="utf-8"
            )
            handbook = root / "审美提升" / "审美手册.md"
            handbook.parent.mkdir(parents=True, exist_ok=True)
            handbook.write_text("每日变化的手册内容 v1\n", encoding="utf-8")
            first = json.dumps(self.indexer.build(root, entity_root=self.entity_root), ensure_ascii=False, indent=2) + "\n"
            handbook.write_text("每日变化的手册内容 v2 已更新\n", encoding="utf-8")
            second = json.dumps(self.indexer.build(root, entity_root=self.entity_root), ensure_ascii=False, indent=2) + "\n"
            self.assertEqual(first, second)
            self.assertNotIn("审美提升", first)


class AestheticsGenerationSafetyTest(unittest.TestCase):
    """生成索引与运行核验同等的实体安全：软链、越界、额外文件都不能产出“正常”索引。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = _physical_temp(self.tmp.name)
        self.root = self.base / "skills"
        self.root.mkdir()
        shutil.copytree(SKILLS_ROOT / "aesthetics", self.root / "aesthetics")
        catalog = json.loads((SKILLS_ROOT / "catalog.json").read_text(encoding="utf-8"))
        self.entry = copy.deepcopy(next(e for e in catalog["entries"] if e["id"] == "aesthetics"))
        self._write_catalog([self.entry])
        (self.base / "pyproject.toml").write_text(
            "[project]\nname = \"agentos-fixture\"\nversion = \"1.5.2\"\n", encoding="utf-8"
        )
        self.entity_root = self.base / "shared" / "skills"
        self.entity_root.mkdir(parents=True)
        shutil.copytree(SKILLS_ROOT / "aesthetics", self.entity_root / "aesthetics")
        self.indexer = _script("create_skill_index")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_catalog(self, entries):
        (self.root / "catalog.json").write_text(
            json.dumps({"schema_version": 1, "entries": entries}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_legal_staging_entity_generates_index(self):
        generated = self.indexer.build(self.root, entity_root=self.entity_root)
        entry = generated["entries"][0]
        self.assertEqual(entity_source_path("aesthetics"), entry["entity_source_path"])
        self.assertEqual(entry["entrypoint_sha256"], entry["entity_source_sha256"])

    def test_symlinked_skill_directory_blocks_generation(self):
        os.symlink(self.root / "aesthetics", self.root / "linked-skill", target_is_directory=True)
        self.entry["directory"] = "linked-skill"
        self._write_catalog([self.entry])
        with self.assertRaises(SystemExit):
            self.indexer.build(self.root, entity_root=self.entity_root)

    def test_symlinked_skill_file_blocks_generation(self):
        os.symlink(self.root / "aesthetics" / "SKILL.md", self.root / "aesthetics" / "alias.md")
        with self.assertRaises(SystemExit):
            self.indexer.build(self.root, entity_root=self.entity_root)

    def test_escaping_skill_directory_blocks_generation(self):
        outside = self.base / "outside" / "aesthetics"
        outside.parent.mkdir()
        shutil.copytree(SKILLS_ROOT / "aesthetics", outside)
        for directory in ("../outside/aesthetics", "aesthetics/../../outside/aesthetics"):
            self.entry["directory"] = directory
            self._write_catalog([self.entry])
            with self.assertRaises(SystemExit, msg=directory):
                self.indexer.build(self.root, entity_root=self.entity_root)

    def test_symlinked_entity_root_blocks_generation(self):
        linked = self.base / "linked-entity-root"
        os.symlink(self.entity_root, linked, target_is_directory=True)
        with self.assertRaises(SystemExit):
            self.indexer.build(self.root, entity_root=linked)

    def test_symlinked_entity_root_parent_blocks_generation(self):
        """上级目录本身是软链：alias/nested 形态在生成路径同样被拦（与运行路径共用拒绝逻辑）。"""
        alias = self.base / "generator-alias"
        os.symlink(self.entity_root.parent, alias, target_is_directory=True)
        with self.assertRaises(SystemExit):
            self.indexer.build(self.root, entity_root=alias / "skills")

    def test_dotdot_after_symlink_segment_blocks_generation(self):
        """`..` 不能消掉软链证据：生成路径对 alias/../alias/skills 形态同样拒绝。"""
        alias = self.base / "dot-alias"
        os.symlink(self.entity_root, alias, target_is_directory=True)
        with self.assertRaises(SystemExit):
            self.indexer.build(self.root, entity_root=alias / ".." / "dot-alias" / "skills")

    def test_relative_physical_root_generates_index(self):
        """相对正常根：以临时物理目录为 cwd 生成索引同样成立。"""
        with working_directory(self.entity_root.parent):
            generated = self.indexer.build(self.root, entity_root=Path("skills"))
        entry = generated["entries"][0]
        self.assertEqual(entity_source_path("aesthetics"), entry["entity_source_path"])
        self.assertEqual(entry["entrypoint_sha256"], entry["entity_source_sha256"])

    def test_default_formal_root_alias_segment_blocks_generation(self):
        """缺省正式定位走同一守卫：替身声明为 alias/skills 形态时生成器必须退出。"""
        alias = self.base / "formal-alias"
        os.symlink(self.entity_root, alias, target_is_directory=True)
        with simulated_formal_root(alias / "skills"):
            with self.assertRaises(SystemExit):
                self.indexer.build(self.root, entity_root=None)

    def test_default_formal_root_with_installed_entity_generates_index(self):
        """缺省正式定位：替身声明指向装有合法实体的临时物理根时，不传 entity_root 也能生成。"""
        with simulated_formal_root(self.entity_root):
            declared = entity_source_path("aesthetics")
            generated = self.indexer.build(self.root, entity_root=None)
        entry = generated["entries"][0]
        self.assertEqual(declared, entry["entity_source_path"])
        self.assertEqual(entry["entrypoint_sha256"], entry["entity_source_sha256"])

    def test_symlinked_entity_directory_blocks_generation(self):
        shutil.rmtree(self.entity_root / "aesthetics")
        elsewhere = self.base / "elsewhere" / "aesthetics"
        elsewhere.parent.mkdir()
        shutil.copytree(SKILLS_ROOT / "aesthetics", elsewhere)
        os.symlink(elsewhere, self.entity_root / "aesthetics", target_is_directory=True)
        with self.assertRaises(SystemExit):
            self.indexer.build(self.root, entity_root=self.entity_root)

    def test_extra_entity_file_blocks_generation(self):
        (self.entity_root / "aesthetics" / "extra.md").write_text("额外文件\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.indexer.build(self.root, entity_root=self.entity_root)


class AestheticsSecondBrainTest(unittest.TestCase):
    """build_second_brain：真实模板合入、单根唯一链接、实体卡片与受限覆盖。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.brain = _script("build_second_brain")
        self.real_template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))

    def tearDown(self):
        self.tmp.cleanup()

    def _make_root(self, template=None) -> Path:
        root = Path(self.tmp.name) / "root"
        (root / "skills").mkdir(parents=True)
        (root / "knowledge").mkdir(parents=True)
        shutil.copyfile(INDEX_PATH, root / "skills" / "index.json")
        (root / "knowledge" / "index.json").write_text('{"entries": []}', encoding="utf-8")
        if template is not None:
            directory = root / "docs" / "second_brain"
            directory.mkdir(parents=True)
            (directory / "v152_extra_nodes.json").write_text(
                json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        return root

    def _build(self, root: Path) -> dict:
        published = Path(self.tmp.name) / "published"
        published.mkdir(exist_ok=True)
        return self.brain.build(root, published)

    def test_real_template_merges_single_root_and_unique_links(self):
        rendered = self._build(self._make_root(template=self.real_template))
        self.assertEqual([], self.brain.validate(rendered))
        # 审美手册入口与发版流程两篇已归笔记库，发布不再生成它们及其分支。
        self.assertNotIn("30_方法与知识.md", rendered)
        self.assertNotIn("30_方法与知识/小八审美手册.md", rendered)
        self.assertNotIn("40_工作流/AgentOS发版流程.md", rendered)

    def test_recovery_policy_body_is_replaced_with_template_version(self):
        template_body = next(
            item["body"] for item in self.real_template if item["id"] == "recovery-policy"
        )
        rendered = self._build(self._make_root(template=self.real_template))
        recovery = rendered["10_治理与裁决/故障自愈与任务恢复.md"]
        self.assertIn("不能用缓存健康代替", recovery)
        self.assertIn(template_body, recovery)
        self.assertIn('last_verified: "2026-09-26"', recovery)

    def test_skill_card_points_to_shared_entity(self):
        from xiaoba_agentos import content_notes

        knowledge = Path(self.tmp.name) / "knowledge.json"
        knowledge.write_text('{"entries": []}', encoding="utf-8")
        notes = {
            note["path"]: note["text"]
            for note in content_notes.derived_notes(INDEX_PATH, Path(self.tmp.name) / "published" / "skills", knowledge)
        }
        card = notes["20_能力与技能/aesthetics.md"]
        self.assertIn(entity_source_path("aesthetics"), card)
        self.assertIn("skill-aesthetics", card)

    def test_missing_template_is_rejected(self):
        with self.assertRaises(SystemExit):
            self._build(self._make_root())

    def test_bad_field_set_is_rejected(self):
        template = copy.deepcopy(self.real_template)
        template[0]["extra"] = "不允许"
        with self.assertRaises(SystemExit):
            self._build(self._make_root(template=template))

    def test_unauthorized_update_id_is_rejected(self):
        template = copy.deepcopy(self.real_template)
        for item in template:
            if item["id"] == "recovery-policy":
                item["id"] = "constitution"
                item["parent"] = "governance"
        with self.assertRaises(SystemExit):
            self._build(self._make_root(template=template))

    def test_update_identity_change_is_rejected(self):
        template = copy.deepcopy(self.real_template)
        for item in template:
            if item["id"] == "recovery-policy":
                item["parent"] = "knowledge"
        with self.assertRaises(SystemExit):
            self._build(self._make_root(template=template))

    def test_template_cannot_add_a_note(self):
        template = copy.deepcopy(self.real_template)
        template.append({
            "path": "30_方法与知识/新笔记.md", "id": "new-leaf", "type": "knowledge", "parent": "knowledge",
            "authority": False, "title": "新笔记", "body": "不应进入发布。", "source_path": "", "last_verified": "2026-10-10",
        })
        with self.assertRaises(SystemExit):
            self._build(self._make_root(template=template))


if __name__ == "__main__":
    unittest.main()
