"""卡 07 C07-03：监看前端的纯逻辑反例（node 执行页面真实函数，非真实浏览器）。

覆盖：顶层 item.reason 显示、全局阶段投影角标稳定、迟到轮询/旧缓存不覆盖新依据、
ops 只读投影渲染的“不可用 vs 0”。不是真实用户验收，只是前端逻辑反例。
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1]
        / "src/xiaoba_agentos/review_ui_assets/xiaoba-monitor.html")


def extract_function(source, name):
    m = re.search(rf"^  (?:async )?function {re.escape(name)}\([^\n]*\)\{{[\s\S]*?^  \}}",
                  source, re.MULTILINE)
    if not m:
        raise AssertionError("function not found: " + name)
    return m.group(0)


def extract_top_function(source, name):
    m = re.search(rf"^function {re.escape(name)}\([^\n]*\)\{{[\s\S]*?^\}}",
                  source, re.MULTILINE)
    if not m:
        raise AssertionError("top-level function not found: " + name)
    return m.group(0)


class Card07FrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = PAGE.read_text(encoding="utf-8")
        cls.node = shutil.which("node")

    def run_node(self, script):
        if not self.node:
            self.skipTest("Node.js unavailable")
        result = subprocess.run([self.node, "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def test_top_level_item_reason_is_rendered_in_value_queue(self):
        helper = extract_function(self.source, "refreshValueQueue")
        script = r"""
const TOKEN="T"; let valueQueueBusy=false;
const VALUE_DISPATCH_LABEL={not_started:"尚未派工"};
const queue=[{task_id:"t1",candidate_sha256:"a".repeat(64),reason:"回执缺少必填字段",dispatch:{status:"blocked",reason:"无可用节点"}}];
const fetch=async()=>({ok:true,status:200,json:async()=>({ok:true,items:queue})});
function el(){return {children:[],textContent:"",className:"",style:{},append(...c){this.children.push(...c)},replaceChildren(...c){this.children=[...c]},addEventListener(){}}}
const elements={valueReviewRows:el(),valueReviewSummary:el(),valueReviewMessage:el()};
const document={createElement:el}; const $=id=>elements[id]; async function refresh(){}
""" + helper + r"""
(async()=>{await refreshValueQueue();
  const row=elements.valueReviewRows.children[0];
  const status=row.children[2];
  process.stdout.write(JSON.stringify({statusText:status.textContent,statusChildren:status.children.map(c=>c.textContent)}));
})().catch(e=>{process.stderr.write(String(e.stack||e));process.exit(1)});
"""
        result = self.run_node(script)
        self.assertEqual(result["statusText"], "待价值评分")
        self.assertIn("回执缺少必填字段", result["statusChildren"],
                      "顶层 item.reason 必须显示，且与派工状态分开")

    def test_badge_uses_global_projection_not_local_page_count(self):
        helper = extract_function(self.source, "stageBadgeCount")
        script = helper + r"""
const a=stageBadgeCount({items:[1,2,3]}, {items:[1]}, {items:[1]}, 3);
const b=stageBadgeCount({items:[1,2,3]}, {items:[1]}, {items:[1]}, 1);  // 总览页传入不同局部 n
const c=stageBadgeCount({items:[1,2,3]}, null, null, 3);                // kept/frozen 未加载
const d=stageBadgeCount(null, null, null, 0);
process.stdout.write(JSON.stringify({a,b,c,d}));
"""
        r = self.run_node(script)
        self.assertEqual(r["a"], 5)
        self.assertEqual(r["b"], 5, "切页传入不同局部数量不得改变角标")
        self.assertEqual(r["c"], 3, "kept/frozen 未加载时角标不跳变")
        self.assertEqual(r["d"], 0)

    def test_global_badge_refreshes_without_review_tab_and_preserves_complete_snapshot(self):
        helper = extract_function(self.source, "stageBadgeCount")
        refresh = extract_function(self.source, "refreshBadge")
        script = helper + r"""
const TOKEN='test'; const document={hidden:false};
let badgeSnapshot=null,badgeReadSeq=0,badgeLoading=false,shown=[];
let fail=false, count=2;
function badge(){ shown.push(stageBadgeCount(badgeSnapshot.state,badgeSnapshot.kept,badgeSnapshot.frozen,0)); }
async function api(name){ if(fail&&name==='retained') throw Error('unavailable');
 return {items:Array(name==='state'?count:1).fill('test')}; }
