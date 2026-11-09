"""The metadata floor is 3.11; production remains the pinned 3.14 interpreter."""
import unittest
from pathlib import Path
import tomllib


class InterpreterMatrixTests(unittest.TestCase):
    def test_supported_floor_matches_required_stdlib(self):
        data=tomllib.loads((Path(__file__).resolve().parents[1]/'pyproject.toml').read_text())
        self.assertEqual('>=3.11',data['project']['requires-python'])
        import hashlib
        import io
        import sys
        self.assertGreaterEqual(sys.version_info[:2],(3,11))
        # Exercise the actual stdlib API used by workday_ledger, rather than
        # comparing an invented supported-version tuple to itself.
        self.assertEqual(hashlib.sha256(b'TEST reference').hexdigest(),
                         hashlib.file_digest(io.BytesIO(b'TEST reference'),'sha256').hexdigest())


if __name__=='__main__':unittest.main()
