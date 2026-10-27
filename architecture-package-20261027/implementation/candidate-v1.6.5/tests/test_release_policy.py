"""第2步乙发布分档定向测试。

只使用临时受控夹具（tempfile 下自建最小真实树），不连接线上、不读取
运行数据库或凭据、不执行 apply。清单由本文件内的独立实现生成，避免用
被测模块自己的重算逻辑“自证”。全部断言触达 classify_release 的真实
分类分支。
"""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from xiaoba_agentos.release_policy import classify_release

WORKSPACE = Path(__file__).resolve().parents[1]
RULES_PATH = WORKSPACE / "config" / "release_policy.json"

STORE_TEMPLATE = '''"""minimal store fixture."""
DATABASE_SCHEMA_VERSION = 7


class Store:
    def initialize(self, conn):
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        for name, definition in (
            ("execution_snapshot_json", "TEXT"),
            ("idempotency_key", "TEXT"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        conn.execute("PRAGMA user_version=7")
'''


MARKS_TEMPLATE = {
    "schema_version": 1,
    "description": "fixture marks",
    "baselines": {
        "B09": {
            "name": "配置一致",
            "status": "guarded",
            "tests": ["test_guard.py::test_b09_config_consistency"],
        },
        "B19": {
            "name": "非答案不算成功",
            "status": "guarded",
            "tests": ["test_guard.py::test_b19_non_answer"],
        },
        "B24": {
            "name": "无自动测试",
            "status": "no_automated_test",
            "tests": [],
        },
    },
}

GUARD_TEST_TEMPLATE = '''import unittest


class GuardTest(unittest.TestCase):
    def test_b09_config_consistency(self):
        self.assertTrue(True)

    def test_b19_non_answer(self):
        self.assertTrue(True)
'''

PLAIN_TEST_TEMPLATE = '''import unittest


class PlainTest(unittest.TestCase):
    def test_plain_thing(self):
        self.assertTrue(True)
'''


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, obj) -> None:
    _write(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _source_manifest_payload(root: Path) -> dict:
    """独立复刻 create_manifest 纳入规则（与被测模块的实现相互独立）。"""
    included = (".agents", "bin", "config", "docs", "knowledge", "launchd",
                "macos", "scripts", "skills", "src", "tests")
    root_files = ("README.md", "pyproject.toml", ".gitignore", "CAPABILITY.json")
    entries = {}
    for directory in included:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.name != ".DS_Store":
                entries[path.relative_to(root).as_posix()] = _sha256_file(path)
    for name in root_files:
        path = root / name
        if path.is_file():
            entries[name] = _sha256_file(path)
    return {"algorithm": "sha256", "files": entries}


class SourceSide:
    """源码布局夹具：真实文件 + 本地重算的 SOURCE_MANIFEST.json。"""

    def __init__(self, root: Path):
        self.root = root
        _write(root / ".gitignore", ".runtime/\n")
        _write(root / "README.md", "# fixture agentos\n")
        _write(root / "CAPABILITY.json", json.dumps({"schema_version": 1}) + "\n")
        _write(root / "pyproject.toml", '[project]\nname = "fixture"\nversion = "1.5.5"\n')
        _write(root / "src/xiaoba_agentos/__init__.py", '"""fixture."""\n')
        _write(root / "src/xiaoba_agentos/store.py", STORE_TEMPLATE)
        _write(root / "scripts/agentos_version.py", '"""fixture."""\nVERSION = "1.5.5"\n')
        _write(root / "config/agentos.json", json.dumps({"schema_version": 2}) + "\n")
        _write(root / "skills/index.json", json.dumps({"schema_version": 1}) + "\n")
        _write(root / "docs/guide.md", "# guide\n")
        _write(root / "artifacts/judge.json", '{"baseline": "fixture"}\n')
        _write(root / "tests/guardrail_marks.json",
               json.dumps(MARKS_TEMPLATE, ensure_ascii=False, indent=2) + "\n")
        _write(root / "tests/test_guard.py", GUARD_TEST_TEMPLATE)
        _write(root / "tests/test_plain.py", PLAIN_TEST_TEMPLATE)
        self.rebuild_manifest()

    def rebuild_manifest(self) -> None:
        _write_json(self.root / "SOURCE_MANIFEST.json",
                    _source_manifest_payload(self.root))

    def write(self, rel: str, text: str) -> None:
        _write(self.root / rel, text)

    def write_json(self, rel: str, obj) -> None:
        _write_json(self.root / rel, obj)

    def unlink(self, rel: str) -> None:
        (self.root / rel).unlink()


class PolicyTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def classify(self, baseline, candidate, **kwargs):
        return classify_release(baseline, candidate,
                                rules_path=str(RULES_PATH), **kwargs)

    def source_pair(self):
        baseline = SourceSide(self.tmp / "baseline")
        candidate = SourceSide(self.tmp / "candidate")
        return baseline, candidate


    def assertChange(self, result, path, change_type=None, tier=None):
        matches = [c for c in result["changes"] if c["path"] == path]
        self.assertTrue(matches, f"no change entry for {path}: {result['changes']}")
        entry = matches[0]
        if change_type is not None:
            self.assertEqual(entry["change_type"], change_type)
        if tier is not None:
            self.assertEqual(entry["tier"], tier, entry["reasons"])
        return entry


class PatchAndRegularTierTest(PolicyTestCase):
    def test_pure_docs_and_unmarked_tests_are_patch(self):
        baseline, candidate = self.source_pair()
        candidate.write("docs/extra.md", "# extra doc\n")
        candidate.write("tests/test_plain.py", PLAIN_TEST_TEMPLATE + "\n# touched\n")
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "patch")
        self.assertEqual(result["effective_tier"], "patch")
        self.assertChange(result, "docs/extra.md", "added", "patch")
        self.assertChange(result, "tests/test_plain.py", "modified", "patch")

    def test_runtime_file_mixed_in_is_regular(self):
        baseline, candidate = self.source_pair()
        candidate.write("docs/extra.md", "# extra doc\n")
        candidate.write("src/xiaoba_agentos/core.py", "VALUE = 1\n")
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "regular")
        self.assertChange(result, "src/xiaoba_agentos/core.py", "added", "regular")
        self.assertChange(result, "docs/extra.md", "added", "patch")

    def test_identical_trees_have_no_changes(self):
        baseline, candidate = self.source_pair()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["calculated_tier"], "patch")


