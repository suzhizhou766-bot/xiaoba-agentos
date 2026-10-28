import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xiaoba_agentos.adapters import AgyAdapter, BaseAdapter, DshAdapter
from xiaoba_agentos.user_rules import USER_RULES_TEXT


STRICT_PROMPT = "计算 37 × 41。只输出最终的阿拉伯数字，不要输出任何其他文字。"


class UserRulesPromptTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = SimpleNamespace(
            project_dir=self.root,
            raw={"runtime_dir": str(self.root / "runtime")},
            integrations={"agy_command":"stub-agy", "node_command":"stub-node", "dsh_command":"stub-dsh",
                          "codex_home":str(self.root / "codex-home")},
        )

    def config(self, driver):
        return SimpleNamespace(
            role="独立审查员", name=driver + "_test", timeout_seconds=1, app_config=self.app,
            type="local_cli" if driver in {"agy", "dsh"} else "cc_switch_cli",
            raw={"driver":driver, "cli_route":"fixture", "gateway_route":"fixture"},
        )

    def assert_rule_before_task(self, sent, prompt):
        self.assertEqual(1, sent.count(USER_RULES_TEXT))
        self.assertLess(sent.index("独立审查员"), sent.index(USER_RULES_TEXT))
        self.assertLess(sent.index(USER_RULES_TEXT), sent.index("任务："))
        self.assertIn(prompt, sent)

    def test_public_rule_is_independent_of_context_and_strict_output_branch(self):
        adapter = BaseAdapter(self.config("dsh"))
        for context in ("", "技能默认建议：发布前先备份。历史记忆未命中用户规则。"):
            with self.subTest(context=context):
                sent = adapter._full_prompt(STRICT_PROMPT, context)
                self.assert_rule_before_task(sent, STRICT_PROMPT)
                self.assertIn("严格遵守任务题面中的输出约束", sent)
                self.assertNotIn("请给出明确结论、依据、风险和可验证步骤", sent)

    def test_enabled_agy_and_dsh_send_rule_with_empty_context_and_strict_output(self):
        for driver, adapter_class in (("agy", AgyAdapter), ("dsh", DshAdapter)):
            payload = {"status":"SUCCESS", "response":"1517"} if driver == "agy" else {"type":"final", "text":"1517"}
            selection = {"model":"fixture-model", "provider":"fixture-provider", "desktop_config":str(self.root / "desktop.yml")}
            completed = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
            with self.subTest(driver=driver), \
                    patch("xiaoba_agentos.adapters.local_cli_route", return_value=selection), \
                    patch("xiaoba_agentos.adapters.command_environment", return_value={}), \
                    patch("xiaoba_agentos.adapters.run_cli", return_value=completed) as caller:
                result = adapter_class(self.config(driver)).run(STRICT_PROMPT, "")
            sent = caller.call_args.kwargs["input"]
            if driver == "agy":
                sent = json.loads(sent)["message"]["content"]
            self.assert_rule_before_task(sent, STRICT_PROMPT)
            self.assertIn("严格遵守任务题面中的输出约束", sent)
            self.assertNotIn("任务上下文与参考资料", sent)
            self.assertEqual("1517", result.output)

    def test_caller_skills_and_user_document_share_the_backend_rule(self):
        project = Path(__file__).resolve().parents[1]
        for relative in ("docs/USER_RULES.md", ".agents/skills/xiaoba-agentos/SKILL.md"):
            with self.subTest(path=relative):
                self.assertIn(USER_RULES_TEXT, (project / relative).read_text(encoding="utf-8"))
        # Build input lives beside the source tree; built packages use that
        # same parent/interface layout. Retain old candidate-layout support.
        interface = project / "interface/SKILL.md"
        if not interface.is_file():
            interface = project.parent / "interface/SKILL.md"
        self.assertTrue(interface.is_file(), "both caller entrypoints must be supplied")
        self.assertIn(USER_RULES_TEXT, interface.read_text(encoding="utf-8"))
        self.assertEqual((project / ".agents/skills/xiaoba-agentos/SKILL.md").read_bytes(),
                         interface.read_bytes(), "build entry must exactly match the source-bound entry")


if __name__ == "__main__":
    unittest.main()