""" + refresh + r"""
(async()=>{await refreshBadge();count=4;await refreshBadge();fail=true;count=8;await refreshBadge();
process.stdout.write(JSON.stringify({shown,pending:badgeSnapshot.state.items.length}));})()
"""
        result=self.run_node(script)
        self.assertEqual([4,6],result['shown'])
        self.assertEqual(4,result['pending'])
        self.assertIn('setInterval(refreshBadge, 15000)',self.source)

    def test_latest_validation_prefers_live_detail_over_stale_state(self):
        helper = extract_function(self.source, "latestValidation")
        script = r"""
const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
""" + helper + r"""
const expected={status:"delivery_approved",evidence_sha256:"e1",updated_at:1};
const item={task_id:"t1",expected,experience_validation:{source:"old-saved"}};
const current={task_id:"t1",expected,experience_validation:{source:"stale-state"}};
const live={task_id:"t1",expected,experience_validation:{source:"fresh-detail"}};
const withLive=latestValidation(item,current,live);
const withoutLive=latestValidation(item,current,null);
const changedExpected=latestValidation(item,{...current,expected:{...expected,updated_at:2}},live);
process.stdout.write(JSON.stringify({withLive:withLive.source,withoutLive:withoutLive.source,changedExpected:changedExpected.source}));
"""
        r = self.run_node(script)
        self.assertEqual(r["withLive"], "fresh-detail", "实时详情优先，旧 state 不得覆盖")
        self.assertEqual(r["withoutLive"], "stale-state")
        self.assertEqual(r["changedExpected"], "old-saved", "版本不匹配时保持原依据，不用旧缓存")

    def test_ops_rendering_distinguishes_unavailable_from_zero(self):
        helpers = "\n".join(extract_top_function(self.source, n) for n in (
            "opsUnavailableText", "opsProblemsHtml", "opsTodayHtml", "opsWeeklyHtml", "opsRankingHtml", "opsB19Html"))
        script = r"""
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
""" + helpers + r"""
const out={
  unavailable: opsTodayHtml({available:false,problems:["数据库不可读"]}),
  zero: opsTodayHtml({available:true,items:[]}),
  weeklyZero: opsWeeklyHtml({available:true,items:[]}),
  ranking: opsRankingHtml({available:true,items:[{source_task_id:"S1",score:3,used:2}]}),
  b19: opsB19Html({available:true,items:[{reviewer:"nodeA",model:"m",consecutive:4}]}),
};
process.stdout.write(JSON.stringify(out));
"""
        r = self.run_node(script)
        self.assertIn("读取不可用", r["unavailable"])
        self.assertIn("今天暂无任务记录", r["zero"])
        self.assertIn("本周尚无合格评分样本", r["weeklyZero"])
        self.assertIn("S1", r["ranking"])
        self.assertIn("连续 4 次不合格", r["b19"])

    def test_delivery_rejection_clears_retry_while_unknown_outcome_keeps_same_packet(self):
        submit = extract_function(self.source, "submitTaskDelivery")
        open_delivery = extract_function(self.source, "openTaskDelivery")
        open_confirmation = extract_function(self.source, "openTaskDeliveryConfirmation")
        script = r"""
