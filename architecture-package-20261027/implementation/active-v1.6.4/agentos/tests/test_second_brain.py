import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from xiaoba_agentos.user_rules import USER_RULES_TEXT


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_knowlp_index.py"
SPEC = importlib.util.spec_from_file_location("build_knowlp_index", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

BRAIN_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_second_brain.py"
BRAIN_SPEC = importlib.util.spec_from_file_location("build_second_brain", BRAIN_SCRIPT)
BRAIN_MODULE = importlib.util.module_from_spec(BRAIN_SPEC)
assert BRAIN_SPEC and BRAIN_SPEC.loader
BRAIN_SPEC.loader.exec_module(BRAIN_MODULE)

# 模板门槛要求冻结身份恰好各一次，所以夹具必须与生产模板同身份，正文用测试文本。
TEMPLATE_FIXTURE = [
    {
        "path": "10_治理与裁决/故障自愈与任务恢复.md",
        "id": "recovery-policy",
        "type": "memory",
        "parent": "governance",
        "authority": True,
        "title": "故障自愈与任务恢复",
        "body": "测试恢复正文。",
        "source_path": "",
        "last_verified": "2026-09-26",
    },
]


def write_extra_nodes_template(root: Path) -> None:
    directory = root / "docs" / "second_brain"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "v152_extra_nodes.json").write_text(
        json.dumps(TEMPLATE_FIXTURE, ensure_ascii=False, indent=2), encoding="utf-8"
    )