class ProtectedTestTierTest(PolicyTestCase):
    def test_protected_test_modification_is_regular(self):
        baseline, candidate = self.source_pair()
        candidate.write("tests/test_guard.py",
                        GUARD_TEST_TEMPLATE + "\n# extra assertion comment\n")
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "regular")
        entry = self.assertChange(result, "tests/test_guard.py", "modified", "regular")
        self.assertTrue(any("受保护底线测试文件" in r for r in entry["reasons"]))

    def test_protected_test_deletion_with_mark_removed_is_regular(self):
        baseline, candidate = self.source_pair()
        marks = json.loads(json.dumps(MARKS_TEMPLATE))
        for entry in marks["baselines"].values():
            entry["tests"] = [t for t in entry["tests"]
                              if not t.startswith("test_guard.py::")]
        candidate.write_json("tests/guardrail_marks.json", marks)
        candidate.unlink("tests/test_guard.py")
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "regular")
        entry = self.assertChange(result, "tests/test_guard.py", "deleted", "regular")
        self.assertTrue(any("受保护底线测试文件" in r for r in entry["reasons"]))
        marks_entry = self.assertChange(result, "tests/guardrail_marks.json",
                                        "modified", "regular")
        self.assertTrue(any("移除底线标记" in r for r in marks_entry["reasons"]))

    def test_protected_test_deletion_without_mark_update_fails_closed(self):
        baseline, candidate = self.source_pair()
        candidate.unlink("tests/test_guard.py")
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("dangling" in p for p in result["problems"]), result["problems"])
        self.assertIsNone(result["calculated_tier"])

    def test_moved_protected_test_is_regular(self):
        baseline, candidate = self.source_pair()
        marks = json.loads(json.dumps(MARKS_TEMPLATE))
        for entry in marks["baselines"].values():
            entry["tests"] = [t.replace("test_guard.py::", "test_guard_moved.py::")
                              if t.startswith("test_guard.py::") else t
                              for t in entry["tests"]]
        candidate.write_json("tests/guardrail_marks.json", marks)
        candidate.unlink("tests/test_guard.py")
        candidate.write("tests/test_guard_moved.py", GUARD_TEST_TEMPLATE)
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "regular")
        self.assertChange(result, "tests/test_guard.py", "deleted", "regular")
        self.assertChange(result, "tests/test_guard_moved.py", "added", "regular")

    def test_remove_mark_then_modify_test_cannot_downgrade(self):
        baseline, candidate = self.source_pair()
        marks = json.loads(json.dumps(MARKS_TEMPLATE))
        marks["baselines"]["B19"]["tests"] = []
        candidate.write_json("tests/guardrail_marks.json", marks)
        candidate.write("tests/test_guard.py", GUARD_TEST_TEMPLATE + "\n# weakened\n")
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "regular")
        entry = self.assertChange(result, "tests/test_guard.py", "modified", "regular")
        self.assertTrue(any("受保护底线测试文件" in r for r in entry["reasons"]))
        marks_entry = self.assertChange(result, "tests/guardrail_marks.json",
                                        "modified", "regular")
        self.assertTrue(any("移除底线标记" in r for r in marks_entry["reasons"]))

    def test_candidate_new_mark_is_at_least_regular(self):
        baseline, candidate = self.source_pair()
        marks = json.loads(json.dumps(MARKS_TEMPLATE))
        marks["baselines"]["B09"]["tests"].append("test_plain.py::test_plain_thing")
        candidate.write_json("tests/guardrail_marks.json", marks)
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "regular")
        entry = self.assertChange(result, "tests/guardrail_marks.json", "modified", "regular")
        self.assertTrue(any("新增底线标记" in r for r in entry["reasons"]))

    def test_marks_list_change_alone_is_regular(self):
        baseline, candidate = self.source_pair()
        marks = json.loads(json.dumps(MARKS_TEMPLATE))
        marks["baselines"]["B09"]["name"] = "配置一致（改名）"
        candidate.write_json("tests/guardrail_marks.json", marks)
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "regular")

    def test_policy_rules_change_is_regular_and_flagged(self):
        baseline, candidate = self.source_pair()
        rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        rules["path_rules"].append({"id": "extra", "paths": ["docs/zzz.md"],
                                    "tier": "patch", "reason": "fixture"})
        candidate.write_json("config/release_policy.json", rules)
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "regular")
        self.assertTrue(any("candidate_rules_differ" in p for p in result["problems"]))


