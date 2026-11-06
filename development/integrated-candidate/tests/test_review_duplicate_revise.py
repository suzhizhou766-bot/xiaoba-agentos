"""卡 47：已退回（revision_requested）的条目不能重复退回。

后端：正文未改时再点「需要修改」不新增裁决、不新增审计记录；
页面：列表和详情给出提示，「需要修改」按钮不可点，仍可驳回。
"""
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from xiaoba_agentos.store import EventStore

PAGE = Path(__file__).resolve().parents[1] / "src/xiaoba_agentos/review_ui_assets/xiaoba-monitor.html"


def render_list_source(html: str) -> str:
    start = html.index("  function renderList(){")
    end = html.index("  function counts(){", start)
    return html[start:end]


class DuplicateReviseStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = EventStore(self.root / "test.sqlite3")
        self.store.initialize()
        self.task_id = self.store.submit("duplicate revise", 1)
        self.store.complete_manually(self.task_id, "result")
        self.staged = self.root / "candidate.md"
        self.staged.write_text("# first draft\n", encoding="utf-8")
        self.sha = hashlib.sha256(self.staged.read_bytes()).hexdigest()
        self.store.stage_memory_candidate(self.task_id, str(self.staged), self.sha)

    def tearDown(self):
        self.tmp.cleanup()

    def _reviews(self):
        with self.store.read_only() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM memory_reviews WHERE task_id=? ORDER BY created_at", (self.task_id,)
            )]

    def _review_events(self):
        with self.store.read_only() as conn:
            return conn.execute(
                "SELECT count(*) FROM events WHERE task_id=? AND event_type='memory.reviewed'",
                (self.task_id,),
            ).fetchone()[0]

    def test_three_revise_calls_only_first_writes_a_decision_and_audit_event(self):
        first = self.store.review_memory(self.task_id, "delivery", "revise", "请补充证据", self.sha)
        self.assertEqual("revision_requested", first["status"])
        second = self.store.review_memory(self.task_id, "delivery", "revise", "请补充证据", self.sha)
        third = self.store.review_memory(self.task_id, "delivery", "revise", "请补充证据", self.sha)

        self.assertEqual("already_revision_requested", second["status"])
        self.assertEqual("already_revision_requested", third["status"])
        record = self._reviews()[0]
        self.assertEqual(first["review_id"], second["revision_requested_review_id"])
        self.assertEqual("请补充证据", second["revision_note"])
        self.assertEqual(record["created_at"], second["revision_requested_at"])
        self.assertEqual(1, len(self._reviews()))
        self.assertEqual(1, self._review_events())
        self.assertEqual("revision_requested", self.store.get_memory_candidate(self.task_id)["status"])

    def test_author_changed_body_then_revise_is_recorded_normally(self):
        first = self.store.review_memory(self.task_id, "delivery", "revise", "请补充证据", self.sha)
        self.staged.write_text("# revised draft\n", encoding="utf-8")
        changed = hashlib.sha256(self.staged.read_bytes()).hexdigest()
        self.store.stage_memory_candidate(self.task_id, str(self.staged), changed)
        second = self.store.review_memory(self.task_id, "delivery", "revise", "请再改一处", changed)

        self.assertEqual("revision_requested", second["status"])
        self.assertNotEqual(first["review_id"], second["review_id"])
        self.assertEqual(2, len(self._reviews()))
        self.assertEqual(2, self._review_events())

    def test_reject_still_works_after_revision_requested(self):
        self.store.review_memory(self.task_id, "delivery", "revise", "调整", self.sha)
        rejected = self.store.review_memory(self.task_id, "delivery", "reject", "不予采用", self.sha)
        self.assertEqual("delivery_rejected", rejected["status"])
        self.assertEqual(2, len(self._reviews()))

    def test_approve_still_works_after_revision_requested(self):
        self.store.review_memory(self.task_id, "delivery", "revise", "调整", self.sha)
        approved = self.store.review_memory(self.task_id, "delivery", "approve", "通过", self.sha)
        self.assertEqual("delivery_approved", approved["status"])
        self.assertEqual(2, len(self._reviews()))


class ReturnedRevisionPageTest(unittest.TestCase):
    def setUp(self):
        self.html = PAGE.read_text(encoding="utf-8")

    @unittest.skipUnless(shutil.which("node"), "node 不可用时只做静态检查")
    def test_returned_item_notice_and_last_time_are_rendered_in_list(self):
        source = render_list_source(self.html)
        script = """
const els={rcSearch:{value:""},rcList:{innerHTML:""}};
const $=id=>els[id]; const esc=s=>String(s); const day=t=>t?"2026-11-05":"d";
const DECISION={}; const saved={}; let selected=null;
let state={items:[
  {task_id:"t1",title:"已退回稿",status:"revision_requested",status_label:"待修改",created_at:0},
  {task_id:"t2",title:"待初审稿",status:"pending_delivery_review",status_label:"待初审",created_at:0}
],reviews:[{task_id:"t1",decision:"revise",created_at:1762300000}]};
""" + source + """
renderList();
console.log(JSON.stringify({html:els.rcList.innerHTML, seq:[...els.rcList.innerHTML.matchAll(/class="rc-seq"[^>]*>([^<]+)</g)].map(m=>m[1])}));
"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20, check=True)
        rendered = json.loads(out.stdout)
        self.assertIn("已退回，等作者修改", rendered["html"])
        self.assertIn("2026-11-05", rendered["html"])
        self.assertEqual(["#1", "#2"], rendered["seq"])
        # 只有已退回那一条带提示；未退回的条目不受影响。
        self.assertEqual(1, rendered["html"].count("已退回，等作者修改"))

    @unittest.skipUnless(shutil.which("node"), "node 不可用时只做静态检查")
    def test_revise_button_is_blocked_but_reject_stays_available(self):
        self.assertIn('id="rcReturned"', self.html)
        self.assertIn('<button class="choice" type="button" data-decision="revise" aria-pressed="false" disabled>',
                      self.html)
        line = re.search(r"^\s*const blocked\s*=\s*returned\s*&&\s*b\.dataset\.decision===\"revise\";\s*$",
                         self.html, re.MULTILINE)
        self.assertIsNotNone(line, "详情里没有按已退回状态禁用「需要修改」按钮")
        script = """
const returned=true;
function blockedFor(decision){ const b={dataset:{decision}}; %s return blocked; }
console.log(JSON.stringify({revise:blockedFor("revise"),reject:blockedFor("reject"),approve:blockedFor("approve")}));
""" % line.group(0).strip()
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20, check=True)
        self.assertEqual({"revise": True, "reject": False, "approve": False}, json.loads(out.stdout))

    def test_returned_notice_text_is_shipped_in_detail(self):
        self.assertIn("已退回，等作者修改", self.html)
        self.assertIn("不能重复退回", self.html)
        self.assertRegex(self.html, r'\$\("rcReturned"\)\.hidden\s*=\s*!returned')


if __name__ == "__main__":
    unittest.main()