class SecondBrainTemplateGateTest(unittest.TestCase):
    """模板身份门槛：缺项、重复、额外项、恢复节点标题或身份漂移都必须明确失败。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _merge(self, template):
        nodes = json.loads(json.dumps(BRAIN_MODULE.STATIC_NODES))
        directory = self.root / "docs" / "second_brain"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "v152_extra_nodes.json").write_text(
            json.dumps(template, ensure_ascii=False), encoding="utf-8"
        )
        BRAIN_MODULE._merge_extra_nodes(nodes, self.root)
        return nodes

    def test_the_frozen_node_is_merged_and_content_branches_are_not_generated(self):
        nodes = self._merge(TEMPLATE_FIXTURE)
        self.assertEqual(
            1, sum(1 for node in nodes if node["id"] == "recovery-policy")
        )
        self.assertEqual(
            "测试恢复正文。", next(node for node in nodes if node["id"] == "recovery-policy")["body"]
        )
        self.assertEqual(
            [], [node["id"] for node in nodes if node["id"] in ("capabilities", "knowledge", "workflows", "assets")]
        )

    def test_missing_required_nodes_are_rejected(self):
        with self.assertRaises(SystemExit):
            self._merge([])

    def test_duplicate_and_extra_nodes_are_rejected(self):
        with self.assertRaises(SystemExit):
            self._merge(TEMPLATE_FIXTURE + [TEMPLATE_FIXTURE[0]])
        extra = json.loads(json.dumps(TEMPLATE_FIXTURE))
        extra.append(
            {
                "path": "30_方法与知识/临时笔记.md",
                "id": "unplanned-leaf",
                "type": "knowledge",
                "parent": "knowledge",
                "authority": False,
                "title": "临时笔记",
                "body": "不应进入发布。",
                "source_path": "",
                "last_verified": "2026-10-10",
            }
        )
        with self.assertRaises(SystemExit):
            self._merge(extra)

    def test_recovery_title_suffix_exception_is_removed(self):
        drifted = json.loads(json.dumps(TEMPLATE_FIXTURE))
        next(item for item in drifted if item["id"] == "recovery-policy")["title"] += "（不变）"
        with self.assertRaises(SystemExit):
            self._merge(drifted)

    def test_recovery_identity_drift_is_rejected(self):
        for key, value in (
            ("path", "10_治理与裁决/别的名.md"),
            ("type", "branch"),
            ("parent", "capabilities"),
            ("authority", False),
            ("last_verified", "2026-10-10"),
        ):
            drifted = json.loads(json.dumps(TEMPLATE_FIXTURE))
            next(item for item in drifted if item["id"] == "recovery-policy")[key] = value
            with self.assertRaises(SystemExit, msg=f"key={key}"):
                self._merge(drifted)

    def test_unreadable_template_is_rejected(self):
        directory = self.root / "docs" / "second_brain"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "v152_extra_nodes.json").write_text("{not json", encoding="utf-8")
        nodes = json.loads(json.dumps(BRAIN_MODULE.STATIC_NODES))
        with self.assertRaises(SystemExit):
            BRAIN_MODULE._merge_extra_nodes(nodes, self.root)
        missing = Path(self.tmp.name) / "empty"
        missing.mkdir()
        with self.assertRaises(SystemExit):
            BRAIN_MODULE._merge_extra_nodes(json.loads(json.dumps(BRAIN_MODULE.STATIC_NODES)), missing)


class SecondBrainIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        (self.vault / "child").mkdir()
        (self.vault / "root.md").write_text(
            '---\nid: "root"\ntype: "root"\nparent: ""\nstatus: "active"\nauthority: true\n---\n\n# Root\n\n- [[child/note]]\n',
            encoding="utf-8",
        )
        (self.vault / "child" / "note.md").write_text(
            '---\nid: "note"\ntype: "memory"\nparent: "root"\nstatus: "active"\nauthority: false\n---\n\n# Note\n\nCurrent verified content.\n',
            encoding="utf-8",
        )
        (self.vault / "skills").mkdir()
        (self.vault / "knowledge").mkdir()
        (self.vault / "skills" / "index.json").write_text('{"entries": []}', encoding="utf-8")
        (self.vault / "knowledge" / "index.json").write_text('{"entries": []}', encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_tree_build_has_no_similarity_edges(self):
        built = MODULE.build(self.vault)
        graph = built["dual_graph.json"]
        self.assertEqual({}, graph["similarity"])
        self.assertEqual(["note"], graph["prerequisite"]["root"])
        self.assertEqual(2, built["deep_extraction_prep.json"]["nodes"])
        self.assertEqual(1, built["deep_extraction_prep.json"]["edges"])

    def test_unresolved_link_is_rejected(self):
        (self.vault / "root.md").write_text(
            (self.vault / "root.md").read_text(encoding="utf-8").replace("child/note", "missing"),
            encoding="utf-8",
        )
        with self.assertRaises(RuntimeError):
            MODULE.build(self.vault)

    def test_revalidation_note_is_excluded_from_graph_and_chunks(self):
        root = self.vault / "root.md"
        root.write_text(
            root.read_text(encoding="utf-8").replace("\n- [[child/note]]", ""),
            encoding="utf-8",
        )
        note = self.vault / "child" / "note.md"
        note.write_text(
            note.read_text(encoding="utf-8").replace(
                'status: "active"', 'status: "revalidation_required"'
            ),
            encoding="utf-8",
        )
        built = MODULE.build(self.vault)
        self.assertEqual(1, built["deep_extraction_prep.json"]["nodes"])
        self.assertEqual(0, built["deep_extraction_prep.json"]["edges"])
        self.assertEqual(["root.md"], [item["path"] for item in built["meta_index.json"]])

    def test_current_state_records_distinct_stable_runtime_release_and_vault_paths(self):
        write_extra_nodes_template(self.vault)
        rendered = BRAIN_MODULE.build(self.vault)
        self.assertIn("唯一跨 Agent 入口", rendered["60_当前上下文/系统当前态.md"])
        self.assertIn("运行源码", rendered["60_当前上下文/系统当前态.md"])
        self.assertIn("不可变发布内容", rendered["60_当前上下文/系统当前态.md"])
        self.assertIn("当前 Obsidian Vault", rendered["60_当前上下文/系统当前态.md"])

    def test_generated_constitution_uses_the_same_public_user_rule_as_backends(self):
        write_extra_nodes_template(self.vault)
        rendered = BRAIN_MODULE.build(self.vault)
        constitution = rendered["10_治理与裁决/运行宪法.md"]
        self.assertIn(USER_RULES_TEXT, constitution)
        self.assertIn('last_verified: "2026-10-05"', constitution)
        self.assertIn(f'last_verified: "{BRAIN_MODULE.DATE}"', rendered["10_治理与裁决/模型链路与认证.md"])
        document = BRAIN_SCRIPT.parents[1] / "docs/USER_RULES.md"
        self.assertIn(USER_RULES_TEXT, document.read_text(encoding="utf-8"))


class SecondBrainGeneratedScopeTest(unittest.TestCase):
    """发布只生成规则与发布状态；四个内容分支由根笔记链接，但不在生成结果里。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write_extra_nodes_template(self.root)

    def test_no_content_branch_note_is_generated_and_tree_is_valid(self):
        rendered = BRAIN_MODULE.build(self.root)
        self.assertEqual([], BRAIN_MODULE.validate(rendered))
        prefixes = ("20_能力与技能", "30_方法与知识", "40_工作流", "50_资产与索引")
        self.assertEqual([], [path for path in rendered if path.startswith(prefixes)])
        for branch in prefixes:
            self.assertEqual(1, rendered["00_小八AgentOS.md"].count(f"- [[{branch}]]"))

    def test_generating_a_content_branch_or_dropping_its_root_link_is_invalid(self):
        rendered = BRAIN_MODULE.build(self.root)
        with_branch = dict(rendered)
        with_branch["30_方法与知识.md"] = (
            '---\nid: "knowledge"\ntype: "branch"\nparent: "root"\nstatus: "active"\nauthority: false\n---\n\n# 方法\n'
        )
        self.assertTrue(BRAIN_MODULE.validate(with_branch))
        unlinked = dict(rendered)
        unlinked["00_小八AgentOS.md"] = unlinked["00_小八AgentOS.md"].replace("- [[40_工作流]]\n", "")
        self.assertTrue(BRAIN_MODULE.validate(unlinked))

    def test_generation_does_not_read_content_indexes(self):
        # 夹具根目录里没有 skills/ 与 knowledge/：生成不得依赖它们。
        self.assertFalse((self.root / "skills").exists())
        self.assertIn("10_治理与裁决/运行宪法.md", BRAIN_MODULE.build(self.root))


if __name__ == "__main__":
    unittest.main()
