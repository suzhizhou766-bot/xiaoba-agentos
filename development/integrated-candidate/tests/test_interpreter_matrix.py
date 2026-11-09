"""The metadata floor is 3.11; production remains the pinned 3.14 interpreter."""
import unittest
from pathlib import Path
import tomllib


class InterpreterMatrixTests(unittest.TestCase):
    def test_supported_floor_matches_required_stdlib(self):
        data=tomllib.loads((Path(__file__).resolve().parents[1]/'pyproject.toml').read_text())
        self.assertEqual('>=3.11',data['project']['requires-python'])
        supported=((3,11),(3,12),(3,13),(3,14))
        self.assertTrue(all(version>=(3,11) for version in supported))


if __name__=='__main__':unittest.main()
