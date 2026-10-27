from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "inventory_unregistered.py"
SPEC = importlib.util.spec_from_file_location("inventory_unregistered", SCRIPT)
inventory_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory_module)


class InventoryUnregisteredTest(unittest.TestCase):
    @staticmethod
    def snapshot(root: Path) -> dict[str, bytes | None]:
        return {
            str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")
        }

    def _setup_base_environment(self, base_dir: Path) -> tuple[Path, Path, Path, Path]:
        root = base_dir / "私人助理"
        (root / "AGENTS.md").parent.mkdir(parents=True, exist_ok=True)
        (root / "AGENTS.md").write_text("# AGENTS\n", encoding="utf-8")
        registry_dir = root / "系统" / "小八工作体系"
        registry_dir.mkdir(parents=True, exist_ok=True)
        (registry_dir / "registry.json").write_text(json.dumps({"modules": []}), encoding="utf-8")
        brain = root / "第二大脑" / "20_能力与技能"
        brain.mkdir(parents=True, exist_ok=True)
        index = base_dir / "index.json"
        index.write_text(json.dumps({"entries": []}), encoding="utf-8")
        codex = base_dir / "Codex"
        codex.mkdir(parents=True, exist_ok=True)
        return root, codex, index, registry_dir

    def test_reports_missing_skill_registration_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "私人助理"
            skill = root / "skills" / "new-skill"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# new-skill\n", encoding="utf-8")
            (root / "AGENTS.md").write_text("skills/new-skill/SKILL.md\n", encoding="utf-8")
            brain = root / "第二大脑" / "20_能力与技能"
            brain.mkdir(parents=True)
            (brain / "new-skill.md").write_text(f"source_path: {skill / 'SKILL.md'}\n", encoding="utf-8")
            registry = root / "系统" / "小八工作体系"
            registry.mkdir(parents=True)
            (registry / "registry.json").write_text(
                json.dumps({"modules": [{"manifest": "系统/小八工作体系/modules/new.json"}]}),
                encoding="utf-8",
            )
            (registry / "modules").mkdir()
            (registry / "modules" / "new.json").write_text(
                json.dumps({"implementation_paths": ["${ROOT}/skills/new-skill/SKILL.md"]}),
                encoding="utf-8",
            )
            index = Path(directory) / "index.json"
            index.write_text(json.dumps({"entries": []}), encoding="utf-8")

            before = self.snapshot(Path(directory))
            real_open = Path.open

            def read_only_open(path, mode="r", *args, **kwargs):
                if any(flag in mode for flag in ("w", "a", "x", "+")):
                    raise AssertionError("inventory opened a file for writing")
                return real_open(path, mode, *args, **kwargs)

            with ExitStack() as stack:
                for name in ("write_text", "write_bytes", "mkdir", "unlink", "rename",
                             "replace", "touch", "rmdir"):
                    stack.enter_context(patch.object(
                        Path, name, side_effect=AssertionError(f"inventory called {name}")
                    ))
                stack.enter_context(patch.object(Path, "open", new=read_only_open))
                report = inventory_module.inventory(root, Path(directory) / "Codex", [], index)
            self.assertEqual(before, self.snapshot(Path(directory)))
            self.assertTrue(report["read_only"])
            self.assertEqual(1, report["scanned"])
            self.assertEqual(1, len(report["items"]))
            item = report["items"][0]
            self.assertEqual("shared_skill", item["kind"])
            self.assertEqual(["skill_index"], item["missing"])
            self.assertTrue(item["evidence"]["registry"])
            self.assertEqual("confirmed_unregistered", item["category"])
            self.assertEqual("确定漏登记", item["category_display"])

            (registry / "registry.json").write_text(
                json.dumps({"modules": [{"manifest": "../配置/secret.json"}]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unsafe registry manifest path"):
                inventory_module.inventory(root, Path(directory) / "Codex", [], index)

    def test_decided_excluded_skill_is_not_reported_as_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, codex, index, registry = self._setup_base_environment(base)

            # Create minimax skill
            skill = root / "skills" / "minimax-h3-video-director"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# minimax-h3-video-director\n", encoding="utf-8")

            # Create exclusions config
            exclusions_file = base / "exclusions.json"
            exclusions_file.write_text(
                json.dumps({
                    "schema_version": 1,
                    "exclusions": [
                        {
                            "id": "minimax-h3-video-director",
                            "name": "minimax-h3-video-director",
                            "target_path": "skills/minimax-h3-video-director",
                            "kind": "shared_skill",
                            "scope": "non_release_standalone_skill",
                            "scope_description": "明确不随 AgentOS 发布的现有决定；作为独立实体技能由私人助理直接维护，不在 AgentOS 发版清单与哈希绑定内，并不等同于全体系不可用。",
                            "decision_source": "AGENTS.md 第 54 行与 2026-10-08 裁决",
                            "reason": "MiniMax H3 视听导演为特定视频管线独立技能，由工作体系直接维护实体文件夹，明确不作为 AgentOS 核心随版不可变发布包分发。",
                        }
                    ],
                }),
                encoding="utf-8",
            )

            report = inventory_module.inventory(root, codex, [], index, exclusions_file)
            self.assertEqual(1, report["scanned"])
            self.assertEqual(0, report["summary"]["confirmed_unregistered"])
            self.assertEqual(1, report["summary"]["decided_excluded"])

            item = report["items"][0]
            self.assertEqual("decided_excluded", item["category"])
            self.assertEqual("已决定不接入", item["category_display"])
            self.assertEqual([], item["missing"])
            self.assertIn("不随 AgentOS 发布", item["scope"])
            self.assertIn("并不等同于全体系不可用", item["scope"])
            self.assertIn("特定视频管线独立技能", item["reason"])

    def test_pure_doc_directory_is_classified_as_manual_review_not_confirmed_unregistered(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, codex, index, registry = self._setup_base_environment(base)

            # Create Playbook as a pure document directory
            playbook = root / "Playbook"
            (playbook / "偏好").mkdir(parents=True)
            (playbook / "偏好" / "README.md").write_text("# 偏好\n", encoding="utf-8")
            (playbook / "踩坑").mkdir(parents=True)
            (playbook / "踩坑" / "README.md").write_text("# 踩坑\n", encoding="utf-8")

            # Reference it in AGENTS.md
            (root / "AGENTS.md").write_text("Playbook/偏好/README.md\nPlaybook/踩坑/README.md\n", encoding="utf-8")

            report = inventory_module.inventory(root, codex, [], index)
            self.assertEqual(1, report["scanned"])
            self.assertEqual(0, report["summary"]["confirmed_unregistered"])
            self.assertEqual(1, report["summary"]["manual_review"])

            item = report["items"][0]
            self.assertEqual("manual_review", item["category"])
            self.assertEqual("待人工判断", item["category_display"])
            self.assertEqual("top_level_doc", item["kind"])
            self.assertEqual([], item["missing"])
            self.assertIn("纯文档目录", item["reason"])

    def test_exact_contract_matching_prevents_substring_false_positives(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, codex, index, registry = self._setup_base_environment(base)

            # Create a tool named 'tool-a'
            tool_a = root / "工具" / "tool-a"
            tool_a.mkdir(parents=True)
            (tool_a / "script.py").write_text("print('a')\n", encoding="utf-8")

            # Registry mentions 'tool-a-extended', which previously would substring match 'tool-a'
            (registry / "registry.json").write_text(
                json.dumps({"modules": [{"manifest": "系统/小八工作体系/modules/ext.json"}]}),
                encoding="utf-8",
            )
            (registry / "modules").mkdir(parents=True)
            (registry / "modules" / "ext.json").write_text(
                json.dumps({"implementation_paths": ["${ROOT}/工具/tool-a-extended/script.py"]}),
                encoding="utf-8",
            )

            report = inventory_module.inventory(root, codex, [], index)
            item = report["items"][0]
            # Since tool-a != tool-a-extended, registry evidence MUST be False!
            self.assertFalse(item["evidence"]["registry"])
            self.assertEqual("confirmed_unregistered", item["category"])

    def test_codex_output_traversal_depth_is_strictly_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, codex, index, registry = self._setup_base_environment(base)

            # Valid bounded depth: Codex / 2026-10-01 / session / outputs (depth 3 from codex)
            day = codex / "2026-10-01"
            valid_out = day / "valid_session" / "outputs"
            valid_out.mkdir(parents=True)
            (valid_out / "res.txt").write_text("ok\n", encoding="utf-8")

            # Deep nested output: Codex / 2026-10-01 / deep / sub / nested / outputs (depth 5 from codex)
            deep_out = day / "deep" / "sub" / "nested" / "outputs"
            deep_out.mkdir(parents=True)
            (deep_out / "deep.txt").write_text("too deep\n", encoding="utf-8")

            report = inventory_module.inventory(root, codex, [], index)
            # Only valid bounded depth is captured
            self.assertEqual(1, report["scanned"])
            item = report["items"][0]
            self.assertEqual("codex_output", item["kind"])
            self.assertEqual("manual_review", item["category"])
            self.assertEqual("待人工判断", item["category_display"])
            self.assertEqual(str(valid_out), item["path"])
            self.assertIn("日期会话输出目录", item["reason"])

    def test_taste_notes_are_described_as_long_term_maintenance_not_finished_product(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, codex, index, registry = self._setup_base_environment(base)

            # Create taste / aesthetics experience note
            exp_dir = root / "第二大脑" / "70_复利规则" / "经验"
            exp_dir.mkdir(parents=True)
            taste_note = exp_dir / "小八审美提升资料入口与首批探索方向-bba2acc3.md"
            taste_note.write_text(
                "---\nstatus: active\nlast_verified: 2026-10-09\n---\n# 小八审美提升资料入口\n",
                encoding="utf-8",
            )

            report = inventory_module.inventory(root, codex, [], index)
            self.assertEqual(1, report["scanned"])
            item = report["items"][0]
            self.assertEqual("asset_experience", item["kind"])
            self.assertEqual("manual_review", item["category"])
            self.assertEqual("待人工判断", item["category_display"])
            self.assertIn("长期维护探索内容", item["reason"])
            self.assertIn("非已完成待接入成品", item["reason"])

    def test_exclusion_scope_is_strict_and_does_not_leak_to_tools_or_agent_skills(self):
        # 1. Internal scope: shared_skill vs tool with the same id
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, codex, index, registry = self._setup_base_environment(base)

            # Target skill to be excluded
            skill = root / "skills" / "minimax-h3-video-director"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# skill\n", encoding="utf-8")

            # Same-name tool (must NOT be excluded)
            tool = root / "工具" / "minimax-h3-video-director"
            tool.mkdir(parents=True)
            (tool / "run.py").write_text("print('tool')\n", encoding="utf-8")

            exclusions_file = base / "exclusions.json"
            exclusions_file.write_text(
                json.dumps({
                    "schema_version": 1,
                    "exclusions": [
                        {
                            "id": "minimax-h3-video-director",
                            "name": "minimax-h3-video-director",
                            "target_path": "skills/minimax-h3-video-director",
                            "kind": "shared_skill",
                            "scope": "non_release_standalone_skill",
                            "decision_source": "AGENTS.md 第 54 行与 v1.5.2 发布规划",
                            "reason": "MiniMax H3 视听导演为特定视频管线独立技能，由工作体系直接维护实体文件夹，明确不作为 AgentOS 核心随版不可变发布包分发。",
                        }
                    ],
                }),
                encoding="utf-8",
            )

            report = inventory_module.inventory(root, codex, [], index, exclusions_file)
            items_by_kind = {item["kind"]: item for item in report["items"]}

            # shared_skill is excluded
            self.assertIn("shared_skill", items_by_kind)
            self.assertEqual("decided_excluded", items_by_kind["shared_skill"]["category"])

            # tool is NOT excluded, it is confirmed_unregistered
            self.assertIn("tool", items_by_kind)
            self.assertEqual("confirmed_unregistered", items_by_kind["tool"]["category"])
            self.assertEqual("确定漏登记", items_by_kind["tool"]["category_display"])

        # 2. External agent skill with the same id (must NOT be excluded)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, codex, index, registry = self._setup_base_environment(base)

            agent_skills_dir = base / "external_agent_skills"
            agent_skill = agent_skills_dir / "minimax-h3-video-director"
            agent_skill.mkdir(parents=True)
            (agent_skill / "SKILL.md").write_text("# external\n", encoding="utf-8")

            exclusions_file = base / "exclusions.json"
            exclusions_file.write_text(
                json.dumps({
                    "schema_version": 1,
                    "exclusions": [
                        {
                            "id": "minimax-h3-video-director",
                            "name": "minimax-h3-video-director",
                            "target_path": "skills/minimax-h3-video-director",
                            "kind": "shared_skill",
                            "scope": "non_release_standalone_skill",
                            "decision_source": "AGENTS.md 第 54 行与 v1.5.2 发布规划",
                            "reason": "MiniMax H3 视听导演为特定视频管线独立技能，由工作体系直接维护实体文件夹，明确不作为 AgentOS 核心随版不可变发布包分发。",
                        }
                    ],
                }),
                encoding="utf-8",
            )

            report = inventory_module.inventory(root, codex, [agent_skills_dir], index, exclusions_file)
            items_by_kind = {item["kind"]: item for item in report["items"]}

            # agent_skill is NOT excluded, it is manual_review (external agent skill)
            self.assertIn("agent_skill", items_by_kind)
            self.assertEqual("manual_review", items_by_kind["agent_skill"]["category"])
            self.assertEqual("待人工判断", items_by_kind["agent_skill"]["category_display"])

    def test_path_matches_strictly_rejects_broad_ancestor_and_same_name_diff_path(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "私人助理"
            tool = root / "工具" / "my-tool"
            tool.mkdir(parents=True)
            entry_file = tool / "main.py"
            entry_file.write_text("print(1)\n", encoding="utf-8")

            # 1. Broad ancestor (e.g. root) must return False
            self.assertFalse(inventory_module._path_matches(tool, {root}, {"my-tool"}))
            self.assertFalse(inventory_module._path_matches(tool, {root / "工具"}, set()))

            # 2. Same name in different path (e.g. skills/my-tool) must return False
            other_path = root / "skills" / "my-tool"
            self.assertFalse(inventory_module._path_matches(tool, {other_path}, {"my-tool"}))

            # 3. Exact component path or declared entry file inside component returns True
            self.assertTrue(inventory_module._path_matches(tool, {tool.resolve()}, set()))
            self.assertTrue(inventory_module._path_matches(tool, {entry_file.resolve()}, set()))

    def test_pure_doc_dir_exceeding_max_depth_is_classified_as_top_level_deep_manual_review(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, codex, index, registry = self._setup_base_environment(base)

            # Create a doc directory exceeding max depth 3 (depth 4)
            deep_doc = root / "DeepDocs"
            level4 = deep_doc / "l1" / "l2" / "l3" / "l4"
            level4.mkdir(parents=True)
            (level4 / "note.md").write_text("# deep note\n", encoding="utf-8")

            report = inventory_module.inventory(root, codex, [], index)
            self.assertEqual(1, report["scanned"])
            item = report["items"][0]
            self.assertEqual("top_level_deep", item["kind"])
            self.assertEqual("manual_review", item["category"])
            self.assertEqual("待人工判断", item["category_display"])
            self.assertIn("超过扫描契约限制深度", item["reason"])

    def test_unsafe_traversal_and_symlinks_are_strictly_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, codex, index, registry = self._setup_base_environment(base)

            # 1. Traversal in exclusions file target_path must raise ValueError
            bad_excl = base / "bad_excl.json"
            bad_excl.write_text(
                json.dumps({
                    "exclusions": [
                        {"id": "test", "kind": "tool", "target_path": "工具/../../secret"}
                    ]
                }),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unsafe target_path in exclusions"):
                inventory_module.inventory(root, codex, [], index, bad_excl)

            # 2. Traversal in skill index must raise ValueError
            bad_index = base / "bad_index.json"
            bad_index.write_text(
                json.dumps({
                    "entries": [
                        {"id": "bad", "entity_source_path": "../out/secret.md"}
                    ]
                }),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unsafe path traversal in skill index"):
                inventory_module.inventory(root, codex, [], bad_index)

            # 3. Out-of-bounds symlink in manifest implementation_paths must be rejected by contract
            outside = base / "outside_dir"
            outside.mkdir()
            (outside / "external.py").write_text("# ext\n", encoding="utf-8")
            symlink_dir = root / "tools_link"
            symlink_dir.symlink_to(outside)

            tool = root / "工具" / "sym_tool"
            tool.mkdir(parents=True)
            (tool / "run.py").write_text("print(1)\n", encoding="utf-8")

            (registry / "registry.json").write_text(
                json.dumps({"modules": [{"manifest": "系统/小八工作体系/modules/sym.json"}]}),
                encoding="utf-8",
            )
            (registry / "modules").mkdir(parents=True, exist_ok=True)
            (registry / "modules" / "sym.json").write_text(
                json.dumps({"implementation_paths": ["${ROOT}/tools_link/external.py"]}),
                encoding="utf-8",
            )
            # Directly verify _validate_safe_path rejects out of bounds symlink
            with self.assertRaisesRegex(ValueError, "symlink out of bounds"):
                inventory_module._validate_safe_path(root, "tools_link/external.py")

            # And inventory correctly rejects it from declared_paths, meaning sym_tool lacks registry evidence
            report = inventory_module.inventory(root, codex, [], index)
            item = report["items"][0]
            self.assertFalse(item["evidence"]["registry"])
            self.assertEqual("confirmed_unregistered", item["category"])

    def test_symlink_equivalence_matrix_and_external_control_group(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root, codex, index, registry = self._setup_base_environment(base)

            # 外部真实存在的文件（合法外部物理声明控制组）
            outside_real = base / "outside_real"
            outside_real.mkdir()
            outside_skill = outside_real / "SKILL.md"
            outside_skill.write_text("# real outside skill\n", encoding="utf-8")

            # 越界软链夹具：内部 skills/escape 指向外部目录
            outside_escaped = base / "outside_escaped"
            outside_escaped.mkdir()
            escaped_target_file = outside_escaped / "SKILL.md"
            escaped_target_file.write_text("# escaped target\n", encoding="utf-8")

            escape_link = root / "skills" / "escape"
            escape_link.parent.mkdir(parents=True, exist_ok=True)
            escape_link.symlink_to(outside_escaped)

            rel_escape = "skills/escape/SKILL.md"
            abs_escape = str(escape_link / "SKILL.md")

            # 1. 验证 _validate_safe_path 对越界软链的绝对与相对形式均等价抛出 ValueError
            with self.assertRaisesRegex(ValueError, "symlink out of bounds"):
                inventory_module._validate_safe_path(root, rel_escape, allow_within_root_only=True)
            with self.assertRaisesRegex(ValueError, "symlink out of bounds"):
                inventory_module._validate_safe_path(root, abs_escape, allow_within_root_only=False)

            # 合法外部控制组：真实物理路径（非越界软链）在 allow_within_root_only=False 下正常采纳
            resolved_outside = inventory_module._validate_safe_path(
                root, str(outside_skill), allow_within_root_only=False
            )
            self.assertEqual(outside_skill.resolve(), resolved_outside)

            # 2. Registry 声明来源矩阵：相对与绝对形式的越界软链均被拒绝且不进入 declared_paths
            manifest_dir = registry / "modules"
            manifest_dir.mkdir(parents=True, exist_ok=True)
            probe_manifest = manifest_dir / "probe.json"
            (registry / "registry.json").write_text(
                json.dumps({"modules": [{"manifest": "系统/小八工作体系/modules/probe.json"}]}),
                encoding="utf-8",
            )

            # 相对形式：不进入 declared_paths
            probe_manifest.write_text(
                json.dumps({"implementation_paths": ["${ROOT}/" + rel_escape]}),
                encoding="utf-8",
            )
            reg_paths_rel, _ = inventory_module._collect_registry_contracts(root)
            self.assertNotIn(escaped_target_file.resolve(), reg_paths_rel)

            # 绝对形式：同样不进入 declared_paths
            probe_manifest.write_text(
                json.dumps({"implementation_paths": [abs_escape]}),
                encoding="utf-8",
            )
            reg_paths_abs, _ = inventory_module._collect_registry_contracts(root)
            self.assertNotIn(escaped_target_file.resolve(), reg_paths_abs)

            # 控制组：合法外部物理绝对路径进入 declared_paths
            probe_manifest.write_text(
                json.dumps({"implementation_paths": [str(outside_skill)]}),
                encoding="utf-8",
            )
            reg_paths_ctrl, _ = inventory_module._collect_registry_contracts(root)
            self.assertIn(outside_skill.resolve(), reg_paths_ctrl)

            # 3. Skill index 声明来源矩阵：相对与绝对越界软链均不进入 sources
            # 绝对形式
            index.write_text(
                json.dumps({"entries": [{"id": "escape", "entity_source_path": abs_escape}]}),
                encoding="utf-8",
            )
            idx_paths_abs, _ = inventory_module._collect_skill_index_contracts(index, root)
            self.assertNotIn(escaped_target_file.resolve(), idx_paths_abs)

            # 相对形式
            index.write_text(
                json.dumps({"entries": [{"id": "escape", "entity_source_path": rel_escape}]}),
                encoding="utf-8",
            )
            idx_paths_rel, _ = inventory_module._collect_skill_index_contracts(index, root)
            self.assertNotIn(escaped_target_file.resolve(), idx_paths_rel)

            # 控制组：合法外部物理路径进入 sources
            index.write_text(
                json.dumps({"entries": [{"id": "real", "entity_source_path": str(outside_skill)}]}),
                encoding="utf-8",
            )
            idx_paths_ctrl, _ = inventory_module._collect_skill_index_contracts(index, root)
            self.assertIn(outside_skill.resolve(), idx_paths_ctrl)

    def test_unknown_or_symlink_directory_classified_as_manual_review(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root, codex, index, registry = self._setup_base_environment(base)

            # 1. 顶层未知/含软链目录：DocsWithLink 含文档与外部软链，无代码
            folder = root / "DocsWithLink"
            folder.mkdir(parents=True)
            (folder / "README.md").write_text("# Documentation\n", encoding="utf-8")
            outside = base / "outside"
            outside.mkdir()
            (folder / "external_link").symlink_to(outside)

            # 2. 纯文档对比组：PureDocs 只有 README.md
            pure_folder = root / "PureDocs"
            pure_folder.mkdir(parents=True)
            (pure_folder / "README.md").write_text("# Pure Doc\n", encoding="utf-8")

            # 3. 真实功能组件对比组：RealComponent 含 run.py
            comp_folder = root / "RealComponent"
            comp_folder.mkdir(parents=True)
            (comp_folder / "run.py").write_text("print('running')\n", encoding="utf-8")

            report = inventory_module.inventory(root, codex, [], index)
            items_by_path = {item["path"]: item for item in report["items"]}

            # DocsWithLink 必须归入 top_level_unknown 与待人工判断
            item_unknown = items_by_path[str(folder)]
            self.assertEqual("top_level_unknown", item_unknown["kind"])
            self.assertEqual("manual_review", item_unknown["category"])
            self.assertEqual("待人工判断", item_unknown["category_display"])
            self.assertIn("受软链", item_unknown["reason"])
            self.assertEqual("unknown_or_symlink_directory", item_unknown["assessment"])

            # PureDocs 归入 top_level_doc 与待人工判断
            item_doc = items_by_path[str(pure_folder)]
            self.assertEqual("top_level_doc", item_doc["kind"])
            self.assertEqual("manual_review", item_doc["category"])
            self.assertEqual("pure_doc_directory", item_doc["assessment"])

            # RealComponent 归入 top_level 与确定漏登记（无契约）
            item_comp = items_by_path[str(comp_folder)]
            self.assertEqual("top_level", item_comp["kind"])
            self.assertEqual("confirmed_unregistered", item_comp["category"])
            self.assertEqual("确定漏登记", item_comp["category_display"])

    def test_canonical_path_identity_and_rejection_of_fictitious_prefix_mapping(self):
        # 1. 负例验证：虚构前缀不能被映射到另一个真实存在的文件，必须忠实保留 OS canonical 身份
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            base = Path(directory).resolve()
            root, codex, index, registry = self._setup_base_environment(base)

            skill_dir = root / "skills" / "genuine"
            skill_dir.mkdir(parents=True)
            real_file = skill_dir / "SKILL.md"
            real_file.write_text("# genuine skill\n", encoding="utf-8")

            # 构造输入：前置 /private 的虚构路径
            fictitious_input = "/private" + str(real_file)
            self.assertFalse(Path(fictitious_input).exists())
            expected_canonical = Path(fictitious_input).resolve()

            # allow_within_root_only=False 时：必须返回该输入在 OS 中的规范对象，不能篡改成已存在的 real_file
            returned = inventory_module._validate_safe_path(root, fictitious_input, allow_within_root_only=False)
            self.assertEqual(expected_canonical, returned)
            self.assertNotEqual(real_file.resolve(), returned)
            self.assertFalse(returned.exists())

            # 验证契约匹配：真实组件不能被虚构路径冒充登记
            self.assertFalse(inventory_module._path_matches(real_file, {returned}))

            # allow_within_root_only=True 时：虚构路径未进入 root，必须拒绝抛错
            with self.assertRaisesRegex(ValueError, "unsafe absolute path outside root"):
                inventory_module._validate_safe_path(root, fictitious_input, allow_within_root_only=True)

        # 2. 真实平台别名与真实外部物理声明控制组
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, codex, index, registry = self._setup_base_environment(base)
            file_under_root = root / "skills" / "control.md"
            file_under_root.parent.mkdir(parents=True, exist_ok=True)
            file_under_root.write_text("# control\n", encoding="utf-8")

            # 内部真实文件：正常解析并返回其 canonical 路径
            resolved_ctrl = inventory_module._validate_safe_path(root, str(file_under_root))
            self.assertEqual(file_under_root.resolve(), resolved_ctrl)

            # 外部真实存在文件：在 allow_within_root_only=False 下正常采纳
            outside_file = base / "outside.md"
            outside_file.write_text("# outside\n", encoding="utf-8")
            resolved_outside = inventory_module._validate_safe_path(root, str(outside_file), allow_within_root_only=False)
            self.assertEqual(outside_file.resolve(), resolved_outside)

            # 若运行在 macOS 且临时目录以 /var 开头，验证 /var 与 /private/var 真实平台别名解析完全一致
            if str(file_under_root).startswith("/var"):
                alt_var = "/private" + str(file_under_root)
                self.assertEqual(resolved_ctrl, inventory_module._validate_safe_path(root, alt_var))

    def test_same_name_independent_entity_and_true_alias_dedup_control_group(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root, codex, index, registry = self._setup_base_environment(base)

            # 1. 共享技能实体
            shared = root / "skills" / "minimax-h3-video-director"
            shared.mkdir(parents=True)
            (shared / "SKILL.md").write_text("# shared entity\n", encoding="utf-8")

            # 2. 同名外部独立私有实体（不同物理目录，不同内容）
            agent_skills_dir = base / "agent-skills"
            external_private = agent_skills_dir / "minimax-h3-video-director"
            external_private.mkdir(parents=True)
            (external_private / "SKILL.md").write_text("# private implementation\n", encoding="utf-8")

            # 3. 真实同对象去重控制组：shared_path 是指向 external_same_obj 的符号链接
            external_same_obj = agent_skills_dir / "same-object-skill"
            external_same_obj.mkdir(parents=True)
            (external_same_obj / "SKILL.md").write_text("# same obj\n", encoding="utf-8")
            internal_same_obj = root / "skills" / "same-object-skill"
            internal_same_obj.symlink_to(external_same_obj)

            # 配置排除清单（只排除 shared_skill）
            exclusions_file = base / "exclusions.json"
            exclusions_file.write_text(
                json.dumps({
                    "exclusions": [
                        {
                            "id": "minimax-h3-video-director",
                            "kind": "shared_skill",
                            "target_path": "skills/minimax-h3-video-director",
                            "scope": "non_release_standalone_skill",
                            "decision_source": "AGENTS.md",
                            "reason": "仅排除指定共享技能",
                        }
                    ]
                }),
                encoding="utf-8",
            )

            report = inventory_module.inventory(root, codex, [agent_skills_dir], index, exclusions_file)
            paths_in_all = {x["path"]: x for x in report["all_records"]}

            # 验证两份独立实体均可见
            self.assertIn(str(shared), paths_in_all)
            self.assertIn(str(external_private), paths_in_all)
            self.assertEqual("decided_excluded", paths_in_all[str(shared)]["category"])
            self.assertEqual("manual_review", paths_in_all[str(external_private)]["category"])
            self.assertEqual("agent_skill", paths_in_all[str(external_private)]["kind"])

            # 验证真实同对象去重：internal_same_obj 与 external_same_obj 物理相同，外部对象不被重复扫描为 agent_skill
            self.assertNotIn(str(external_same_obj), paths_in_all)

    def test_subdirectory_and_collapsed_chain_alias_out_of_bounds_matrix(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root, codex, index, registry = self._setup_base_environment(base)

            outside = base / "outside"
            outside.mkdir()
            target_file = outside / "SKILL.md"
            target_file.write_text("# outside\n", encoding="utf-8")

            # 内部越界软链接
            escape = root / "skills" / "escape"
            escape.parent.mkdir(parents=True, exist_ok=True)
            escape.symlink_to(outside)

            # 外部子目录别名 -> assistant/skills
            alias = base / "skills-alias"
            alias.symlink_to(escape.parent)

            # 外部直接别名 -> assistant/skills/escape
            direct_alias = base / "direct-alias"
            direct_alias.symlink_to(escape)

            manifest_dir = registry / "modules"
            manifest_dir.mkdir(parents=True, exist_ok=True)
            manifest = manifest_dir / "probe.json"
            (registry / "registry.json").write_text(
                json.dumps({"modules": [{"manifest": "系统/小八工作体系/modules/probe.json"}]}),
                encoding="utf-8",
            )

            test_cases = [
                ("direct", escape / "SKILL.md"),
                ("descendant_alias", alias / "escape" / "SKILL.md"),
                ("collapsed_chain_alias", direct_alias / "SKILL.md"),
            ]

            for label, p in test_cases:
                # 1. helper 直接校验必须抛出 ValueError
                with self.assertRaises(ValueError, msg=f"Helper did not reject {label}"):
                    inventory_module._validate_safe_path(root, p, allow_within_root_only=False)

                # 2. registry 声明均不得采纳外部目标
                manifest.write_text(
                    json.dumps({"implementation_paths": [str(p)]}),
                    encoding="utf-8",
                )
                reg_paths, _ = inventory_module._collect_registry_contracts(root)
                self.assertNotIn(target_file.resolve(), reg_paths, f"Registry adopted outside for {label}")

                # 3. skill_index 声明均不得采纳外部目标
                index.write_text(
                    json.dumps({"entries": [{"id": "probe", "entity_source_path": str(p)}]}),
                    encoding="utf-8",
                )
                idx_paths, _ = inventory_module._collect_skill_index_contracts(index, root)
                self.assertNotIn(target_file.resolve(), idx_paths, f"Index adopted outside for {label}")


if __name__ == "__main__":
    unittest.main()

