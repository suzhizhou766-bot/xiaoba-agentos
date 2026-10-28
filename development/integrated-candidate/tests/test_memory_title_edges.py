"""P06/P10 的标题与文件名边界：围栏内部不作标题、阶段后缀只剥末尾完整配对、文件名 40 字内不留半个词。

只调用类方法和一个临时 Vault fixtures，不调用真实模型、不写正式目录。
"""
import tempfile
import unittest
from pathlib import Path

from fixtures_review import _config
from xiaoba_agentos.memory import MemoryBridge
from xiaoba_agentos.store import EventStore

TASK_ID = "de03126a-a26c-444b-8f93-d2ea27fdf116"
SUFFIX = "-de03126a.md"


def title_part(filename: str) -> str:
    assert filename.endswith(SUFFIX), filename
    return filename[: -len(SUFFIX)]


class FencedTitleTest(unittest.TestCase):
    def test_heading_inside_a_fence_is_never_the_title(self):
        text = "```bash\n# 安装依赖\npip install x\n```\n\n# 发布前置检查\n\n正文"
        self.assertEqual("发布前置检查", MemoryBridge.note_title(text, TASK_ID))
        self.assertEqual("发布前置检查-de03126a.md", MemoryBridge.note_filename(text, TASK_ID))

    def test_only_fenced_heading_falls_back_through_the_tiers(self):
        fenced = "```\n# 围栏内标题\n```\n"
        # 一级标题取不到 → 退到审批哈希覆盖的 frontmatter title
        with_metadata = '---\ntitle: "审批内标题"\n---\n\n' + fenced + "\n正文"
        self.assertEqual("审批内标题", MemoryBridge.note_title(with_metadata, TASK_ID))
        # 没有 frontmatter → 退到“任务”一节
        with_task = fenced + "\n## 任务\n\n任务首行\n"
        self.assertEqual("任务首行", MemoryBridge.note_title(with_task, TASK_ID))
        # 三层都没有 → 保留旧「经验」兜底，不读未哈希的 task.prompt
        without_any = '---\ntask_id: "x"\n---\n\n' + fenced + "\n正文没有标题也没有任务节\n"
        self.assertEqual("经验", MemoryBridge.note_title(without_any, TASK_ID))
        self.assertEqual("经验-de03126a.md", MemoryBridge.note_filename(without_any, TASK_ID))

    def test_tilde_fence_and_length_matched_closing(self):
        tilde = "~~~\n# 围栏内\n~~~\n\n# 围栏外\n"
        self.assertEqual("围栏外", MemoryBridge.note_title(tilde, TASK_ID))
        # 三反引号不能关闭四反引号围栏，中间那行一级标题仍在围栏内部
        longer = "````\n# 内部一\n```\n# 内部二\n````\n\n# 外面\n"
        self.assertEqual("外面", MemoryBridge.note_title(longer, TASK_ID))

    def test_tilde_fence_info_string_may_hold_tildes_and_backticks(self):
        # Q4：波浪线围栏的信息串可以含 ~ 或 `，这类行仍是围栏起点，里面的标题不作标题
        for opener in ("~~~ foo~bar", "~~~ aa ``` ~~~"):
            with self.subTest(opener=opener):
                text = f"{opener}\n# 围栏内假标题\n~~~\n\n# 合法标题\n"
                self.assertEqual("合法标题", MemoryBridge.note_title(text, TASK_ID))
                self.assertEqual("合法标题-de03126a.md", MemoryBridge.note_filename(text, TASK_ID))
                # 同一输入再取一次，标题和文件名逐字不变
                self.assertEqual(MemoryBridge.note_title(text, TASK_ID),
                                 MemoryBridge.note_title(text, TASK_ID))
                self.assertEqual(MemoryBridge.note_filename(text, TASK_ID),
                                 MemoryBridge.note_filename(text, TASK_ID))
                # 正文里围栏内的假标题原样留着，只是不作标题来源
                self.assertIn("# 围栏内假标题", MemoryBridge._titled_body(TASK_ID, text))
                self.assertEqual(1, len([line for line in MemoryBridge._prose_lines(text.splitlines())
                                         if line.startswith("# ")]))

    def test_unclosed_tilde_fence_with_info_string_supplies_no_title(self):
        # 未闭合的波浪线围栏（信息串含 ~ 或 `）把后面的内容全留在代码里：
        # 既不能提供一级标题，也不能把围栏内的伪“任务”节当任务取出来
        for opener in ("~~~ foo~bar", "~~~ aa ``` ~~~"):
            with self.subTest(opener=opener):
                text = f"{opener}\n## 任务\n伪任务首行\n"
                self.assertEqual("经验", MemoryBridge.note_title(text, TASK_ID))
                self.assertEqual("经验-de03126a.md", MemoryBridge.note_filename(text, TASK_ID))
        # 有审批哈希覆盖的 frontmatter title 时，优先回退元数据而不是兜底
        with_metadata = ('---\ntitle: "审批内标题"\n---\n\n'
                         "~~~ foo~bar\n# 围栏内假标题\n\n## 任务\n伪任务首行\n")
        self.assertEqual("审批内标题", MemoryBridge.note_title(with_metadata, TASK_ID))
        self.assertEqual("审批内标题-de03126a.md", MemoryBridge.note_filename(with_metadata, TASK_ID))

    def test_closing_fence_rules_and_backtick_info_string_not_relaxed(self):
        # 反引号围栏的信息串含反引号 → 这行仍不是围栏起点
        self.assertEqual("真标题", MemoryBridge.note_title("```a`b\n# 真标题\n", TASK_ID))
        # 关闭围栏仍要求同种字符、长度不小于开启行、行内不能再有别的内容
        self.assertEqual("经验", MemoryBridge.note_title("~~~ foo~bar\n# 内部\n```\n# 后面\n", TASK_ID))
        self.assertEqual("经验", MemoryBridge.note_title("~~~ foo~bar\n# 内部\n~~~ 尾随说明\n# 后面\n", TASK_ID))
        self.assertEqual("经验", MemoryBridge.note_title("~~~~ foo~bar\n# 内部\n~~~\n# 后面\n", TASK_ID))
        # 更长的同种行才算关闭
        self.assertEqual("后面", MemoryBridge.note_title("~~~ foo~bar\n# 内部\n~~~~\n# 后面\n", TASK_ID))

    def test_info_string_opens_but_same_marker_in_info_does_not(self):
        info = '```python title="x"\n# 围栏内\n```\n\n# 外面标题\n'
        self.assertEqual("外面标题", MemoryBridge.note_title(info, TASK_ID))
        # 信息串里带反引号 → 这行不是围栏，后面的一级标题照旧有效
        not_fence = "```a`b\n# 真标题\n"
        self.assertEqual("真标题", MemoryBridge.note_title(not_fence, TASK_ID))
        # 闭合围栏后面还有内容 → 不算闭合，余下内容仍在围栏内部
        unclosed_by_content = "```\n# 内部\n``` 尾部说明\n# 后面\n"
        self.assertEqual("经验", MemoryBridge.note_title(unclosed_by_content, TASK_ID))

    def test_unclosed_fence_swallows_everything_after_it(self):
        before = "# 围栏前标题\n\n```\n# 围栏后标题\n"
        self.assertEqual("围栏前标题", MemoryBridge.note_title(before, TASK_ID))
        only = "```\n# 围栏后标题\n\n## 任务\n\n任务首行\n"
        self.assertEqual("经验", MemoryBridge.note_title(only, TASK_ID))

    def test_task_section_extraction_skips_fenced_lines(self):
        # 「## 任务」本身在围栏里 → 不算任务节；围栏外的真任务首行才被取到
        text = "## 任务\n\n```\n## 任务\n\n假任务行\n```\n\n真任务行\n"
        self.assertEqual("真任务行", MemoryBridge.note_title(text, TASK_ID))
        # 任务节里第一个非空行在围栏内部 → 继续往后找围栏外的第一行
        second = "## 任务\n\n```\n# 假的一级标题\n```\n\n真任务行\n\n后面的行\n"
        self.assertEqual("真任务行", MemoryBridge.note_title(second, TASK_ID))

    def test_three_space_indented_fence_is_still_a_fence(self):
        self.assertEqual("外面", MemoryBridge.note_title("   ```\n# 内部\n   ```\n\n# 外面\n", TASK_ID))


