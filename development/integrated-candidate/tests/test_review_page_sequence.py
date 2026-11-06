"""裁决中心列表左上角的序号角标（2026-11-05 用户选定）。

序号是该条在全部待审队列里的位置，搜索过滤时不变；只改显示，不参与裁决。
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "src/xiaoba_agentos/review_ui_assets/xiaoba-monitor.html"


def render_list_source(html: str) -> str:
    start = html.index("  function renderList(){")
    end = html.index("  function counts(){", start)
    return html[start:end]


class ReviewPageSequenceTest(unittest.TestCase):
    def setUp(self):
        self.html = PAGE.read_text(encoding="utf-8")

    def test_badge_style_is_shipped(self):
        self.assertIn(".rc-seq{position:absolute;top:9px;left:12px", self.html)
        self.assertIn(".rc-item{position:relative;padding-top:32px}", self.html)

    def test_numbers_come_from_the_full_queue_not_the_filtered_list(self):
        source = render_list_source(self.html)
        self.assertRegex(source, r"new Map\(state\.items\.map\(\(x,i\)=>\[x\.task_id,i\+1\]\)\)")
        self.assertNotRegex(source, r"(?<!state\.)items\.map\(\(x,i\)")

    @unittest.skipUnless(shutil.which("node"), "node 不可用时只做静态检查")
    def test_search_keeps_queue_numbers(self):
        source = render_list_source(self.html)
        script = """
const els={rcSearch:{value:process.argv[1]||""},rcList:{innerHTML:""}};
const $=id=>els[id]; const esc=s=>String(s); const day=()=>"d";
const DECISION={}; const saved={}; let selected=null;
let state={items:Array.from({length:12},(_,i)=>({task_id:"t"+(i+1),title:"标题"+(i+1)+(i===10?"关键":""),status_label:"待初审",created_at:0}))};
""" + source + """
renderList();
console.log(JSON.stringify([...els.rcList.innerHTML.matchAll(/class="rc-seq"[^>]*>([^<]+)</g)].map(m=>m[1])));
"""
        def run(query):
            out = subprocess.run(["node", "-e", script, query], capture_output=True, text=True, timeout=20, check=True)
            return json.loads(out.stdout)

        full = run("")
        self.assertEqual(full[0], "#01")
        self.assertEqual(full[-1], "#12")
        self.assertEqual(len(full), 12)
        self.assertEqual(run("关键"), ["#11"])


if __name__ == "__main__":
    unittest.main()
