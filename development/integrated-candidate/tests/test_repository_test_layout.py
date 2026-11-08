"""Repository-wide main guard ordering; no imports or execution of scanned tests."""
import ast
from pathlib import Path
import unittest

EXCLUSIONS: dict[str, str] = {}


def definitions_after_main(text):
    seen = False
    bad = []
    for node in ast.parse(text).body:
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
                    and len(test.comparators) == 1):
                pair = (test.left, test.comparators[0])
                if any(isinstance(a, ast.Name) and a.id == '__name__' and isinstance(b, ast.Constant)
                       and b.value == '__main__' for a, b in (pair, pair[::-1])):
                    seen = True
        elif seen and isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            bad.append(node.lineno)
    return bad


def violations(root):
    result = []
    for path in sorted(root.rglob('*.py')):
        if '.git' in path.parts or not (path.name.startswith('test') or path.name.endswith('_test.py')):
            continue
        if any(path.is_relative_to(root / excluded) for excluded in EXCLUSIONS):
            continue
        for line in definitions_after_main(path.read_text(encoding='utf-8-sig')):
            result.append(f'{path.relative_to(root)}:{line}')
    return result


class RepositoryTestLayoutTests(unittest.TestCase):
    def test_detects_class_and_functions_after_main(self):
        self.assertEqual([3, 4, 5], definitions_after_main(
            "if __name__ == '__main__':\n    pass\nclass Test: pass\ndef f(): pass\nasync def g(): pass\n"))
        self.assertEqual([], definitions_after_main("def f(): pass\nif '__main__' == __name__:\n    f()\n"))

    def test_all_repository_tests_main_guard_last(self):
        self.assertEqual([], violations(Path(__file__).resolve().parents[3]))