class MigrationTierTest(PolicyTestCase):
    def test_database_schema_version_change_is_migration(self):
        baseline, candidate = self.source_pair()
        candidate.write("src/xiaoba_agentos/store.py",
                        STORE_TEMPLATE.replace("DATABASE_SCHEMA_VERSION = 7",
                                               "DATABASE_SCHEMA_VERSION = 8"))
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "migration")
        entry = self.assertChange(result, "src/xiaoba_agentos/store.py",
                                  "modified", "migration")
        self.assertTrue(any("数据库结构版本变化" in r for r in entry["reasons"]))
        self.assertEqual(
            result["fingerprints"]["migration_inputs"]["database_schema_version"],
            {"source": "src/xiaoba_agentos/store.py", "baseline": 7, "candidate": 8})


    def test_format_contract_change_is_migration(self):
        baseline, candidate = self.source_pair()
        candidate.write_json("skills/index.json", {"schema_version": 2})
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "migration")
        entry = self.assertChange(result, "skills/index.json", "modified", "migration")
        self.assertTrue(any("契约声明版本变化" in r for r in entry["reasons"]))

    def test_deleted_contract_source_is_migration_with_gap(self):
        baseline, candidate = self.source_pair()
        candidate.unlink("skills/index.json")
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "migration")
        self.assertTrue(result["gaps"], "删除契约源必须写入缺口")

    def test_new_standalone_tool_is_regular_not_migration(self):
        baseline, candidate = self.source_pair()
        candidate.write("scripts/new_standalone_tool.py",
                        "#!/usr/bin/env python3\nprint('tool')\n")
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "regular")
        self.assertEqual(result["gaps"], [])
        entry = self.assertChange(result, "scripts/new_standalone_tool.py",
                                  "added", "regular")
        self.assertFalse(any("migration" in r for r in entry["reasons"]))


    def test_source_layout_out_of_scope_path_is_flagged_not_compared(self):
        baseline, candidate = self.source_pair()
        candidate.write("tools/helper.py", "VALUE = 1\n")
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertTrue(any("out_of_declared_scope" in p for p in result["problems"]),
                        result["problems"])
        self.assertFalse(any(c["path"].startswith("tools/") for c in result["changes"]))

    def test_removed_top_level_directory_is_migration_with_gap(self):
        baseline, candidate = self.source_pair()
        for rel in sorted(p.relative_to(candidate.root).as_posix()
                          for p in (candidate.root / "docs").rglob("*") if p.is_file()):
            candidate.unlink(rel)
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "migration")
        self.assertTrue(any("结构段" in g for g in result["gaps"]))


