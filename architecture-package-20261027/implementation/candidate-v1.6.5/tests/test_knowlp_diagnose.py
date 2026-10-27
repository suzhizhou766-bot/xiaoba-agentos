import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_knowlp_index.py"
SPEC = importlib.util.spec_from_file_location("build_knowlp_index", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class KnowLPDiagnoseTest(unittest.TestCase):
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

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_tree_diagnose_ok(self):
        res = MODULE.diagnose(self.vault)
        self.assertTrue(res["ok"])
        self.assertEqual(2, res["notes"])
        self.assertEqual(1, res["links"])
        self.assertEqual(1, res["expected_links"])
        self.assertEqual([], res["problems"])

    def test_extra_link_diagnosed(self):
        (self.vault / "child" / "sibling.md").write_text(
            '---\nid: "sibling"\ntype: "memory"\nparent: "root"\nstatus: "active"\nauthority: false\n---\n\n# Sibling\n',
            encoding="utf-8",
        )
        (self.vault / "root.md").write_text(
            '---\nid: "root"\ntype: "root"\nparent: ""\nstatus: "active"\nauthority: true\n---\n\n# Root\n\n- [[child/note]]\n- [[child/sibling]]\n',
            encoding="utf-8",
        )
        (self.vault / "child" / "note.md").write_text(
            '---\nid: "note"\ntype: "memory"\nparent: "root"\nstatus: "active"\nauthority: false\n---\n\n# Note\n\n- [[child/sibling]]\n',
            encoding="utf-8",
        )
        res = MODULE.diagnose(self.vault)
        self.assertFalse(res["ok"])
        extra_problems = [p for p in res["problems"] if p["kind"] == "extra_link"]
        self.assertEqual(1, len(extra_problems))
        self.assertEqual("child/note.md", extra_problems[0]["note"])
        self.assertEqual("child/sibling", extra_problems[0]["target"])
        self.assertIn("不是直接子笔记", extra_problems[0]["detail"])

    def test_duplicate_link_diagnosed(self):
        (self.vault / "root.md").write_text(
            '---\nid: "root"\ntype: "root"\nparent: ""\nstatus: "active"\nauthority: true\n---\n\n# Root\n\n- [[child/note]]\n- [[child/note]]\n',
            encoding="utf-8",
        )
        res = MODULE.diagnose(self.vault)
        self.assertFalse(res["ok"])
        dup_problems = [p for p in res["problems"] if p["kind"] == "duplicate_link"]
        self.assertEqual(1, len(dup_problems))
        self.assertEqual("root.md", dup_problems[0]["note"])
        self.assertEqual("child/note", dup_problems[0]["target"])

    def test_unresolved_link_diagnosed(self):
        (self.vault / "root.md").write_text(
            '---\nid: "root"\ntype: "root"\nparent: ""\nstatus: "active"\nauthority: true\n---\n\n# Root\n\n- [[child/missing]]\n',
            encoding="utf-8",
        )
        res = MODULE.diagnose(self.vault)
        self.assertFalse(res["ok"])
        unresolved = [p for p in res["problems"] if p["kind"] == "unresolved_link"]
        self.assertEqual(1, len(unresolved))
        self.assertEqual("root.md", unresolved[0]["note"])
        self.assertEqual("child/missing", unresolved[0]["target"])

    def test_multiple_roots_diagnosed(self):
        (self.vault / "second_root.md").write_text(
            '---\nid: "root2"\ntype: "root"\nparent: ""\nstatus: "active"\nauthority: true\n---\n\n# Root2\n',
            encoding="utf-8",
        )
        res = MODULE.diagnose(self.vault)
        self.assertFalse(res["ok"])
        multi_roots = [p for p in res["problems"] if p["kind"] == "multiple_roots"]
        self.assertEqual(2, len(multi_roots))
        root_notes = {p["note"] for p in multi_roots}
        self.assertEqual({"root.md", "second_root.md"}, root_notes)

    def test_missing_parent_link_diagnosed(self):
        (self.vault / "root.md").write_text(
            '---\nid: "root"\ntype: "root"\nparent: ""\nstatus: "active"\nauthority: true\n---\n\n# Root\n',
            encoding="utf-8",
        )
        res = MODULE.diagnose(self.vault)
        self.assertFalse(res["ok"])
        missing_parent_links = [
            p for p in res["problems"] if p["kind"] == "missing_parent_link"
        ]
        self.assertEqual(1, len(missing_parent_links))
        self.assertEqual("root.md", missing_parent_links[0]["note"])
        self.assertEqual("child/note", missing_parent_links[0]["target"])

    def test_build_error_contains_original_prefix_and_diagnosed_note_path(self):
        (self.vault / "child" / "note.md").write_text(
            '---\nid: "note"\ntype: "memory"\nparent: "root"\nstatus: "active"\nauthority: false\n---\n\n# Note\n\n- [[root]]\n',
            encoding="utf-8",
        )
        with self.assertRaises(RuntimeError) as ctx:
            MODULE.build(self.vault)
        err_msg = str(ctx.exception)
        self.assertTrue(err_msg.startswith("结构边数量错误："))
        self.assertIn("；问题：", err_msg)
        self.assertIn("child/note.md", err_msg)
        self.assertIn("root", err_msg)

    def test_cli_diagnose_mode_creates_no_files(self):
        initial_files = sorted(p.relative_to(self.vault) for p in self.vault.rglob("*"))
        stdout_buf = io.StringIO()
        with patch("sys.argv", ["build_knowlp_index.py", "--vault", str(self.vault), "--diagnose"]):
            with redirect_stdout(stdout_buf):
                exit_code = MODULE.main()

        self.assertEqual(0, exit_code)
        output = json.loads(stdout_buf.getvalue())
        self.assertTrue(output["ok"])
        self.assertEqual([], output["problems"])

        files_after = sorted(p.relative_to(self.vault) for p in self.vault.rglob("*"))
        self.assertEqual(initial_files, files_after)

        (self.vault / "root.md").write_text(
            '---\nid: "root"\ntype: "root"\nparent: ""\nstatus: "active"\nauthority: true\n---\n\n# Root\n\n- [[child/missing]]\n',
            encoding="utf-8",
        )
        initial_files_broken = sorted(p.relative_to(self.vault) for p in self.vault.rglob("*"))
        stdout_broken_buf = io.StringIO()
        with patch("sys.argv", ["build_knowlp_index.py", "--vault", str(self.vault), "--diagnose"]):
            with redirect_stdout(stdout_broken_buf):
                exit_code_broken = MODULE.main()

        self.assertEqual(1, exit_code_broken)
        output_broken = json.loads(stdout_broken_buf.getvalue())
        self.assertFalse(output_broken["ok"])
        self.assertTrue(len(output_broken["problems"]) > 0)

        files_after_broken = sorted(p.relative_to(self.vault) for p in self.vault.rglob("*"))
        self.assertEqual(initial_files_broken, files_after_broken)

    def test_other_diagnostic_kinds(self):
        # missing_id & duplicate_id
        (self.vault / "child" / "noid.md").write_text(
            '---\ntype: "memory"\nparent: "root"\nstatus: "active"\n---\n\n# No ID\n',
            encoding="utf-8",
        )
        (self.vault / "child" / "dupid.md").write_text(
            '---\nid: "note"\ntype: "memory"\nparent: "root"\nstatus: "active"\n---\n\n# Dup ID\n',
            encoding="utf-8",
        )
        res = MODULE.diagnose(self.vault)
        kinds = {p["kind"] for p in res["problems"]}
        self.assertIn("missing_id", kinds)
        self.assertIn("duplicate_id", kinds)

        # missing_parent
        (self.vault / "child" / "orphan.md").write_text(
            '---\nid: "orphan"\nparent: "nonexistent_parent"\nstatus: "active"\n---\n\n# Orphan\n',
            encoding="utf-8",
        )
        res2 = MODULE.diagnose(self.vault)
        kinds2 = {p["kind"] for p in res2["problems"]}
        self.assertIn("missing_parent", kinds2)


if __name__ == "__main__":
    unittest.main()
