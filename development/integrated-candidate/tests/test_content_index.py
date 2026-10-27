"""技能与价值文件出包之后：内容索引的生成、技能“草稿、审查、发布”的核对，以及切换时的三条内容路径步骤。"""
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xiaoba_agentos import cli, content_index
from xiaoba_agentos.skills import SkillRegistry
from xiaoba_agentos.user_rules import USER_RULES_TEXT
from test_switch_release import Fixture, seal, switch, write

PASS = "检查完毕，未见问题。\nFINAL_ACCEPTANCE: PASS\n"
FAIL = "发现越权指示。\nFINAL_ACCEPTANCE: FAIL\n"


class ContentIndexCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.skills = self.root / "assistant/skills"
        self.knowledge = self.root / "assistant/系统/价值文件索引"
        self.database = self.root / "agentos.sqlite3"
        self.project = self.root / "runtime"
        self.draft = self.root / "drafts/demo"
        write(self.skills / "demo/SKILL.md", "# demo\n第一版\n")
        write(self.skills / "demo/scripts/run.py", "print('demo')\n")
        write(self.skills / "other/SKILL.md", "# other\n")
        self.catalog([self.entry("demo"), self.entry("other")])
        content_index.write_index(self.skills / "index.json", content_index.build_skill_index(self.skills))
        baseline = {entry["id"]: sorted(content_index.evidence_lines(entry)) for entry in self.current()["entries"]}
        write(self.project / "config/content_baseline.json", json.dumps({"schema_version": 1, "skills": baseline}))
        write(self.root / "value.md", "方法原文\n")
        write(self.knowledge / "catalog.json", json.dumps({"schema_version": 1, "entries": [
            {"id": "method", "title": "方法", "category": "method", "index_mode": "semantic_source",
             "path": str(self.root / "value.md"), "summary": "s"}]}, ensure_ascii=False))
        connection = sqlite3.connect(self.database)
        connection.executescript(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, prompt TEXT);"
            "CREATE TABLE agent_runs (task_id TEXT, agent_name TEXT, status TEXT, output TEXT, created_at REAL);")
        connection.close()
        self.config = SimpleNamespace(
            project_dir=self.project, skills_root=self.skills, skills_index=self.skills / "index.json",
            knowledge_index=self.knowledge / "index.json", database_path=self.database,
            agents=[SimpleNamespace(name="agy_review", enabled=True), SimpleNamespace(name="dsh_review", enabled=True),
                    SimpleNamespace(name="retired", enabled=False)])
        self.synced = 0

    @staticmethod
    def entry(name, triggers=("演示",)):
        return {"id": name, "name": name, "directory": name, "description": "演示技能", "category": "test",
                "capabilities": ["verify"], "triggers": list(triggers)}

    def catalog(self, entries):
        write(self.skills / "catalog.json", json.dumps({"schema_version": 1, "entries": entries}, ensure_ascii=False))

    def current(self):
        return json.loads((self.skills / "index.json").read_text(encoding="utf-8"))

    def make_draft(self, skill="demo", files=None, source=None):
        """草稿：默认从正式目录拷一份再改。"""
        draft = self.root / "drafts" / skill
        if draft.exists():
            shutil.rmtree(draft)
        if (self.skills / skill).exists():
            shutil.copytree(source or self.skills / skill, draft)
        for relative, text in (files or {}).items():
            if text is None:
                (draft / relative).unlink()
            else:
                write(draft / relative, text)
        return draft

    def task(self, task_id, prompt, outputs=None, status="completed"):
        connection = sqlite3.connect(self.database)
        connection.execute("INSERT INTO tasks VALUES (?,?,?)", (task_id, status, prompt))
        for order, (agent, output) in enumerate((outputs or {"agy_review": PASS, "dsh_review": PASS}).items()):
            connection.execute("INSERT INTO agent_runs VALUES (?,?,?,?,?)", (task_id, agent, "completed", output, order))
        connection.commit()
        connection.close()

    def run_command(self, command, skill="demo", draft=None, **extra):
        def sync(config, verify_only):
            self.synced += 1
            return 0

        args = SimpleNamespace(command=command, skill_id=skill, draft=draft or self.root / "drafts" / skill, entry=None, review_task=None)
        args.__dict__.update(extra)
        output = io.StringIO()
        with patch.object(cli, "_content_notes_sync", sync), redirect_stdout(output):
            code = cli._content_index_command(self.config, args)
        text = output.getvalue()
        return code, (json.loads(text) if text.lstrip().startswith("{") else text)

    def review(self, task_id, skill="demo", **kwargs):
        """用工具生成的说明提交一条审查任务（两个后台都通过，除非另给 outputs）。"""
        code, prompt = self.run_command("skill-review-prompt", skill=skill)
        self.assertEqual(code, 0, prompt)
        self.task(task_id, prompt.strip(), **kwargs)   # 命令行传参会去掉末尾换行
        return prompt

    def verify(self):
        return SkillRegistry(self.config).verify()