class TestSupportFilesTest(PolicyTestCase):
    """夹具和被别的测试共用的代码不是补丁：改它们能绕开带标记的测试文件本身。"""

    def pair(self):
        baseline, candidate = self.source_pair()
        for side in (baseline, candidate):
            side.write("tests/shared_helpers.py", "VALUE = 1\n")
            side.write("tests/fixtures/sample.json", "{}\n")
            side.write("tests/test_base_case.py", "import unittest\nclass Base(unittest.TestCase):\n    pass\n")
            side.write("tests/test_user.py", "from test_base_case import Base\nimport shared_helpers\n")
            side.rebuild_manifest()
        return baseline, candidate

    def test_fixtures_helpers_and_imported_test_modules_are_regular(self):
        for path, text in (("tests/shared_helpers.py", "VALUE = 2\n"), ("tests/fixtures/sample.json", "[]\n"),
                           ("tests/test_base_case.py", "import unittest\nclass Base(unittest.TestCase):\n    x = 1\n")):
            with self.subTest(path=path):
                self.setUp()
                baseline, candidate = self.pair()
                candidate.write(path, text)
                candidate.rebuild_manifest()
                result = self.classify(baseline.root, candidate.root)
                self.assertChange(result, path, tier="regular")
                self.assertEqual(result["calculated_tier"], "regular")

    def test_new_fixture_file_is_regular_and_standalone_unmarked_test_stays_patch(self):
        baseline, candidate = self.pair()
        candidate.write("tests/fixtures/new.json", "{}\n")
        candidate.rebuild_manifest()
        self.assertChange(self.classify(baseline.root, candidate.root), "tests/fixtures/new.json", tier="regular")
        self.setUp()
        baseline, candidate = self.pair()
        candidate.write("tests/test_plain.py", PLAIN_TEST_TEMPLATE + "# note\n")
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertChange(result, "tests/test_plain.py", tier="patch")
        self.assertEqual(result["calculated_tier"], "patch")

    def test_dropping_the_import_in_the_candidate_does_not_unprotect_the_module(self):
        baseline, candidate = self.pair()
        candidate.write("tests/test_user.py", "import shared_helpers\n")
        candidate.write("tests/test_base_case.py", "import unittest\n")
        candidate.rebuild_manifest()
        self.assertChange(self.classify(baseline.root, candidate.root), "tests/test_base_case.py", tier="regular")


