"""The metadata floor is 3.11; production remains the pinned 3.14 interpreter."""
import tempfile
import unittest
from pathlib import Path
import tomllib


class InterpreterMatrixTests(unittest.TestCase):
    def test_supported_floor_matches_required_stdlib(self):
        data=tomllib.loads((Path(__file__).resolve().parents[1]/'pyproject.toml').read_text())
        self.assertEqual('>=3.11',data['project']['requires-python'])
        import hashlib
        import sys
        self.assertGreaterEqual(sys.version_info[:2],(3,11))
        # 卡 66：第三子项改为真实临时文件上的 file_digest，对齐 workday_ledger 读原文件哈希的路径。
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'sample.bin'
            path.write_bytes(b'TEST reference')
            with path.open('rb') as handle:
                digest=hashlib.file_digest(handle,'sha256').hexdigest()
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)


if __name__=='__main__':unittest.main()
