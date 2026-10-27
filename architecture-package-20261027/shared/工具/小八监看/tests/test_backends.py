"""后台并发模块的纯函数测试（2026-10-24）。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor_core.modules import backends as b

DSH = "/Users/x/node /Users/x/DeepSeek Harness/current/apps/cli/lib/bin.js --profile headless --patch /p.yml --json -"
AGY = "/Users/x/.local/bin/agy --input-format stream-json --output-format stream-json --model M"
QODER = "/Users/x/.local/bin/qodercli -p --model m --tools"


class ProcessTest(unittest.TestCase):
    def test_only_non_interactive_backend_runs_are_counted(self):
        rows = [DSH, DSH, AGY, QODER,
                "/Applications/Antigravity.app/Contents/MacOS/Electron",
                "/Users/x/.local/bin/agy",                       # interactive agy
                "/Users/x/.local/bin/qodercli",                  # interactive qoder
                "/Users/x/node /Users/x/DeepSeek Harness/current/apps/cli/lib/bin.js web",
                "/bin/zsh -c agy -p", ""]
        self.assertEqual({"dsh": 2, "agy": 1, "qoder": 1}, b.count_running(rows))


class StatusTest(unittest.TestCase):
    def test_codes_need_status_context(self):
        for text, code in (("HTTP 429", 429), ("status: 404", 404), ("INTERNAL (code 500): x", 500),
                           ("429 Too Many Requests", 429), ("SERVER: 500", 500)):
            self.assertEqual(code, b.status_code(text), text)
        for text in ("worker pid 4290", "line 404", "parsed 500 rows", "", None):
            self.assertIsNone(b.status_code(text), text)

    def test_counts_per_backend_and_keypool(self):
        runs = b.count_runs([("dsh_review", "HTTP 429"), ("agy_review", "API error: INTERNAL (code 500)"),
                             ("qoder_review", "status 404"), ("dsh_review", "line 404"), ("other", "HTTP 429")])
        self.assertEqual({429: 1, 404: 0, 500: 0}, runs["dsh"])
        self.assertEqual({429: 0, 404: 0, 500: 1}, runs["agy"])
        self.assertEqual({429: 0, 404: 1, 500: 0}, runs["qoder"])
        self.assertEqual({429: 2, 404: 1, 500: 0},
                         b.count_keypool([{"status": 429}, {"status": 429}, {"status": 404}, {"status": 200}, {}]))


if __name__ == "__main__":
    unittest.main()