class ReleaseBookkeepingTest(PolicyTestCase):
    """每版必变的发布簿记不计档，否则真实的新版本永远到不了补丁档。"""

    def pair(self):
        baseline, candidate = self.source_pair()
        for side in (baseline, candidate):
            side.write_json("config/agentos.json", {"schema_version": 2, "release": {"expected_version": "v1.5.5"}})
            side.write("skills/core/SKILL.md", "# core\n适用版本 v1.5.5\n")
            side.write_json("skills/index.json", {"schema_version": 1, "software_version": "1.5.5", "entries": ["a"]})
            side.write_json("docs/DEPLOYMENT.json", {"schema_version": 1, "from_source_manifest_sha256": "0" * 64, "steps": []})
            side.write_json("docs/HISTORY_VERSIONS.json", {"schema_version": 1, "records": [{"version": "v1.5.5"}]})
            side.rebuild_manifest()
        return baseline, candidate

    def bump(self, baseline, candidate):
        candidate.write("pyproject.toml", '[project]\nname = "fixture"\nversion = "1.5.6"\n')
        candidate.write_json("config/agentos.json", {"schema_version": 2, "release": {"expected_version": "v1.5.6"}})
        candidate.write("skills/core/SKILL.md", "# core\n适用版本 v1.5.6\n")
        candidate.write_json("skills/index.json", {"schema_version": 1, "software_version": "1.5.6", "entries": ["b"]})
        candidate.write_json("docs/DEPLOYMENT.json", {
            "schema_version": 1, "steps": [],
            "from_source_manifest_sha256": _sha256_file(baseline.root / "SOURCE_MANIFEST.json")})
        candidate.write_json("docs/HISTORY_VERSIONS.json", {
            "schema_version": 1, "records": [{"version": "v1.5.5"}, {"version": "v1.5.6"}]})

    def tier(self, baseline, candidate):
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        return result

    def test_doc_only_release_with_its_version_bookkeeping_is_patch(self):
        baseline, candidate = self.pair()
        self.bump(baseline, candidate)
        candidate.write("docs/guide.md", "# guide\n补一句\n")
        result = self.tier(baseline, candidate)
        self.assertEqual(result["calculated_tier"], "patch", [(c["path"], c["tier"]) for c in result["changes"]])
        self.assertEqual(len(result["changes"]), 8)

    def test_real_change_next_to_the_version_stamp_is_not_bookkeeping(self):
        cases = {
            "config/agentos.json": lambda c: c.write_json("config/agentos.json", {
                "schema_version": 2, "release": {"expected_version": "v1.5.6"}, "worker": {"minimum_successful_agents": 1}}),
            "skills/core/SKILL.md": lambda c: c.write("skills/core/SKILL.md", "# core\n适用版本 v1.5.6\n新增一条指令\n"),
            "docs/HISTORY_VERSIONS.json": lambda c: c.write_json("docs/HISTORY_VERSIONS.json", {
                "schema_version": 1, "records": [{"version": "v1.5.5", "status": "rewritten"}, {"version": "v1.5.6"}]}),
            "docs/DEPLOYMENT.json": lambda c: c.write_json("docs/DEPLOYMENT.json", {
                "schema_version": 1, "steps": [], "from_source_manifest_sha256": "1" * 64}),
        }
        for path, change in cases.items():
            with self.subTest(path=path):
                self.setUp()
                baseline, candidate = self.pair()
                self.bump(baseline, candidate)
                change(candidate)
                result = self.tier(baseline, candidate)
                self.assertChange(result, path, tier="regular")
                self.assertEqual(result["calculated_tier"], "regular")

    def test_bookkeeping_never_hides_a_migration_or_a_protected_test(self):
        baseline, candidate = self.pair()
        self.bump(baseline, candidate)
        candidate.write_json("skills/index.json", {"schema_version": 2, "software_version": "1.5.6", "entries": ["b"]})
        candidate.write_json("docs/DEPLOYMENT.json", {
            "schema_version": 1, "steps": [{"id": "0001"}],
            "from_source_manifest_sha256": _sha256_file(baseline.root / "SOURCE_MANIFEST.json")})
        result = self.tier(baseline, candidate)
        self.assertChange(result, "skills/index.json", tier="migration")
        self.assertChange(result, "docs/DEPLOYMENT.json", tier="migration")
        self.setUp()
        baseline, candidate = self.pair()
        for side in (baseline, candidate):
            side.write("tests/test_guard.py", GUARD_TEST_TEMPLATE + "# 对应 v1.5.5\n")
            side.rebuild_manifest()
        self.bump(baseline, candidate)
        candidate.write("tests/test_guard.py", GUARD_TEST_TEMPLATE + "# 对应 v1.5.6\n")
        self.assertChange(self.tier(baseline, candidate), "tests/test_guard.py", tier="regular")

    def test_version_text_edited_without_a_declared_version_bump_is_regular(self):
        baseline, candidate = self.pair()
        candidate.write_json("config/agentos.json", {"schema_version": 2, "release": {"expected_version": "v1.5.6"}})
        self.assertChange(self.tier(baseline, candidate), "config/agentos.json", tier="regular")


class HumanRaiseTest(PolicyTestCase):
    def _patch_pair(self):
        baseline, candidate = self.source_pair()
        candidate.write("docs/extra.md", "# extra doc\n")
        candidate.rebuild_manifest()
        return baseline, candidate

    def test_structured_human_raise_is_applied_and_recorded(self):
        baseline, candidate = self._patch_pair()
        result = self.classify(baseline.root, candidate.root, human_raise={
            "authorized_by": "user",
            "basis": "用户 2026-10-12 明确要求按 regular 送审",
            "target_tier": "regular",
        })
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "patch")
        self.assertEqual(result["effective_tier"], "regular")
        self.assertTrue(result["human_raise"]["applied"])
        self.assertEqual(result["human_raise"]["basis"],
                         "用户 2026-10-12 明确要求按 regular 送审")
        self.assertEqual(result["human_raise"]["raised_from"], "patch")

    def test_plain_string_human_raise_is_rejected(self):
        baseline, candidate = self._patch_pair()
        result = self.classify(baseline.root, candidate.root,
                               human_raise="用户说升档")
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "patch")
        self.assertEqual(result["effective_tier"], "patch")
        self.assertFalse(result["human_raise"]["applied"])
        self.assertTrue(any("human_raise_rejected" in p for p in result["problems"]))

    def test_downgrade_request_is_refused(self):
        baseline, candidate = self.source_pair()
        candidate.write("src/xiaoba_agentos/core.py", "VALUE = 1\n")
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root, human_raise={
            "authorized_by": "user", "basis": "请求降为 patch", "target_tier": "patch",
        })
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "regular")
        self.assertEqual(result["effective_tier"], "regular")
        self.assertTrue(any("降档" in p for p in result["problems"]))

    def test_raise_cannot_lower_migration(self):
        baseline, candidate = self.source_pair()
        candidate.write_json("skills/index.json", {"schema_version": 2})
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root, human_raise={
            "authorized_by": "user", "basis": "请求按 regular", "target_tier": "regular",
        })
        self.assertEqual(result["calculated_tier"], "migration")
        self.assertEqual(result["effective_tier"], "migration")
        self.assertTrue(any("降档" in p for p in result["problems"]))

    def test_human_raise_without_basis_is_rejected(self):
        baseline, candidate = self._patch_pair()
        result = self.classify(baseline.root, candidate.root, human_raise={
            "authorized_by": "user", "target_tier": "regular",
        })
        self.assertEqual(result["effective_tier"], "patch")
        self.assertFalse(result["human_raise"]["applied"])