class StageMarkSuffixTest(unittest.TestCase):
    def test_complete_matching_pairs_at_the_end_are_removed(self):
        for raw, expected in [
            ("标题（待审核）", "标题"),
            ("标题(待审核)", "标题"),
            ("标题（宿主整理，待审核）", "标题"),
            ("标题(宿主整理, 待审核)", "标题"),
            ("标题 （待审核）  ", "标题"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(expected, MemoryBridge._without_stage_mark(raw))

    def test_mismatched_incomplete_nested_or_midstring_marks_are_kept(self):
        for raw in [
            "标题（待审核)",      # 错配
            "标题(待审核）",      # 错配
            "标题（待审核",       # 缺右括号
            "标题待审核)",        # 缺左括号
            "标题（待审核）附录",   # 在中间
            "标题（宿主（待审核））",  # 同种嵌套
            "标题（宿主(待审核)）",   # 跨种嵌套
            "标题（已批准）（待审核）说明",
            "待审核",
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(raw, MemoryBridge._without_stage_mark(raw))

    def test_only_the_trailing_run_of_stage_marks_is_removed(self):
        # 末尾括号不含「待审核」→ 整串保留（主力决定：不按 Q1 的“反复删到标题（第二版）”）
        self.assertEqual("标题（待审核）（第二版）", MemoryBridge._without_stage_mark("标题（待审核）（第二版）"))
        self.assertEqual("标题（第二版）", MemoryBridge._without_stage_mark("标题（第二版）（待审核）"))
        # 连续的末尾阶段标注全剥，标题才可能幂等；遇到非阶段标注的括号就停，
        # 所以前面那个「（待审核）」已经不在末尾，原样留着。
        self.assertEqual("标题", MemoryBridge._without_stage_mark("标题（待审核）（待审核）"))
        self.assertEqual("标题（待审核）（第二版）",
                         MemoryBridge._without_stage_mark("标题（待审核）（第二版）（待审核）"))

    def test_titled_body_reaches_a_fixed_point(self):
        for body in [
            "# 标题（待审核）（待审核）\n\n正文",
            "# 验收防误判规则（宿主整理，待审核）\n\n正文一行",
            "# 标题（待审核）（第二版）\n\n正文",
            "# 标题(待审核)\n\n正文",
            "# 标题（待审核\n\n正文",
        ]:
            with self.subTest(body=body):
                once = MemoryBridge._titled_body(TASK_ID, body)
                self.assertEqual(once, MemoryBridge._titled_body(TASK_ID, once))


class FilenameLimitTest(unittest.TestCase):
    def test_title_part_is_capped_at_forty_unicode_characters_not_bytes(self):
        for count, expected in [(39, 39), (40, 40), (41, 40), (200, 40)]:
            with self.subTest(count=count):
                part = title_part(MemoryBridge.note_filename("# " + "长" * count, TASK_ID))
                self.assertEqual(expected, len(part))
                self.assertLessEqual(len(part), 40)
        # 40 个汉字是 120 字节，仍然允许 → 说明限制是码位不是字节
        self.assertEqual(120, len(("长" * 40).encode("utf-8")))

    def test_english_word_is_never_left_half_on_the_boundary(self):
        # 40 字位置落在词内部 → 退回词前，不留半个词
        text = "# Continuous integration pipeline migration checklist for testing"
        self.assertEqual("Continuous integration pipeline", title_part(MemoryBridge.note_filename(text, TASK_ID)))
        # 40 字位置正好是词尾（40 个 x 之后是空格）→ 完整词保留
        self.assertEqual("x" * 40, title_part(MemoryBridge.note_filename("# " + "x" * 40 + " yy", TASK_ID)))
        # 词跨越边界但词前还有中文 → 只保留中文，不切词
        mixed = "# 大模型派工规则 continuous-integration-pipeline-migration-checklist"
        self.assertEqual("大模型派工规则", title_part(MemoryBridge.note_filename(mixed, TASK_ID)))

    def test_hyphen_and_apostrophe_bind_one_word(self):
        # 连字符/撇号/下划线两侧都是字母数字时属于同一个词，词内不切开
        self.assertEqual(
            "don't do this partially truncated word",
            title_part(MemoryBridge.note_filename("# don't do this partially truncated word here now", TASK_ID)),
        )
        self.assertEqual(
            "经验", title_part(MemoryBridge.note_filename("# continuous-integration-pipeline-migration", TASK_ID))
        )
        self.assertEqual("a-b", MemoryBridge.note_title("# a-b", TASK_ID))

    def test_single_over_long_word_falls_back_to_experience_but_body_keeps_the_full_title(self):
        for long_word in ["a" * 60, "1234567890" * 6, "ab_cd" * 12]:
            with self.subTest(long_word=long_word):
                text = "# " + long_word + "\n\n正文"
                self.assertEqual(long_word, MemoryBridge.note_title(text, TASK_ID))
                self.assertEqual("经验-de03126a.md", MemoryBridge.note_filename(text, TASK_ID))
                # 只有文件名被兜底，正文一级标题保留完整合法原标题
                self.assertEqual(text, MemoryBridge._titled_body(TASK_ID, "# " + long_word + "\n\n正文"))

    def test_dangerous_path_characters_are_replaced_and_the_part_is_never_empty(self):
        self.assertEqual(
            "a b c d e f g h i j k quoted",
            title_part(MemoryBridge.note_filename('# a/b:c*d?e<f>g|h^i[j]k "quoted"', TASK_ID)),
        )
        # 整串都是非法字符 → 文件名不出现空标题部分
        self.assertEqual("经验-de03126a.md", MemoryBridge.note_filename("# ///", TASK_ID))
        self.assertEqual("经验-de03126a.md", MemoryBridge.note_filename("# ...", TASK_ID))

    def test_same_title_different_tasks_and_repeated_calls_are_stable(self):
        text = "# 同一个标题"
        first = MemoryBridge.note_filename(text, "11111111-1111-1111-1111-111111111111")
        second = MemoryBridge.note_filename(text, "22222222-2222-2222-2222-222222222222")
        self.assertEqual(("同一个标题-11111111.md", "同一个标题-22222222.md"), (first, second))
        long_text = "# Continuous integration pipeline migration checklist for testing"
        self.assertEqual(MemoryBridge.note_filename(long_text, TASK_ID), MemoryBridge.note_filename(long_text, TASK_ID))


class PublishedNoteBoundaryTest(unittest.TestCase):
    """自动候选的结果里带代码围栏时，发布出去的标题、文件名和改名幂等。"""

    def fixture(self, root: Path):
        config = _config(root)
        config.raw["project_dir"] = str(Path(__file__).resolve().parents[1])
        vault = root / "vault"
        config.raw["memory"].update({
            "publish_enabled": True,
            "obsidian_vault": str(vault),
            "obsidian_subdir": "70_复利规则/经验",
            "catalog_note": "70_复利规则/经验索引.md",
            "knowlp_graph_dir": str(root / "runtime/graph"),
        })
        (vault / "70_复利规则").mkdir(parents=True)
        (vault / "root.md").write_text(
            '---\nid: "root"\ntype: "root"\nparent: ""\nstatus: "active"\nauthority: true\n'
            "---\n\n# Root\n\n- [[70_复利规则/经验索引]]\n",
            encoding="utf-8",
        )
        catalog = vault / "70_复利规则" / "经验索引.md"
        catalog.write_text(
            '---\nid: "experiences"\ntype: "branch"\nparent: "root"\nstatus: "active"\nauthority: true\n'
            "---\n\n# 经验索引\n",
            encoding="utf-8",
        )
        store = EventStore(config.database_path)
        store.initialize()
        return config, catalog, MemoryBridge(config, store)

    def test_publication_ignores_fenced_headings_and_rename_is_idempotent(self):
        task_id = "aaaaaaaa-1111-1111-1111-111111111111"
        with tempfile.TemporaryDirectory() as directory:
            config, catalog, bridge = self.fixture(Path(directory))
            bridge.stage_experience(
                task_id,
                "检索重复文件（待审核）",
                "```python\n# 假的一级标题\nprint(1)\n```\n\n真结果正文",
                [],
            )
            published = bridge.publish_candidate(task_id)
            note = Path(published["path"])
            self.assertEqual("检索重复文件-aaaaaaaa.md", note.name)
            text = note.read_text(encoding="utf-8")
            self.assertIn("# 检索重复文件\n", text)
            self.assertIn("# 假的一级标题", text)          # 围栏内的原样留着，只是不作标题
            self.assertNotIn("待审核", text.split("---\n\n", 1)[1].splitlines()[0])
            self.assertEqual([f"- [[70_复利规则/经验/检索重复文件-aaaaaaaa]]"],
                             [line for line in catalog.read_text(encoding="utf-8").splitlines()
                              if line.startswith("- [[")])
            prose = MemoryBridge._prose_lines(text.split("---\n\n", 1)[1].splitlines())
            self.assertEqual(1, len([line for line in prose if line.startswith("# ")]))
            again = bridge.rename_note(task_id)
            self.assertFalse(again["changed"])            # 改名幂等
            self.assertEqual(note, bridge.find_note(Path(config.raw["memory"]["obsidian_vault"]).resolve(), task_id))

    def test_publication_ignores_headings_inside_tilde_fences_with_info_strings(self):
        task_id = "bbbbbbbb-1111-1111-1111-111111111111"
        result = ("~~~ foo~bar\n# 围栏内假标题一\n~~~\n\n"
                  "~~~ aa ``` ~~~\n# 围栏内假标题二\n~~~\n\n真结果正文")
        with tempfile.TemporaryDirectory() as directory:
            config, catalog, bridge = self.fixture(Path(directory))
            bridge.stage_experience(task_id, "波浪线围栏候选（待审核）", result, [])
            published = bridge.publish_candidate(task_id)
            note = Path(published["path"])
            self.assertEqual("波浪线围栏候选-bbbbbbbb.md", note.name)
            text = note.read_text(encoding="utf-8")
            self.assertIn("# 波浪线围栏候选\n", text)
            for fake in ("# 围栏内假标题一", "# 围栏内假标题二"):
                self.assertIn(fake, text)                 # 字节仍在正文里，只是不作标题
            prose = MemoryBridge._prose_lines(text.split("---\n\n", 1)[1].splitlines())
            self.assertEqual([f"# {MemoryBridge.note_title(text, task_id)}"],
                             [line for line in prose if line.startswith("# ")])
            self.assertEqual([f"- [[70_复利规则/经验/波浪线围栏候选-bbbbbbbb]]"],
                             [line for line in catalog.read_text(encoding="utf-8").splitlines()
                              if line.startswith("- [[")])
            again = bridge.rename_note(task_id)
            self.assertFalse(again["changed"])


if __name__ == "__main__":
    unittest.main()
