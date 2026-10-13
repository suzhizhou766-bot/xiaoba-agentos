"""Keep the release gate's baseline test markers auditable."""

import ast
import json
import unittest
from pathlib import Path


TESTS_ROOT = Path(__file__).resolve().parent
MARKS = TESTS_ROOT / "guardrail_marks.json"


class GuardrailMarksTest(unittest.TestCase):
    def test_every_baseline_has_an_explicit_test_status(self):
        data = json.loads(MARKS.read_text(encoding="utf-8"))
        self.assertEqual(1, data["schema_version"])
        baselines = data["baselines"]
        self.assertEqual({f"B{number:02d}" for number in range(1, 25)}, set(baselines))
        for baseline_id, entry in baselines.items():
            with self.subTest(baseline=baseline_id):
                self.assertTrue(entry["name"])
                self.assertEqual(sorted(set(entry["tests"])), entry["tests"])
                self.assertEqual(
                    "guarded" if entry["tests"] else "no_automated_test",
                    entry["status"],
                )

    def test_marked_functions_exist_in_this_candidate(self):
        baselines = json.loads(MARKS.read_text(encoding="utf-8"))["baselines"]
        marked = {test for entry in baselines.values() for test in entry["tests"]}
        existing = set()
        for path in TESTS_ROOT.glob("test_*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                    existing.add(f"{path.name}::{node.name}")
        self.assertFalse(marked - existing, f"stale guardrail marks: {sorted(marked - existing)}")
        self.assertEqual(
            {"B01", "B04", "B07", "B24"},
            {key for key, entry in baselines.items() if not entry["tests"]},
        )
        self.assertEqual("partial", baselines["B23"]["coverage"])
        self.assertEqual(["MG-009"], baselines["B23"]["tested_rules"])
        self.assertEqual(["MG-002", "MG-010"], baselines["B23"]["untested_rules"])


if __name__ == "__main__":
    unittest.main()