class FailClosedTest(PolicyTestCase):
    def test_missing_source_manifest_fails_closed(self):
        baseline, candidate = self.source_pair()
        candidate.unlink("SOURCE_MANIFEST.json")
        result = self.classify(baseline.root, candidate.root)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["calculated_tier"])
        self.assertEqual(result["changes"], [])

    def test_missing_guardrail_marks_fails_closed(self):
        baseline, candidate = self.source_pair()
        candidate.unlink("tests/guardrail_marks.json")
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("guardrail_marks" in p for p in result["problems"]))

    def test_tampered_file_after_manifest_fails_closed(self):
        baseline, candidate = self.source_pair()
        candidate.write("docs/guide.md", "# guide tampered\n")
        # 不重建清单：清单与真实文件不一致必须失败关闭。
        result = self.classify(baseline.root, candidate.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("manifest_mismatch" in p for p in result["problems"]))

    def test_manifest_path_escape_fails_closed(self):
        baseline, candidate = self.source_pair()
        doc = json.loads((candidate.root / "SOURCE_MANIFEST.json").read_text(encoding="utf-8"))
        doc["files"]["../escape.py"] = "0" * 64
        candidate.write_json("SOURCE_MANIFEST.json", doc)
        result = self.classify(baseline.root, candidate.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("path_escape" in p for p in result["problems"]))

    def test_corrupt_manifest_json_fails_closed(self):
        baseline, candidate = self.source_pair()
        candidate.write("SOURCE_MANIFEST.json", "{not json")
        result = self.classify(baseline.root, candidate.root)
        self.assertFalse(result["ok"])

    def test_corrupt_marks_json_fails_closed(self):
        baseline, candidate = self.source_pair()
        candidate.write("tests/guardrail_marks.json", "{broken")
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertFalse(result["ok"])

    def test_marks_dangling_reference_fails_closed(self):
        baseline, candidate = self.source_pair()
        marks = json.loads(json.dumps(MARKS_TEMPLATE))
        marks["baselines"]["B09"]["tests"].append("test_missing.py::test_absent")
        candidate.write_json("tests/guardrail_marks.json", marks)
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("dangling" in p for p in result["problems"]))

    def test_symlink_in_tree_fails_closed(self):
        baseline, candidate = self.source_pair()
        (candidate.root / "docs").mkdir(parents=True, exist_ok=True)
        os.symlink(str(candidate.root / "docs/guide.md"),
                   str(candidate.root / "docs/link.md"))
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("symlink" in p for p in result["problems"]))

    def test_bad_rules_path_fails_closed(self):
        baseline, candidate = self.source_pair()
        bad = self.tmp / "bad_rules.json"
        bad.write_text("{oops", encoding="utf-8")
        result = classify_release(baseline.root, candidate.root, rules_path=str(bad))
        self.assertFalse(result["ok"])

    def test_relative_root_fails_closed(self):
        baseline, candidate = self.source_pair()
        result = self.classify("relative/path", candidate.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("not_absolute" in p for p in result["problems"]))

    def test_unknown_layout_fails_closed(self):
        baseline, candidate = self.source_pair()
        other = self.tmp / "plain"
        other.mkdir()
        (other / "readme.txt").write_text("x", encoding="utf-8")
        result = self.classify(baseline.root, other)
        self.assertFalse(result["ok"])
        self.assertTrue(any("layout" in p for p in result["problems"]))

    def test_identical_roots_fails_closed(self):
        baseline, _ = self.source_pair()
        result = self.classify(baseline.root, baseline.root)
        self.assertFalse(result["ok"])


