"""由内容索引派生的笔记（技能卡、价值文件索引）与笔记交还笔记库的门禁。"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xiaoba_agentos import cli, content_notes
from test_switch_release import Fixture, seal, switch, write

SHA_A, SHA_B, SHA_C = "a" * 64, "b" * 64, "c" * 64
BRANCH = (
    '---\nid: "capabilities"\ntype: "branch"\nparent: "root"\nstatus: "active"\nauthority: false\n---\n\n'
    "# 能力与技能\n\n- [[20_能力与技能/demo]]\n\n## 保留的扩展入口\n\n- [[20_能力与技能/手工卡]]\n"
)


def skill(name, sha, description="演示技能"):
    return {"id": name, "name": name, "category": "test", "capabilities": ["verify"], "description": description,
            "entrypoint": f"{name}/SKILL.md", "entrypoint_sha256": sha}


def value(name, sha):
    return {"id": name, "index_mode": "semantic_source", "path": f"/content/{name}.md", "sha256": sha}


class ContentNotesCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.vault = self.root / "vault"
        self.skills_index = self.root / "skills/index.json"
        self.knowledge_index = self.root / "knowledge/index.json"
        self.indexes([skill("demo", SHA_A)], [value("method", SHA_B)])
        write(self.vault / "20_能力与技能.md", BRANCH)
        for note in self.notes():
            write(self.vault / note["path"], note["text"])

    def indexes(self, skills, values):
        write(self.skills_index, json.dumps({"schema_version": 1, "entries": skills}, ensure_ascii=False))
        write(self.knowledge_index, json.dumps({"schema_version": 1, "entries": values}, ensure_ascii=False))

    def notes(self):
        return content_notes.derived_notes(self.skills_index, self.root / "skills", self.knowledge_index)

    def plan(self):
        return content_notes.plan(self.vault, self.notes())

    def read(self, relative):
        return (self.vault / relative).read_text(encoding="utf-8")


class DerivedNotesTest(ContentNotesCase):
    def test_card_source_is_the_configured_skills_root_or_the_declared_entity(self):
        card = self.read("20_能力与技能/demo.md")
        self.assertIn(f'source_path: "{self.root / "skills" / "demo" / "SKILL.md"}"', card)
        self.assertIn(f"入口哈希：`{SHA_A}`", card)
        entity = dict(skill("demo", SHA_A), entity_source_path="/shared/demo/SKILL.md")
        self.indexes([entity], [])
        self.assertIn('source_path: "/shared/demo/SKILL.md"', self.notes()[0]["text"])

    def test_vault_matching_the_indexes_needs_no_write(self):
        planned = self.plan()
        self.assertEqual((planned["ok"], planned["in_sync"], planned["writes"]), (True, True, []))

    def test_index_change_updates_the_card_and_keeps_text_appended_by_the_user(self):
        card = self.vault / "20_能力与技能/demo.md"
        card.write_text(card.read_text(encoding="utf-8") + "\n## 我的备注\n\n保留这段。\n", encoding="utf-8")
        self.indexes([skill("demo", SHA_C, "改过的说明")], [value("method", SHA_B)])
        planned = self.plan()
        self.assertEqual([item["path"] for item in planned["writes"]], ["20_能力与技能/demo.md"])
        content_notes.apply(self.vault, planned)
        updated = self.read("20_能力与技能/demo.md")
        self.assertIn(f"入口哈希：`{SHA_C}`", updated)
        self.assertIn("改过的说明", updated)
        self.assertNotIn(SHA_A, updated)
        self.assertTrue(updated.endswith("\n## 我的备注\n\n保留这段。\n"))
        self.assertTrue(self.plan()["in_sync"])

    def test_note_that_is_not_a_recognisable_card_is_never_overwritten(self):
        for text in ("# 用户自己写的同名笔记\n", self.read("20_能力与技能/demo.md").replace("入口哈希", "哈希")):
            with self.subTest(text=text[:12]):
                write(self.vault / "20_能力与技能/demo.md", text)
                self.indexes([skill("demo", SHA_C)], [value("method", SHA_B)])
                planned = self.plan()
                self.assertFalse(planned["ok"])
                self.assertIn("未覆盖", planned["problems"][0])
                with self.assertRaises(content_notes.ContentNotesError):
                    content_notes.apply(self.vault, planned)
                self.assertEqual(self.read("20_能力与技能/demo.md"), text)

    def test_new_skill_gets_a_card_and_exactly_one_branch_link_after_existing_text(self):
        self.indexes([skill("demo", SHA_A), skill("extra", SHA_C)], [value("method", SHA_B)])
        planned = self.plan()
        self.assertEqual(sorted(item["path"] for item in planned["writes"]), ["20_能力与技能.md", "20_能力与技能/extra.md"])
        content_notes.apply(self.vault, planned)
        branch = self.read("20_能力与技能.md")
        self.assertTrue(branch.startswith(BRANCH))
        self.assertEqual(branch.count("[[20_能力与技能/extra]]"), 1)
        self.assertEqual(branch.count("[[20_能力与技能/手工卡]]"), 1)
        self.assertTrue(self.plan()["in_sync"])

    def test_duplicate_branch_link_or_missing_branch_is_reported_not_repaired(self):
        write(self.vault / "20_能力与技能.md", BRANCH + "- [[20_能力与技能/demo]]\n")
        self.assertIn("重复", self.plan()["problems"][0])
        (self.vault / "20_能力与技能.md").unlink()
        self.assertIn("缺少分支笔记", self.plan()["problems"][0])

    def test_value_index_follows_the_index_and_keeps_hand_added_lines(self):
        note = self.vault / content_notes.VALUE_INDEX_NOTE
        manual = f"- `hand-added` · semantic_source · `/elsewhere/x.md` · `{SHA_C}`\n"
        note.write_text(note.read_text(encoding="utf-8") + manual + "\n人工说明。\n", encoding="utf-8")
        self.assertTrue(self.plan()["in_sync"])
        self.indexes([skill("demo", SHA_A)], [value("method", SHA_A), value("second", SHA_B)])
        content_notes.apply(self.vault, self.plan())
        text = self.read(content_notes.VALUE_INDEX_NOTE)
        self.assertIn(f"- `method` · semantic_source · `/content/method.md` · `{SHA_A}`\n", text)
        self.assertIn("- `second` ·", text)
        self.assertEqual(text.count(manual), 1)
        self.assertTrue(text.endswith(manual + "\n人工说明。\n"))
        self.assertTrue(self.plan()["in_sync"])

    def test_user_text_between_the_list_and_a_later_hand_added_line_is_kept(self):
        """审查发现的缺口：列表后面先写说明、再补一条同格式条目时，中间的说明不能被删。"""
        note = self.vault / content_notes.VALUE_INDEX_NOTE
        tail = f"\n## 我的补充\n\n先写一段说明。\n\n- `hand` · semantic_source · `/x.md` · `{SHA_C}`\n结尾。\n"
        note.write_text(note.read_text(encoding="utf-8") + tail, encoding="utf-8")
        self.assertTrue(self.plan()["in_sync"])
        self.indexes([skill("demo", SHA_A)], [value("method", SHA_A), value("second", SHA_B)])
        content_notes.apply(self.vault, self.plan())
        text = self.read(content_notes.VALUE_INDEX_NOTE)
        self.assertTrue(text.endswith(tail))
        self.assertIn(f"`method` · semantic_source · `/content/method.md` · `{SHA_A}`", text)
        self.assertTrue(self.plan()["in_sync"])

    def test_value_note_without_exactly_one_recognisable_list_is_not_overwritten(self):
        note = self.vault / content_notes.VALUE_INDEX_NOTE
        for text in (note.read_text(encoding="utf-8").replace("正式目录只包含", "目录包含"),
                     note.read_text(encoding="utf-8") + "\n正式目录只包含当前验证通过的来源：\n\n"):
            with self.subTest(text=text[-20:]):
                note.write_text(text, encoding="utf-8")
                self.indexes([skill("demo", SHA_A)], [value("method", SHA_C)])
                self.assertIn("未覆盖", self.plan()["problems"][0])
                self.assertEqual(self.read(content_notes.VALUE_INDEX_NOTE), text)

    def test_skill_name_that_could_leave_the_branch_directory_is_refused(self):
        for name in ("../escape", "a/b", "x]]", ""):
            with self.subTest(name=name):
                self.indexes([dict(skill("demo", SHA_A), name=name)], [])
                with self.assertRaises(content_notes.ContentNotesError):
                    self.notes()

    def test_symlinked_card_and_unreadable_index_are_refused(self):
        card = self.vault / "20_能力与技能/demo.md"
        target = self.root / "elsewhere.md"
        target.write_text(card.read_text(encoding="utf-8"), encoding="utf-8")
        card.unlink()
        card.symlink_to(target)
        self.assertIn("符号链接", self.plan()["problems"][0])
        write(self.skills_index, "{not json")
        with self.assertRaises(content_notes.ContentNotesError):
            self.notes()


class ContentNotesCommandTest(ContentNotesCase):
    def setUp(self):
        super().setUp()
        self.config = SimpleNamespace(skills_index=self.skills_index, skills_root=self.root / "skills",
                                      knowledge_index=self.knowledge_index, memory={"obsidian_vault": str(self.vault)})
        self.rebuilds = []

    def run_command(self, verify=False, registry_ok=True, rebuild_error=None):
        owner = self

        class Bridge:
            def __init__(self, config):
                pass

            def _rebuild_and_verify_index(self, vault):
                owner.rebuilds.append(Path(vault))
                if rebuild_error:
                    raise RuntimeError(rebuild_error)
                return {"ok": True}

        checked = SimpleNamespace(verify=lambda: {"ok": registry_ok, "problems": [] if registry_ok else ["技能入口哈希不匹配：demo"]})
        output = io.StringIO()
        with patch.object(cli, "SkillRegistry", return_value=checked), \
                patch.object(cli, "KnowledgeCatalog", return_value=SimpleNamespace(verify=lambda: {"ok": True, "problems": []})), \
                patch.object(cli, "MemoryBridge", Bridge), redirect_stdout(output):
            code = cli._content_notes_sync(self.config, verify)
        return code, json.loads(output.getvalue())

    def test_in_sync_vault_writes_nothing_and_does_not_rebuild(self):
        code, result = self.run_command()
        self.assertEqual((code, result["in_sync"], result["written"], self.rebuilds), (0, True, [], []))

    def test_verify_reports_pending_without_writing(self):
        self.indexes([skill("demo", SHA_C)], [value("method", SHA_B)])
        before = self.read("20_能力与技能/demo.md")
        code, result = self.run_command(verify=True)
        self.assertEqual((code, result["pending"]), (1, ["20_能力与技能/demo.md"]))
        self.assertEqual(self.read("20_能力与技能/demo.md"), before)
        self.assertEqual(self.rebuilds, [])

    def test_sync_writes_then_rebuilds_the_note_index(self):
        self.indexes([skill("demo", SHA_C)], [value("method", SHA_B)])
        code, result = self.run_command()
        self.assertEqual((code, result["written"], result["in_sync"]), (0, ["20_能力与技能/demo.md"], True))
        self.assertIn(SHA_C, self.read("20_能力与技能/demo.md"))
        self.assertEqual(self.rebuilds, [self.vault])

    def test_failed_index_rebuild_puts_every_note_back(self):
        self.indexes([skill("demo", SHA_C), skill("extra", SHA_B)], [value("method", SHA_B)])
        before = {name: self.read(name) for name in ("20_能力与技能/demo.md", "20_能力与技能.md")}
        code, result = self.run_command(rebuild_error="tree invalid")
        self.assertEqual((code, result["ok"], result["written"]), (1, False, []))
        self.assertIn("已恢复笔记原样", result["error"])
        self.assertEqual({name: self.read(name) for name in before}, before)
        self.assertFalse((self.vault / "20_能力与技能/extra.md").exists())

    def test_failure_while_writing_puts_already_written_notes_back(self):
        self.indexes([skill("demo", SHA_C), skill("extra", SHA_B)], [value("method", SHA_B)])
        before = {name: self.read(name) for name in ("20_能力与技能/demo.md", "20_能力与技能.md")}
        real_apply = content_notes.apply

        def partial(vault, planned):
            real_apply(vault, dict(planned, writes=planned["writes"][:1]))
            raise OSError("disk full")

        with patch.object(content_notes, "apply", partial):
            code, result = self.run_command()
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertEqual({name: self.read(name) for name in before}, before)
        self.assertEqual(self.rebuilds, [])

    def test_failing_index_self_check_blocks_note_generation(self):
        self.indexes([skill("demo", SHA_C)], [value("method", SHA_B)])
        before = self.read("20_能力与技能/demo.md")
        code, result = self.run_command(registry_ok=False)
        self.assertEqual((code, result["status"]), (1, "blocked"))
        self.assertEqual(self.read("20_能力与技能/demo.md"), before)


class VaultHandoverTest(unittest.TestCase):
    """包里不再生成的笔记只有列在随包清单里才允许；交还的笔记原地保留。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fx = Fixture(Path(temp.name))
        self.previous = self.fx.layout.runtime / ".runtime/releases/v9.0.0"
        self.package = self.fx.layout.package
        self.note = "30_方法与知识/方法.md"
        write(self.previous / "second-brain" / self.note, "方法正文\n")
        seal(self.previous)
        index = switch.load(self.fx.layout.index)
        index["releases"][0]["files_manifest_sha256"] = switch.state(self.previous / "FILES.sha256")["sha256"]
        write(self.fx.layout.index, switch.encoded(index))
        write(self.fx.layout.vault / self.note, "方法正文\n用户后来改过。\n")
        write(self.fx.layout.runtime / ".runtime/knowlp/nodes.json", switch.graph_bytes(
            {"z_format": 1, "notes": sorted(p.read_text() for p in self.fx.layout.vault.rglob("*.md"))}))

    def handover(self, notes):
        write(self.package / "agentos/docs/second_brain/handover.json",
              switch.encoded({"schema_version": 1, "notes": notes}))
        seal(self.package)

    def preflight(self):
        return switch.preflight(self.fx.layout, self.fx.system, now=lambda: 1000)

    def test_listed_note_is_left_in_place_untouched_through_a_real_switch(self):
        self.handover([self.note])
        ticket = self.preflight()
        self.assertTrue(ticket["ok"], ticket)
        self.assertNotIn(self.note, ticket["vault_changed_notes"])
        result = switch.apply(self.fx.layout, ticket, self.fx.system, now=lambda: 1000)
        self.assertTrue(result["ok"], result)
        self.assertEqual((self.fx.layout.vault / self.note).read_text(), "方法正文\n用户后来改过。\n")

    def test_note_missing_from_the_package_but_not_listed_is_refused(self):
        for notes in ([], ["30_方法与知识/别的.md"]):
            with self.subTest(notes=notes):
                self.handover(notes)
                self.assertIn("deleted vault notes", self.preflight().get("error", ""))

    def test_missing_or_malformed_handover_list_is_refused(self):
        self.assertFalse(self.preflight()["ok"])
        write(self.package / "agentos/docs/second_brain/handover.json", switch.encoded({"schema_version": 2, "notes": [self.note]}))
        seal(self.package)
        self.assertIn("deleted vault notes", self.preflight().get("error", ""))


if __name__ == "__main__":
    unittest.main()