class IndexGenerationTest(ContentIndexCase):
    def test_indexes_carry_file_hashes_and_no_software_version(self):
        skills = content_index.build_skill_index(self.skills)
        knowledge = content_index.build_knowledge_index(self.knowledge)
        self.assertNotIn("release", skills)
        self.assertNotIn("release", knowledge)
        self.assertEqual(sorted(skills["entries"][0]["files_sha256"]), ["SKILL.md", "scripts/run.py"])
        self.assertEqual(knowledge["entries"][0]["bytes"], len("方法原文\n".encode()))
        self.assertEqual(content_index.render(skills), content_index.render(content_index.build_skill_index(self.skills)))

    def test_unreadable_catalog_or_value_file_is_refused(self):
        (self.root / "value.md").unlink()
        with self.assertRaises(content_index.ContentIndexError):
            content_index.build_knowledge_index(self.knowledge)
        write(self.skills / "catalog.json", "{broken")
        with self.assertRaises(content_index.ContentIndexError):
            content_index.build_skill_index(self.skills)

    def test_knowledge_rebuild_needs_no_review(self):
        code, _ = self.run_command("knowledge-index-rebuild")
        self.assertEqual(code, 0)
        index = json.loads((self.knowledge / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["entries"][0]["id"], "method")
        self.assertEqual(self.synced, 1)

    def test_index_script_can_only_verify(self):
        script = Path(__file__).resolve().parents[1] / "scripts/create_skill_index.py"
        before = (self.skills / "index.json").read_bytes()
        write(self.skills / "demo/SKILL.md", "# demo\n改动\n")
        done = subprocess.run([sys.executable, "-B", str(script), "--root", str(self.skills)], capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("skill-publish", done.stderr)
        self.assertEqual((self.skills / "index.json").read_bytes(), before)

    def test_collaboration_skill_must_carry_the_user_rule_text(self):
        write(self.skills / "collaboration-core/SKILL.md", "# 协作\n没有规则原文\n")
        self.catalog([self.entry("demo"), self.entry("other"), self.entry("collaboration-core")])
        with self.assertRaises(content_index.ContentIndexError):
            content_index.build_skill_index(self.skills)
        draft = self.make_draft("collaboration-core", {"SKILL.md": "# 协作\n仍然没有\n"})
        code, result = self.run_command("skill-review-prompt", skill="collaboration-core", draft=draft)
        self.assertEqual((code, result["status"]), (1, "blocked"))
        draft = self.make_draft("collaboration-core", {"SKILL.md": "# 协作\n" + USER_RULES_TEXT + "\n"})
        code, _ = self.run_command("skill-review-prompt", skill="collaboration-core", draft=draft)
        self.assertEqual(code, 0)


class DraftReviewPublishTest(ContentIndexCase):
    """技能不就地改：草稿生成审查说明，两个后台通过后发布；证据是说明逐字节相同。"""

    def test_reviewed_draft_is_installed_indexed_trusted_and_synced(self):
        self.make_draft(files={"SKILL.md": "# demo\n第二版\n", "scripts/run.py": None, "notes.md": "新增说明\n"})
        prompt = self.review("t1")
        self.assertIn("第二版", prompt)
        self.assertTrue(self.verify()["ok"])                     # 审查期间正式位置没动，系统一直可用
        code, result = self.run_command("skill-publish", review_task="t1")
        self.assertEqual((code, result["ok"]), (0, True), result)
        self.assertEqual((self.skills / "demo/SKILL.md").read_text(encoding="utf-8"), "# demo\n第二版\n")
        self.assertFalse((self.skills / "demo/scripts/run.py").exists())
        self.assertTrue((self.skills / "demo/notes.md").is_file())
        recorded = {entry["id"]: entry.get("review_task") for entry in self.current()["entries"]}
        self.assertEqual(recorded, {"demo": "t1", "other": None})
        self.assertTrue(self.verify()["ok"], self.verify()["problems"])
        self.assertEqual(self.synced, 1)

    def test_enabling_the_standby_keeps_published_skills_trusted(self):
        # Round-50 rehearsal: verification counted every enabled agent, so
        # registering the standby (which reviews no skills) failed them all.
        self.make_draft(files={"SKILL.md": "# demo\n第二版\n"})
        self.review("t1")
        code, result = self.run_command("skill-publish", review_task="t1")
        self.assertEqual((code, result["ok"]), (0, True), result)
        self.config.agents.append(SimpleNamespace(name="qoder_review", enabled=True, standby=True))
        self.assertTrue(self.verify()["ok"], self.verify()["problems"])

    def test_publish_without_a_matching_passed_review_changes_nothing(self):
        self.make_draft(files={"SKILL.md": "# demo\n第二版\n"})
        self.review("failed", outputs={"agy_review": PASS, "dsh_review": FAIL})
        self.review("one", outputs={"agy_review": PASS})
        self.review("twice", outputs={"agy_review": PASS + PASS, "dsh_review": PASS})
        self.review("quoted", outputs={"agy_review": "结论是 FINAL_ACCEPTANCE: PASS 吧", "dsh_review": PASS})
        self.review("stranger", outputs={"agy_review": PASS, "someone_else": PASS})
        self.review("running", status="running")
        before = {str(p.relative_to(self.skills)): p.read_bytes() for p in self.skills.rglob("*") if p.is_file()}
        for task in ("failed", "one", "twice", "quoted", "stranger", "running", "no-such-task"):
            with self.subTest(task=task):
                code, result = self.run_command("skill-publish", review_task=task)
                self.assertEqual((code, result["status"]), (1, "blocked"), result)
        after = {str(p.relative_to(self.skills)): p.read_bytes() for p in self.skills.rglob("*") if p.is_file()}
        self.assertEqual(after, before)
        self.assertEqual(self.synced, 0)

    def test_draft_changed_after_the_review_is_not_covered(self):
        for change in ({"SKILL.md": "# demo\n审查之后又改\n"}, {"scripts/run.py": None}, {"extra.md": "多一个文件\n"}):
            with self.subTest(change=change):
                self.make_draft(files={"SKILL.md": "# demo\n第二版\n"})
                task = "t-" + "-".join(change)
                self.review(task)
                for relative, text in change.items():
                    (self.draft / relative).unlink() if text is None else write(self.draft / relative, text)
                code, result = self.run_command("skill-publish", review_task=task)
                self.assertEqual(code, 1)
                self.assertIn("不是同一份", result["problems"][0])

    def test_review_of_one_skill_cannot_publish_another(self):
        """审查发现的缺口：把技能甲的指纹行写进技能乙的正文，借乙的审查任务发布甲。"""
        self.make_draft(files={"SKILL.md": "# demo\n没审过的改动\n"})
        wanted = content_index.draft_entry(self.skills, "demo", self.draft)
        decoy = self.make_draft("other", {"SKILL.md": "# other\n" + "\n".join(content_index.evidence_lines(wanted)) + "\n"})
        self.review("decoy", skill="other")
        code, result = self.run_command("skill-publish", review_task="decoy")
        self.assertEqual(code, 1)
        self.assertIn("不是同一份", result["problems"][0])

    def test_hand_written_prompt_is_not_evidence_even_with_the_fingerprint_lines(self):
        self.make_draft(files={"SKILL.md": "# demo\n第二版\n"})
        wanted = content_index.draft_entry(self.skills, "demo", self.draft)
        self.task("hand", "请审查 demo。\n" + "\n".join(content_index.evidence_lines(wanted)))
        code, result = self.run_command("skill-publish", review_task="hand")
        self.assertEqual(code, 1)

    def test_draft_must_be_a_real_separate_directory_with_an_entry_point(self):
        code, result = self.run_command("skill-review-prompt", draft=self.root / "missing")
        self.assertEqual(code, 1)
        self.make_draft(files={"SKILL.md": None})
        self.assertEqual(self.run_command("skill-review-prompt")[0], 1)
        link = self.root / "drafts/link"
        link.symlink_to(self.skills / "demo")
        self.assertEqual(self.run_command("skill-review-prompt", draft=link)[0], 1)
        prompt = content_index.review_prompt(content_index.draft_entry(self.skills, "demo", self.skills / "demo"), self.skills / "demo")
        self.task("inplace", prompt)
        code, result = self.run_command("skill-publish", draft=self.skills / "demo", review_task="inplace")
        self.assertEqual(code, 1)
        self.assertIn("草稿目录不能就是正式技能目录", result["error"])

    def test_entry_cannot_point_a_skill_at_another_skills_directory(self):
        """审查发现的缺口：条目里的目录指向别的技能，发布会清掉并覆盖那个技能。"""
        self.make_draft(files={"SKILL.md": "# demo\n第二版\n"})
        other_before = {str(p.relative_to(self.skills)): p.read_bytes() for p in (self.skills / "other").rglob("*") if p.is_file()}
        for directory in ("other", "demo/sub", "../demo", "新目录"):
            with self.subTest(directory=directory):
                entry = self.root / "entry.json"
                write(entry, json.dumps(dict(self.entry("demo"), directory=directory), ensure_ascii=False))
                code, result = self.run_command("skill-review-prompt", entry=entry)
                self.assertEqual((code, result["status"]), (1, "blocked"))
                code, result = self.run_command("skill-publish", entry=entry, review_task="whatever")
                self.assertEqual(code, 1)
        forged = dict(content_index.draft_entry(self.skills, "demo", self.draft), directory="other")
        with self.assertRaises(content_index.ContentIndexError):
            content_index.publish_skill(self.skills, self.skills / "index.json", forged, self.draft, "t")
        after = {str(p.relative_to(self.skills)): p.read_bytes() for p in (self.skills / "other").rglob("*") if p.is_file()}
        self.assertEqual(after, other_before)

    def test_symlink_inside_the_live_skill_directory_blocks_publish_cleanly(self):
        self.make_draft(files={"SKILL.md": "# demo\n第二版\n"})
        self.review("t1")
        (self.skills / "demo/link.md").symlink_to(self.skills / "other/SKILL.md")
        code, result = self.run_command("skill-publish", review_task="t1")
        self.assertEqual((code, result["status"]), (1, "blocked"))
        self.assertEqual((self.skills / "demo/SKILL.md").read_text(encoding="utf-8"), "# demo\n第一版\n")

    def test_verify_script_accepts_an_index_that_records_review_tasks(self):
        script = Path(__file__).resolve().parents[1] / "scripts/create_skill_index.py"
        self.make_draft(files={"SKILL.md": "# demo\n第二版\n"})
        self.review("t1")
        self.assertEqual(self.run_command("skill-publish", review_task="t1")[0], 0)
        done = subprocess.run([sys.executable, "-B", str(script), "--root", str(self.skills), "--verify"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        write(self.skills / "demo/SKILL.md", "# demo\n就地改\n")
        done = subprocess.run([sys.executable, "-B", str(script), "--root", str(self.skills), "--verify"], capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)

    def test_new_skill_needs_an_entry_and_changed_entry_is_part_of_what_is_reviewed(self):
        draft = self.make_draft("fresh", {"SKILL.md": "# fresh\n"})
        self.assertEqual(self.run_command("skill-review-prompt", skill="fresh", draft=draft)[0], 1)
        entry = self.root / "fresh.json"
        write(entry, json.dumps(self.entry("fresh", triggers=("新技能",)), ensure_ascii=False))
        code, prompt = self.run_command("skill-review-prompt", skill="fresh", draft=draft, entry=entry)
        self.assertEqual(code, 0)
        self.task("t-fresh", prompt)
        write(entry, json.dumps(self.entry("fresh", triggers=("审查后改了触发词",)), ensure_ascii=False))
        self.assertEqual(self.run_command("skill-publish", skill="fresh", draft=draft, entry=entry, review_task="t-fresh")[0], 1)
        write(entry, json.dumps(self.entry("fresh", triggers=("新技能",)), ensure_ascii=False))
        code, result = self.run_command("skill-publish", skill="fresh", draft=draft, entry=entry, review_task="t-fresh")
        self.assertEqual(code, 0, result)
        self.assertEqual([item["id"] for item in self.current()["entries"]], ["demo", "fresh", "other"])
        self.assertTrue(self.verify()["ok"], self.verify()["problems"])
        write(entry, json.dumps(dict(self.entry("fresh"), review_task="x"), ensure_ascii=False))
        self.assertEqual(self.run_command("skill-review-prompt", skill="fresh", draft=draft, entry=entry)[0], 1)


class PublicationTrustTest(ContentIndexCase):
    """索引在包外、谁都能改：运行时要求每个技能要么与随包基线一致，要么带着核对得过的审查任务。"""

    def hand_edit(self, files, review_task=None):
        """绕过命令：就地改技能文件，再直接把新哈希写进索引。"""
        for relative, text in files.items():
            (self.skills / "demo" / relative).unlink() if text is None else write(self.skills / "demo" / relative, text)
        index = content_index.build_skill_index(self.skills)
        if review_task:
            next(entry for entry in index["entries"] if entry["id"] == "demo")["review_task"] = review_task
        content_index.write_index(self.skills / "index.json", index)

    def test_content_matching_the_release_baseline_is_trusted(self):
        self.assertTrue(self.verify()["ok"], self.verify()["problems"])

    def test_hand_edited_index_for_unreviewed_content_is_not_trusted(self):
        self.hand_edit({"SKILL.md": "# demo\n未经审查的改动\n"})
        self.assertIn("未经审查", "".join(self.verify()["problems"]))

    def test_removing_or_adding_a_file_of_a_baseline_skill_is_not_trusted(self):
        """审查发现的缺口：删掉基线技能的一个文件，剩下的指纹仍“属于基线”，不能因此放行。"""
        self.hand_edit({"scripts/run.py": None})
        self.assertIn("未经审查", "".join(self.verify()["problems"]))
        self.hand_edit({"scripts/run.py": "print('demo')\n", "extra.md": "多出来的文件\n"})
        self.assertIn("未经审查", "".join(self.verify()["problems"]))
        self.hand_edit({"extra.md": None})
        self.assertTrue(self.verify()["ok"], self.verify()["problems"])

    def test_one_skill_cannot_borrow_another_skills_baseline(self):
        index = self.current()
        swapped = {"demo": sorted(content_index.evidence_lines(index["entries"][1])),
                   "other": sorted(content_index.evidence_lines(index["entries"][0]))}
        write(self.project / "config/content_baseline.json", json.dumps({"schema_version": 1, "skills": swapped}))
        self.assertEqual(len([p for p in self.verify()["problems"] if "未经审查" in p]), 2)

    def test_recorded_review_task_must_be_for_exactly_the_content_on_disk(self):
        self.make_draft(files={"SKILL.md": "# demo\n第二版\n"})
        self.review("t1")
        self.assertEqual(self.run_command("skill-publish", review_task="t1")[0], 0)
        self.assertTrue(self.verify()["ok"], self.verify()["problems"])
        # 发布之后再就地改，并把索引手改成与现状一致、仍挂着旧任务
        for change in ({"SKILL.md": "# demo\n发布后偷偷改\n"}, {"notes.md": "多一个文件\n"}):
            with self.subTest(change=change):
                self.hand_edit(change, review_task="t1")
                self.assertIn("核对不过", "".join(self.verify()["problems"]))
        self.hand_edit({"SKILL.md": "# demo\n第二版\n", "notes.md": None}, review_task="no-such-task")
        self.assertIn("核对不过", "".join(self.verify()["problems"]))
        self.hand_edit({}, review_task="t1")
        self.assertTrue(self.verify()["ok"], self.verify()["problems"])

    def test_unreadable_or_missing_baseline_fails_closed_in_a_release_bound_directory(self):
        write(self.project / "config/content_baseline.json", "{broken")
        self.assertIn("基线不可读", "".join(self.verify()["problems"]))
        (self.project / "config/content_baseline.json").unlink()
        self.assertTrue(self.verify()["ok"])            # 没有发布清单的目录是测试夹具，不要求基线
        write(self.project / "SOURCE_MANIFEST.json", "{}")
        self.assertIn("缺少技能内容基线", "".join(self.verify()["problems"]))

    def test_packaged_baseline_is_exactly_the_fixture_content(self):
        root = Path(__file__).resolve().parents[1]
        index = json.loads((root / "tests/fixtures/content/skills/index.json").read_text(encoding="utf-8"))
        expected = {entry["id"]: sorted(content_index.evidence_lines(entry)) for entry in index["entries"]}
        packaged = json.loads((root / "config/content_baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(packaged["skills"], expected)


class ContentPathStepTest(unittest.TestCase):
    """切换时把配置里的三个内容路径指到助理目录下的固定位置；别的值、别的字段一律拒绝。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fx = Fixture(Path(temp.name))
        self.layout = self.fx.layout
        config = switch.load(self.layout.config)
        config["skills"] = {"root_dir": str(self.layout.runtime / "skills"), "index_path": str(self.layout.runtime / "skills/index.json")}
        config["knowledge"] = {"index_path": "knowledge/index.json"}
        write(self.layout.config, switch.encoded(config))
        write(self.layout.assistant / "skills/index.json", "{}")
        write(self.layout.assistant / "系统/价值文件索引/index.json", "{}")

    def steps(self, **override):
        steps = [
            {"id": "0001-skills-root", "check": {"kind": "config_value_equals", "path": "skills.root_dir", "runtime_path": "skills"},
             "execute": {"kind": "config_set", "path": "skills.root_dir", "assistant_path": "skills"}, "undo": {"kind": "restore_in_memory"}},
            {"id": "0002-skills-index", "check": {"kind": "config_value_equals", "path": "skills.index_path", "runtime_path": "skills/index.json"},
             "execute": {"kind": "config_set", "path": "skills.index_path", "assistant_path": "skills/index.json"}, "undo": {"kind": "restore_in_memory"}},
            {"id": "0003-knowledge-index", "check": {"kind": "config_value_equals", "path": "knowledge.index_path", "value": "knowledge/index.json"},
             "execute": {"kind": "config_set", "path": "knowledge.index_path", "assistant_path": "系统/价值文件索引/index.json"}, "undo": {"kind": "restore_in_memory"}},
        ]
        for step in steps:
            step["execute"].update(override.get(step["id"], {}))
        declaration = switch.load(self.layout.package / "agentos" / switch.SPEC)
        declaration["steps"] = steps
        write(self.layout.package / "agentos" / switch.SPEC, switch.encoded(declaration))
        seal(self.layout.package)

    def preflight(self):
        return switch.preflight(self.layout, self.fx.system, now=lambda: 1000)

    def test_three_paths_are_repointed_and_a_failed_switch_restores_them(self):
        self.steps()
        before = self.fx.whole_tree()
        result = switch.apply(self.layout, self.preflight(), self.fx.system, now=lambda: 1000,
                              fault=lambda phase: (_ for _ in ()).throw(switch.SwitchError("fault")) if phase == "step:0003-knowledge-index" else None)
        self.assertEqual(result["rollback"]["status"], "已退回", result)
        self.assertEqual(self.fx.whole_tree(), before)
        result = switch.apply(self.layout, self.preflight(), self.fx.system, now=lambda: 1000)
        self.assertTrue(result["activated"], result)
        config = switch.load(self.layout.config)
        self.assertEqual(config["skills"], {"root_dir": str(self.layout.assistant / "skills"),
                                            "index_path": str(self.layout.assistant / "skills/index.json")})
        self.assertEqual(config["knowledge"]["index_path"], str(self.layout.assistant / "系统/价值文件索引/index.json"))
        self.assertEqual(config["private_provider_marker"], "must-remain-unchanged")

    def test_any_other_destination_or_field_is_refused(self):
        for override in ({"0001-skills-root": {"assistant_path": "工具"}},
                         {"0002-skills-index": {"assistant_path": "../outside/index.json"}},
                         {"0003-knowledge-index": {"value": "/tmp/index.json"}},
                         {"0001-skills-root": {"path": "skills.max_selected"}}):
            with self.subTest(override=override):
                self.steps(**override)
                result = self.preflight()
                self.assertFalse(result["ok"], result)

    def test_switch_is_refused_while_the_content_is_not_in_place(self):
        (self.layout.assistant / "系统/价值文件索引/index.json").unlink()
        self.steps()
        self.assertIn("content is not in place", self.preflight().get("error", ""))

    def test_config_that_does_not_hold_the_expected_old_value_is_refused(self):
        config = switch.load(self.layout.config)
        config["skills"]["root_dir"] = "/somewhere/else"
        write(self.layout.config, switch.encoded(config))
        self.steps()
        self.assertIn("config step input/result differs", self.preflight().get("error", ""))


class AbsentContentDirectoriesTest(unittest.TestCase):
    """技能和价值文件索引出包：新包没有这两个目录，切换时运行目录里的旧文件被移除，失败时放回。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fx = Fixture(Path(temp.name))
        self.layout = self.fx.layout
        previous = self.layout.runtime / ".runtime/releases/v9.0.0"
        for root in (previous / "agentos", self.layout.runtime):
            write(root / "skills/demo/SKILL.md", "# demo\n")
            write(root / "knowledge/index.json", "{}\n")
        seal(previous)
        write(self.layout.runtime / "SOURCE_MANIFEST.json", (previous / "agentos/SOURCE_MANIFEST.json").read_bytes())
        index = switch.load(self.layout.index)
        index["releases"][0]["files_manifest_sha256"] = switch.state(previous / "FILES.sha256")["sha256"]
        write(self.layout.index, switch.encoded(index))
        for name in ("skills", "knowledge"):
            directory = self.layout.package / "agentos" / name
            self.assertEqual(list(directory.rglob("*")), [])
            directory.rmdir()
        spec = switch.load(self.layout.package / "agentos" / switch.SPEC)
        spec["from_source_manifest_sha256"] = switch.state(previous / "agentos/SOURCE_MANIFEST.json")["sha256"]
        write(self.layout.package / "agentos" / switch.SPEC, switch.encoded(spec))
        seal(self.layout.package)
        self.fx.system.record_worker(100)

    def test_package_without_the_two_directories_switches_and_rolls_back_cleanly(self):
        before = self.fx.whole_tree()
        ticket = switch.preflight(self.layout, self.fx.system, now=lambda: 1000)
        self.assertTrue(ticket["ok"], ticket)
        result = switch.apply(self.layout, ticket, self.fx.system, now=lambda: 1000,
                              fault=lambda phase: (_ for _ in ()).throw(switch.SwitchError("fault")) if phase == "readback" else None)
        self.assertEqual(result["rollback"]["status"], "已退回", result)
        self.assertEqual(self.fx.whole_tree(), before)
        result = switch.apply(self.layout, switch.preflight(self.layout, self.fx.system, now=lambda: 1000), self.fx.system, now=lambda: 1000)
        self.assertTrue(result["activated"], result)
        self.assertFalse((self.layout.runtime / "skills").exists())
        self.assertFalse((self.layout.runtime / "knowledge").exists())
        self.assertTrue(switch.readback(self.layout, self.fx.system)["ok"])

    def test_file_reappearing_under_a_managed_directory_breaks_the_binding(self):
        ticket = switch.preflight(self.layout, self.fx.system, now=lambda: 1000)
        self.assertTrue(switch.apply(self.layout, ticket, self.fx.system, now=lambda: 1000)["activated"])
        write(self.layout.runtime / "skills/sneaked/SKILL.md", "# 不在清单里\n")
        self.assertIn("source manifest mismatch", switch.readback(self.layout, self.fx.system).get("error", ""))


if __name__ == "__main__":
    unittest.main()