class FingerprintTest(PolicyTestCase):
    def test_byte_change_invalidates_previous_evidence(self):
        baseline, candidate = self.source_pair()
        candidate.write("docs/extra.md", "# extra doc\n")
        candidate.rebuild_manifest()
        first = self.classify(baseline.root, candidate.root)
        self.assertTrue(first["ok"])
        first_fp = first["fingerprints"]["policy_fingerprint"]

        candidate.write("docs/extra.md", "# extra doc changed\n")
        candidate.rebuild_manifest()
        second = self.classify(baseline.root, candidate.root)
        self.assertTrue(second["ok"])
        self.assertNotEqual(first_fp, second["fingerprints"]["policy_fingerprint"])
        entry = self.assertChange(second, "docs/extra.md", "added")
        self.assertIsNone(entry["old_sha256"])
        self.assertIsNotNone(entry["new_sha256"])

    def test_fingerprints_bind_rules_marks_and_migration_inputs(self):
        baseline, candidate = self.source_pair()
        result = self.classify(baseline.root, candidate.root)
        fp = result["fingerprints"]
        for key in ("rules_sha256", "policy_fingerprint"):
            self.assertIn(key, fp)
        self.assertIn("baseline", fp)
        self.assertIn("candidate", fp)
        self.assertIn("guardrail_marks", fp)
        self.assertIn("migration_inputs", fp)
        self.assertEqual(fp["migration_inputs"]["database_schema_version"]["baseline"], 7)
        self.assertEqual(fp["migration_inputs"]["database_schema_version"]["candidate"], 7)
        self.assertEqual(len(fp["guardrail_marks"]["protected_test_files"]), 1)
        self.assertEqual(fp["guardrail_marks"]["protected_test_files"],
                         ["tests/test_guard.py"])
        self.assertEqual(fp["guardrail_marks"]["baseline_sha256"],
                         fp["guardrail_marks"]["candidate_sha256"])

    def test_rules_change_changes_fingerprint(self):
        baseline, candidate = self.source_pair()
        first = self.classify(baseline.root, candidate.root)
        rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        rules["description"] = "fixture variant"
        variant = self.tmp / "rules_variant.json"
        variant.write_text(json.dumps(rules), encoding="utf-8")
        second = classify_release(baseline.root, candidate.root, rules_path=str(variant))
        self.assertNotEqual(first["fingerprints"]["policy_fingerprint"],
                            second["fingerprints"]["policy_fingerprint"])


class ResultShapeTest(PolicyTestCase):
    def test_result_shape_and_change_fields(self):
        baseline, candidate = self.source_pair()
        candidate.write("docs/extra.md", "# extra doc\n")
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        for key in ("schema_version", "ok", "calculated_tier", "effective_tier",
                    "changes", "reasons", "fingerprints", "problems"):
            self.assertIn(key, result)
        self.assertEqual(result["schema_version"], 1)
        for entry in result["changes"]:
            for key in ("path", "change_type", "old_sha256", "new_sha256",
                        "tier", "reasons"):
                self.assertIn(key, entry)
            self.assertIn(entry["change_type"], ("added", "modified", "deleted"))
            self.assertIn(entry["tier"], ("patch", "regular", "migration"))

    def test_version_bump_only_is_patch(self):
        baseline, candidate = self.source_pair()
        candidate.write("pyproject.toml",
                        '[project]\nname = "fixture"\nversion = "1.5.6"\n')
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "patch")
        self.assertChange(result, "pyproject.toml", "modified", "patch")

    def test_pyproject_code_change_is_regular(self):
        baseline, candidate = self.source_pair()
        candidate.write("pyproject.toml",
                        '[project]\nname = "fixture"\nversion = "1.5.6"\n'
                        'dependencies = ["requests"]\n')
        candidate.rebuild_manifest()
        result = self.classify(baseline.root, candidate.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "regular")
        self.assertChange(result, "pyproject.toml", "modified", "regular")