let taskDeliverySelection={decision:"accept",packet:null};
let taskDeliveryDetailRequest=0;
const detail={task_id:"task-1",real_task:{workspace:"/tmp"},artifact:{version:"v1"},expected:{version:1}};
let taskDeliveryDetail=detail;
const taskDeliveryPendingPackets=new Map();
let taskDeliveryBusy=false,calls=0,ids=0,submittedPackets=[],queries=[];
const TOKEN="test-token";
const crypto={randomUUID:()=>"id-"+(++ids)};
const elements={
  taskDeliveryConfirm:{disabled:false},taskDeliveryFeedback:{textContent:""},
  taskDeliveryMessage:{textContent:""},taskDeliveryDialog:{close(){},showModal(){}},
  taskDeliveryConfirmFacts:{replaceChildren(){},append(){}},taskDeliveryCaution:{textContent:""}
};
const $=id=>elements[id];
const document={createElement:()=>({textContent:""})};
function deliveryActionLabel(value){return value;}
function renderTaskDeliveryDetail(){}
async function refreshTaskDeliveries(){}
async function api(query){queries.push(query);return detail;}
let nextFetch;
const fetch=async(_url,options)=>{calls++;submittedPackets.push(JSON.parse(options.body));if(nextFetch instanceof Error)throw nextFetch;return nextFetch;};
""" + "\n".join((open_delivery, open_confirmation, submit)) + r"""
async function run(fetchResult){
  taskDeliverySelection={decision:"accept",packet:null};taskDeliveryBusy=false;
  taskDeliveryPendingPackets.clear();calls=0;submittedPackets=[];nextFetch=fetchResult;
  await submitTaskDelivery();
  return {pending:taskDeliveryPendingPackets.get(detail.task_id)||null,feedback:elements.taskDeliveryFeedback.textContent,calls,
    sent:submittedPackets[0],disabled:elements.taskDeliveryConfirm.disabled};
}
(async()=>{
 const http4xx=await run({ok:false,status:409,json:async()=>({ok:false,pending:false,error:"stale version"})});
 await submitTaskDelivery();
 const callsAfterRejection=calls;
 const http200=await run({ok:true,status:200,json:async()=>({ok:false,pending:false,error:"B1 rejected"})});
 const http503=await run({ok:false,status:503,json:async()=>({ok:false,error:"gateway timeout"})});
 const unknown=await run(new Error("connection reset"));
 nextFetch={ok:true,status:200,json:async()=>({ok:false,pending:true,error:"still unresolved"})};
 await submitTaskDelivery();
 const retry={calls,packet:submittedPackets[1],pending:taskDeliveryPendingPackets.get(detail.task_id)};
 await openTaskDelivery("task-1","newer");
 openTaskDeliveryConfirmation(null,true);
 await submitTaskDelivery();
 const reopened={calls,packet:submittedPackets[2],query:queries[0]};
 process.stdout.write(JSON.stringify({http4xx,http200,http503,unknown,retry,reopened,callsAfterRejection}));
})().catch(e=>{process.stderr.write(String(e.stack||e));process.exit(1)});
"""
        result = self.run_node(script)
        self.assertIsNone(result["http4xx"]["pending"])
        self.assertTrue(result["http4xx"]["disabled"])
        self.assertEqual(1, result["callsAfterRejection"])
        self.assertNotIn("暂不确定", result["http4xx"]["feedback"])
        self.assertIsNone(result["http200"]["pending"])
        self.assertNotIn("暂不确定", result["http200"]["feedback"])
        self.assertIsNotNone(result["http503"]["pending"])
        self.assertEqual(1, result["unknown"]["calls"], "不确定响应不能自动重发")
        self.assertEqual("id-7", result["unknown"]["pending"]["message_id"])
        self.assertIn("暂不确定", result["unknown"]["feedback"])
        self.assertEqual(2, result["retry"]["calls"], "只在测试显式调用重试后才发出第二次请求")
        self.assertEqual(result["unknown"]["pending"], result["retry"]["packet"])
        self.assertEqual(result["unknown"]["pending"]["message_id"], result["retry"]["pending"]["message_id"])
        self.assertEqual("delivery?id=task-1&version=v1", result["reopened"]["query"])
        self.assertEqual(result["unknown"]["pending"], result["reopened"]["packet"])
        self.assertEqual(3, result["reopened"]["calls"])

    def test_topo_anchor_is_single_and_unchanged(self):
        # C07-01：锚点必须在权威页面里唯一且不被删改；agent-pool-native.js 由 mount_pool
        # 以该锚点为字节注入点带入，静态页面本身不应包含第二段脚本引用（否则重复注入）。
        self.assertEqual(self.source.count('id="topo"'), 1)
        self.assertIn('<svg id="topo"', self.source)
        self.assertNotIn("agent-pool-native.js", self.source,
                         "静态页不得自带 native 脚本，避免 mount_pool 注入后重复/重叠")


if __name__ == "__main__":
    unittest.main()