class BuiltinFloorTest(PolicyTestCase):
    """修1：固定底线不可被候选自带规则或外部规则路径降低。"""

    def _lowered_rules(self) -> dict:
        """把实现自身与运行源码的路径规则改成 patch（主力复现的绕过手法）。"""
        rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        for rule in rules["path_rules"]:
            if rule["id"] in ("policy-self", "runtime-source"):
                rule["tier"] = "patch"
                rule.pop("conditional", None)
                rule.pop("tier_if_condition_met", None)
                rule.pop("tier_if_condition_failed", None)
        return rules

    def test_candidate_self_carried_rules_cannot_lower_floors(self):
        baseline, candidate = self.source_pair()
        original = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        baseline.write_json("config/release_policy.json", original)
        baseline.rebuild_manifest()
        candidate.write_json("config/release_policy.json", self._lowered_rules())
        candidate.write("src/xiaoba_agentos/core.py", '"""new runtime module."""\n')
        candidate.rebuild_manifest()
        candidate_rules = candidate.root / "config" / "release_policy.json"
        result = classify_release(baseline.root, candidate.root,
                              rules_path=str(candidate_rules))
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "regular")
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["gaps"], [])
        rules_entry = self.assertChange(result, "config/release_policy.json",
                                       "modified", "regular")
        self.assertTrue(any("固定底线" in r for r in rules_entry["reasons"]),
                        rules_entry["reasons"])
        core_entry = self.assertChange(result, "src/xiaoba_agentos/core.py",
                                      "added", "regular")
        self.assertTrue(any("固定底线" in r for r in core_entry["reasons"]),
                        core_entry["reasons"])


    def test_broad_patch_prefix_rule_cannot_lower_floors(self):
        baseline, candidate = self.source_pair()
        rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        rules["path_rules"].insert(0, {"id": "catch-all", "prefixes": [""],
                                       "tier": "patch", "reason": "fixture 宽泛兜底"})
        candidate.write_json("config/release_policy.json", rules)
        candidate.write("src/xiaoba_agentos/core.py", '"""new runtime module."""\n')
        candidate.rebuild_manifest()
        candidate_rules = candidate.root / "config" / "release_policy.json"
        result = classify_release(baseline.root, candidate.root,
                              rules_path=str(candidate_rules))
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "regular")
        self.assertChange(result, "src/xiaoba_agentos/core.py", "added", "regular")

    def test_external_rules_path_cannot_lower_floors(self):
        baseline, candidate = self.source_pair()
        external = self.tmp / "external_rules.json"
        external.write_text(json.dumps(self._lowered_rules(), ensure_ascii=False),
                            encoding="utf-8")
        candidate.write_json("config/release_policy.json",
                             json.loads(RULES_PATH.read_text(encoding="utf-8")))
        candidate.write("src/xiaoba_agentos/core.py", '"""new runtime module."""\n')
        candidate.rebuild_manifest()
        result = classify_release(baseline.root, candidate.root,
                                  rules_path=str(external))
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["calculated_tier"], "regular")
        self.assertChange(result, "src/xiaoba_agentos/core.py", "added", "regular")
        self.assertTrue(any("candidate_rules_differ_from_applied_rules" in p
                            for p in result["problems"]), result["problems"])

    def test_rules_cannot_repoint_guardrail_marks_away(self):
        baseline, candidate = self.source_pair()
        rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        rules["guardrail_marks"]["path"] = "tests/other_marks.json"
        baseline.write_json("config/release_policy.json",
                            json.loads(RULES_PATH.read_text(encoding="utf-8")))
        baseline.write_json("tests/other_marks.json", MARKS_TEMPLATE)
        baseline.rebuild_manifest()
        repointed = json.loads(json.dumps(MARKS_TEMPLATE))
        repointed["baselines"]["B30"] = {"name": "重指后的新标记",
                                        "status": "no_automated_test", "tests": []}
        candidate.write_json("config/release_policy.json", rules)
        candidate.write_json("tests/other_marks.json", repointed)
        candidate.write("tests/test_guard.py", GUARD_TEST_TEMPLATE + "\n# weakened\n")
        candidate.rebuild_manifest()
        candidate_rules = candidate.root / "config" / "release_policy.json"
        result = classify_release(baseline.root, candidate.root,
                              rules_path=str(candidate_rules))
        self.assertTrue(result["ok"], result["problems"])
        entry = self.assertChange(result, "tests/test_guard.py", "modified", "regular")
        self.assertTrue(any("受保护底线测试文件" in r for r in entry["reasons"]),
                        entry["reasons"])
        marks_entry = self.assertChange(result, "tests/other_marks.json",
                                       "modified", "regular")
        self.assertTrue(any("重指" in r for r in marks_entry["reasons"]),
                        marks_entry["reasons"])

    def test_rules_repointing_marks_to_missing_file_fails_closed(self):
        baseline, candidate = self.source_pair()
        rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        rules["guardrail_marks"]["path"] = "tests/absent_marks.json"
        candidate.write_json("config/release_policy.json", rules)
        candidate.rebuild_manifest()
        candidate_rules = candidate.root / "config" / "release_policy.json"
        result = classify_release(baseline.root, candidate.root,
                              rules_path=str(candidate_rules))
        self.assertFalse(result["ok"])
        self.assertIsNone(result["calculated_tier"])


if __name__ == "__main__":
    unittest.main()

