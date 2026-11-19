"""Contract checks for ReviewCenter helpers without a browser dependency.

These tests execute the page's actual JavaScript helper functions with small
DOM mocks. They do not replace a real Chromium/Playwright interaction test.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


PAGE = (Path(__file__).resolve().parents[1]
        / "src/xiaoba_agentos/review_ui_assets/xiaoba-monitor.html")


def extract_function(source, name):
    match = re.search(rf"^  (?:async )?function {re.escape(name)}\([^\n]*\)\{{[\s\S]*?^  \}}",
                      source, re.MULTILINE)
    if not match:
        raise AssertionError(f"function {name} not found in ReviewCenter")
    return match.group(0)


class ReviewUiFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = PAGE.read_text(encoding="utf-8")
        cls.node = shutil.which("node")

    def run_node(self, script):
        if not self.node:
            self.skipTest("Node.js is unavailable for ReviewCenter helper checks")
        result = subprocess.run([self.node, "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def run_helpers(self, names, setup, invocation):
        helpers = "\n".join(extract_function(self.source, name) for name in names)
        return self.run_node(f"""
{setup}
{helpers}
const result = {invocation};
process.stdout.write(JSON.stringify(result));
""")

    @staticmethod
    def basis_item(task_id, status="proposed"):
        return {
            "task_id": task_id,
            "title": "TEST 方法候选 " + task_id,
            "stage": "final",
            "decision": "approve",
            "experience_validation": {
                "ready": status == "confirmed",
                "state": "ready_for_final" if status == "confirmed" else "validation_basis_confirmation_required",
                "validation_basis": {
                    "status": status,
                    "basis_id": "TEST-basis-" + task_id,
                    "basis_sha256": "a" * 64,
                    "route": "real_task_acceptance",
                    "method_scope": {"claim": "TEST 已采用的方法范围"},
                    "required_checks": ["real_task_acceptance"],
                    "method_claims": [{"claim": "TEST 成品处理步骤", "class": "artifact"}],
                    "real_task": {"id": "TEST-real-task-" + task_id, "purpose": "user_task", "status": "completed", "prompt": "TEST 来源任务已完成"},
                    "application_scope": {"description": "TEST 应用方法范围", "steps": ["TEST 应用步骤"]},
                    "application_id": "TEST-application-" + task_id,
                    "delivery_event_id": "TEST-delivery-" + task_id,
                    "artifact": {"path": "TEST://artifact/version-1", "sha256": "b" * 64},
                    "execution_evidence": [{"path": "TEST://execution/record", "sha256": "c" * 64}],
                    "acceptance_ref": "TEST://acceptance/record",
                    "acceptance_sha256": "d" * 64,
                    "evidence_items": [{
                        "label": "TEST 成品",
                        "ref": "TEST://artifact/version-1",
                        "sha256": "b" * 64,
                    }],
                    "accepted_at": "2026-11-12T10:00:00+08:00",
                    "trust_boundary": "TEST 本机现有业务信任边界",
                }
            },
        }

    @staticmethod
    def basis_check(item):
        basis = item["experience_validation"]["validation_basis"]
        return {"checked": True, "dataset": {
            "taskId": item["task_id"], "basisId": basis["basis_id"], "basisSha256": basis["basis_sha256"],
        }}

    def basis_harness(self, items, checks=(), packet_items=None):
        setup = f"""
const items = {json.dumps(items, ensure_ascii=False)};
const checks = {json.dumps(checks, ensure_ascii=False)};
const packetItems = {json.dumps(packet_items, ensure_ascii=False)};
const packet = packetItems === null ? null : {{items: packetItems}};
const root = {{hidden: false}};
const list = {{
  innerHTML: "",
  querySelectorAll() {{
    return [...this.innerHTML.matchAll(/<input class="rc-basis-confirm" type="checkbox" data-task-id="([^"]*)" data-basis-id="([^"]*)" data-basis-sha256="([^"]*)"([^>]*)>/g)]
      .map(([,taskId,basisId,basisSha256,attrs])=>({{dataset:{{taskId,basisId,basisSha256}},checked:attrs.includes(" checked")}}));
  }}
}};
const submit = {{disabled: false}};
const feedback = {{textContent: ""}};
const elements = {{rcBasisConfirmations: root, rcBasisItems: list, rcSubmit: submit, rcFeedback: feedback}};
const $ = id => elements[id];
const esc = value => String(value ?? "").replace(/[&<>\"]/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;",\"\\\"\":\"&quot;\"}}[ch]));
"""
        return self.run_helpers(
            ["basisOf", "needsBasisConfirmation", "basisReady", "basisValue",
             "basisRequirementsMatch", "basisApprovalReady", "basisCheckboxMatches",
             "basisConfirmationMatches", "basisClaimClass", "basisCheckLabel",
             "syncBasisConfirmationState", "renderBasisConfirmations"],
            setup,
            "(() => { renderBasisConfirmations(items,checks); return {hidden:root.hidden, html:list.innerHTML, disabled:submit.disabled, feedback:feedback.textContent}; })()"
        )

    def test_basis_confirmation_renders_full_scope_and_requires_each_proposed_item(self):
        items = [self.basis_item("one"), self.basis_item("two")]
        checks = [self.basis_check(items[0])]
        result = self.basis_harness(items, checks)

        self.assertFalse(result["hidden"])
        self.assertIn("TEST 已采用的方法范围", result["html"])
        self.assertIn("TEST 来源任务已完成", result["html"])
        self.assertIn("TEST://artifact/version-1", result["html"])
        self.assertIn("成品/实际工作", result["html"])
        self.assertIn("TEST-real-task-one", result["html"])
        self.assertIn("TEST-delivery-one", result["html"])
        self.assertIn("我确认所列方法主张的分类及核验路线正确，且这些材料对应已采用的方法范围", result["html"])
        self.assertEqual(result["html"].count("rc-basis-confirm-row"), 2)
        self.assertTrue(result["disabled"], "one candidate checkbox must not confirm a second candidate")

        checks.append(self.basis_check(items[1]))
        confirmed = self.basis_harness(items, checks)
        self.assertFalse(confirmed["disabled"])

    def test_missing_evidence_and_unknown_basis_status_fail_closed(self):
        missing = self.basis_item("missing")
        missing["experience_validation"]["validation_basis"]["evidence_items"] = []
        no_evidence = self.basis_harness([missing])
        self.assertTrue(no_evidence["disabled"])
        self.assertIn("当前无法确认", no_evidence["html"])

        unknown = self.basis_item("unknown", status="future-v99")
        unknown_status = self.basis_harness([unknown])
        self.assertTrue(unknown_status["disabled"])
        self.assertIn("依据状态或路线无法识别", unknown_status["html"])

        missing_claims = self.basis_item("missing-claims")
        del missing_claims["experience_validation"]["validation_basis"]["method_claims"]
        no_claims = self.basis_harness([missing_claims])
        self.assertTrue(no_claims["disabled"])
        self.assertIn("缺少结构化方法主张", no_claims["html"])

    def test_code_claim_explicitly_keeps_sandbox_requirement(self):
        item = self.basis_item("code")
        item["experience_validation"].update(ready=True, state="ready_for_final")
        basis = item["experience_validation"]["validation_basis"]
        basis["status"] = "not_required"
        basis["method_claims"] = [{"claim": "TEST 可断言命令步骤", "class": "code"}]
        basis["route"] = "sandbox"
        basis["required_checks"] = ["sandbox"]
        basis.pop("real_task", None)
        basis.pop("application_scope", None)
        basis.pop("artifact", None)
        basis.pop("acceptance_ref", None)
        basis.pop("acceptance_sha256", None)
        result = self.basis_harness([item])

        self.assertIn("代码/命令", result["html"])
        self.assertIn("隔离沙箱验证", result["html"])
        self.assertIn("隔离沙箱", result["html"])
        self.assertIn("我确认所列方法主张的分类及核验路线正确。", result["html"])
        self.assertNotIn("这些材料对应已采用的方法范围", result["html"])
        self.assertIn("rc-basis-confirm-row", result["html"])
        self.assertNotIn("真实任务证据 · 免隔离试验", result["html"])
        self.assertNotIn("采用时间", result["html"])
        self.assertTrue(result["disabled"])
        self.assertFalse(self.basis_harness([item], [self.basis_check(item)])["disabled"])

    def test_detail_status_and_actions_follow_the_validation_route(self):
        result = self.run_helpers(
            ["validationPresentation"],
            "",
            "(() => {\n"
            "const real=validationPresentation({required:true,ready:false,state:'validation_basis_confirmation_required',validation_basis:{status:'proposed',route:'real_task_acceptance',required_checks:['real_task_acceptance']}},'delivery_approved','final');\n"
            "const combinedPending=validationPresentation({required:true,ready:false,state:'awaiting_trial',validation_basis:{status:'proposed',route:'combined',required_checks:['real_task_acceptance','sandbox']}},'delivery_approved','final');\n"
            "const combinedConfirmed=validationPresentation({required:true,ready:false,state:'validation_basis_confirmation_required',validation_basis:{status:'proposed',route:'combined',required_checks:['real_task_acceptance','sandbox']}},'delivery_approved','final');\n"
            "const sandbox=validationPresentation({required:true,ready:false,state:'awaiting_trial',validation_basis:{status:'not_required',route:'sandbox',required_checks:['sandbox']}},'delivery_approved','final');\n"
            "return {real,combinedPending,combinedConfirmed,sandbox}; })()"
        )

        real = result["real"]
        self.assertEqual(real["stageLabel"], "真实任务证据待确认")
        self.assertEqual(real["panelTitle"], "真实任务证据与应用记录")
        self.assertEqual(real["stateLabel"], "真实任务证据待终审确认")
        self.assertFalse(real["showTrialRequest"])
        self.assertIn("核对真实任务", real["approveHint"])
        self.assertNotIn("隔离", real["approveHint"])

        combined_pending = result["combinedPending"]
        self.assertEqual(combined_pending["stageLabel"], "混合路线 · 隔离验证")
        self.assertTrue(combined_pending["showTrialRequest"])
        self.assertEqual(combined_pending["panelTitle"], "真实任务证据与隔离验证")

        combined_confirmed = result["combinedConfirmed"]
        self.assertEqual(combined_confirmed["stageLabel"], "隔离已满足 · 真实依据待确认")
        self.assertEqual(combined_confirmed["stateLabel"], "隔离验证已满足，真实任务依据待确认")
        self.assertFalse(combined_confirmed["showTrialRequest"])
        self.assertIn("隔离验证已满足", combined_confirmed["approveHint"])

        sandbox = result["sandbox"]
        self.assertEqual(sandbox["panelTitle"], "隔离验证与独立审查")
        self.assertTrue(sandbox["showTrialRequest"])
        self.assertEqual(sandbox["approveHint"], "须先完成隔离执行与独立双审")

    def test_detail_shows_hashed_real_task_basis_materials(self):
        basis = self.basis_item("evidence")["experience_validation"]["validation_basis"]
        result = self.run_helpers(
            ["basisValue", "basisEvidenceText"],
            f"const basis={json.dumps(basis, ensure_ascii=False)};",
            "basisEvidenceText(basis)"
        )

        self.assertIn("TEST 已采用的方法范围", result)
        self.assertIn("TEST 来源任务已完成", result)
        self.assertIn("TEST://artifact/version-1", result)
        self.assertIn("c" * 64, result)
        self.assertIn("TEST 本机现有业务信任边界", result)

    def test_real_task_detail_omits_stale_pre_route_validation_snapshot(self):
        loop = {
            "applications": [{"application_id": "TEST-application"}],
            "deliveries": [{"event_id": "TEST-delivery"}],
            "attachments": [{"validation": {"state": "awaiting_trial",
                                              "reasons": ["尚未关联隔离验证任务"]}}],
        }
        result = self.run_helpers(
            ["experienceLoopText"],
            f"const trial={{validation_basis:{{route:'real_task_acceptance'}},experience_loop:{json.dumps(loop, ensure_ascii=False)}}};",
            "experienceLoopText(trial)"
        )

        self.assertIn("TEST-application", result)
        self.assertIn("TEST-delivery", result)
        self.assertNotIn("尚未关联隔离验证任务", result)

    def test_value_queue_keeps_blocked_failed_and_waiting_items_visible_and_retryable(self):
        helpers = "\n".join(
            extract_function(self.source, name)
            for name in ("refreshValueQueue", "dispatchValueReview")
        )
        setup = r"""
const TOKEN = "TEST_PAGE_TOKEN";
let valueQueueBusy = false;
const VALUE_DISPATCH_LABEL={not_started:"尚未派工",blocked:"等待审查节点",started:"派工中断或待续",failed:"评分失败",waiting_receipts:"等待双审回执"};
const queue = [
  {task_id:"blocked-task",candidate_sha256:"a".repeat(64),dispatch:{status:"blocked",reason:"没有可用审查节点"}},
  {task_id:"failed-task",candidate_sha256:"b".repeat(64),dispatch:{status:"failed",reason:"评分执行失败"}},
  {task_id:"waiting-task",candidate_sha256:"c".repeat(64),dispatch:{status:"waiting_receipts",reason:"尚缺一份回执"}},
];
const postResults = [
  {ok:true,status:200,payload:{ok:true,result:{route:"blocked",reason:"没有可用审查节点"}}},
  {ok:false,status:503,payload:{ok:false,error:"评分执行失败"}},
  {ok:true,status:200,payload:{ok:true,result:{route:"waiting_receipts",reason:"尚缺一份回执"}}},
];
const calls = [];
function response(value){ return {ok:value.ok,status:value.status,json:async()=>value.payload}; }
const fetch = async (url, options={}) => {
  calls.push({url,options});
  if(url === "/api/agent-pool/value-review-queue")
    return response({ok:true,status:200,payload:{ok:true,items:queue}});
  if(url === "/api/agent-pool/value-review-dispatch") return response(postResults.shift());
  throw new Error("unexpected URL: " + url);
};
function makeElement(tag){
  return {tag,children:[],textContent:"",className:"",style:{},dataset:{},disabled:false,
    append(...children){this.children.push(...children);},
    replaceChildren(...children){this.children=[...children];},
    addEventListener(name,callback){this[name]=callback;}};
}
const elements = {
  valueReviewRows:makeElement("tbody"), valueReviewSummary:makeElement("span"),
  valueReviewMessage:makeElement("p"),
};
const $ = id => elements[id];
const document = {createElement:makeElement};
async function refresh(){ return null; }
"""
        script = setup + "\n" + helpers + r"""
(async()=>{
  await refreshValueQueue();
  const rows = elements.valueReviewRows.children;
  if(rows.length !== queue.length) throw new Error("queue did not render: " + JSON.stringify({
    rowCount:rows.length,summary:elements.valueReviewSummary.textContent,
    message:elements.valueReviewMessage.textContent,
  }));
  const initial = rows.map(row=>({
    id:row.children[0].textContent,
    status:row.children[2].textContent,
    dispatch:row.children[3].textContent,
    reason:row.children[3]?.children[0]?.textContent || "",
    button:row.children[4]?.children[0]?.textContent || "",
  }));
  const messages=[], afterRetry=[];
  for(let i=0;i<queue.length;i++){
    const row=elements.valueReviewRows.children[i];
    const button=row?.children?.[4]?.children?.[0];
    if(!button) throw new Error("missing queue action for row " + i + ": " + JSON.stringify(row));
    await dispatchValueReview(queue[i],button);
    messages.push(elements.valueReviewMessage.textContent);
    const refreshed=elements.valueReviewRows.children[i];
    afterRetry.push({
      id:refreshed.children[0].textContent,
      status:refreshed.children[2].textContent,
      dispatch:refreshed.children[3].textContent,
      reason:refreshed.children[3].children[0]?.textContent || "",
      button:refreshed.children[4].children[0].textContent,
    });
  }
  process.stdout.write(JSON.stringify({
    initial,messages,afterRetry,
    posts:calls.filter(call=>call.options.method==="POST").map(call=>({
      url:call.url,headers:call.options.headers,body:JSON.parse(call.options.body),
    })),
    remainingRows:elements.valueReviewRows.children.map(row=>row.children[0].textContent),
  }));
})().catch(error=>{process.stderr.write(String(error.stack||error));process.exit(1);});
"""
        result = self.run_node(script)

        self.assertEqual(
            ["blocked-task", "failed-task", "waiting-task"],
            [item["id"] for item in result["initial"]],
        )
        self.assertEqual(["待价值评分"] * 3, [item["status"] for item in result["initial"]])
        self.assertEqual(["等待审查节点", "评分失败", "等待双审回执"],
                         [item["dispatch"] for item in result["initial"]])
        self.assertEqual(["重试评分", "重试评分", "继续评分"],
                         [item["button"] for item in result["initial"]])
        self.assertIn("没有可用审查节点", result["initial"][0]["reason"])
        self.assertTrue(all("仍留在待价值评分队列" in message for message in result["messages"]),
                        result["messages"])
        self.assertEqual(["待价值评分"] * 3, [item["status"] for item in result["afterRetry"]])
        self.assertEqual(["等待审查节点", "评分失败", "等待双审回执"],
                         [item["dispatch"] for item in result["afterRetry"]])
        self.assertEqual(["重试评分", "重试评分", "继续评分"],
                         [item["button"] for item in result["afterRetry"]])
        self.assertEqual(["blocked-task", "failed-task", "waiting-task"], result["remainingRows"])
        self.assertEqual(3, len(result["posts"]))
        for call, task_id, digest in zip(
            result["posts"], ("blocked-task", "failed-task", "waiting-task"),
            ("a" * 64, "b" * 64, "c" * 64),
        ):
            self.assertEqual("/api/agent-pool/value-review-dispatch", call["url"])
            self.assertEqual("TEST_PAGE_TOKEN", call["headers"]["X-Xiaoba-Token"])
            self.assertEqual({"candidate_id": task_id, "candidate_sha256": digest}, call["body"])

    def test_not_required_is_limited_to_the_pure_sandbox_route(self):
        item = self.basis_item("not-required-artifact", status="not_required")
        result = self.basis_harness([item])

        self.assertTrue(result["disabled"])
        self.assertIn("依据状态或路线无法识别", result["html"])

    def test_awaiting_evidence_cannot_show_approve_confirmation(self):
        item = self.basis_item("awaiting", status="awaiting_evidence")
        basis = item["experience_validation"]["validation_basis"]
        basis["method_claims"] = []
        basis["required_checks"] = []
        basis.pop("basis_id", None)
        basis.pop("basis_sha256", None)
        result = self.basis_harness([item])
        self.assertTrue(result["disabled"])
        self.assertIn("依据状态或路线无法识别", result["html"])
        self.assertNotIn("rc-basis-confirm-row", result["html"])

    def test_mixed_claim_requires_both_routes_and_real_task_confirmation(self):
        item = self.basis_item("mixed")
        basis = item["experience_validation"]["validation_basis"]
        basis["route"] = "combined"
        basis["method_claims"] = [
            {"claim": "TEST 视觉成品处理", "class": "artifact"},
            {"claim": "TEST 自动处理命令", "class": "code"},
        ]
        basis["required_checks"] = ["real_task_acceptance", "sandbox"]
        result = self.basis_harness([item], checks=[self.basis_check(item)])

        self.assertIn("真实任务证据 + 隔离沙箱", result["html"])
        self.assertIn("这些材料对应已采用的方法范围", result["html"])
        self.assertFalse(result["disabled"])

        item["experience_validation"]["state"] = "awaiting_trial"
        result = self.basis_harness([item], checks=[self.basis_check(item)])
        self.assertNotIn("rc-basis-confirm-row", result["html"])
        self.assertTrue(result["disabled"], "classification confirmation must leave sandbox qualification in force")

    def test_previous_real_task_confirmation_still_requires_classification(self):
        item = self.basis_item("confirmed", status="confirmed")
        result = self.basis_harness([item])

        self.assertFalse(result["hidden"])
        self.assertIn("真实任务依据已确认", result["html"])
        self.assertIn("rc-basis-confirm-row", result["html"])
        self.assertIn("我确认所列方法主张的分类及核验路线正确。", result["html"])
        self.assertNotIn("这些材料对应已采用的方法范围", result["html"])
        self.assertTrue(result["disabled"])
        self.assertFalse(self.basis_harness([item], [self.basis_check(item)])["disabled"])

    def test_refresh_carries_confirmation_only_for_the_same_basis_id_and_hash(self):
        updated, untouched = self.basis_item("updated"), self.basis_item("untouched")
        checks = [self.basis_check(updated), self.basis_check(untouched)]
        basis = updated["experience_validation"]["validation_basis"]
        for field, changed in (("basis_id", "TEST-new-basis"), ("basis_sha256", "e" * 64)):
            with self.subTest(field=field):
                previous, basis[field] = basis[field], changed
                result = self.basis_harness([updated, untouched], checks=checks)
                self.assertTrue(result["disabled"])
                self.assertEqual(result["html"].count(" checked>"), 1)
                self.assertFalse(self.basis_harness([updated, untouched],
                                                   checks=[self.basis_check(updated), checks[1]])["disabled"])
                basis[field] = previous

    def test_retry_requires_the_classification_flag_and_exact_basis(self):
        item = self.basis_item("retry")
        basis = item["experience_validation"]["validation_basis"]
        confirmation = {"basis_id": basis["basis_id"], "basis_sha256": basis["basis_sha256"], "confirmed": True}
        packet_items = [{"task_id": item["task_id"], "validation_basis_confirmation": confirmation}]
        result = self.basis_harness([item], checks=[self.basis_check(item)], packet_items=packet_items)
        self.assertTrue(result["disabled"])
        self.assertIn(" disabled>", result["html"])
        self.assertNotIn(" checked", result["html"])

        confirmation["classification_confirmed"] = True
        result = self.basis_harness([item], packet_items=packet_items)
        self.assertFalse(result["disabled"])
        self.assertIn(" checked disabled>", result["html"])

        basis["basis_sha256"] = "e" * 64
        self.assertTrue(self.basis_harness([item], packet_items=packet_items)["disabled"])

    def test_final_approve_is_the_only_new_confirmation_route(self):
        needs = extract_function(self.source, "needsBasisConfirmation")
        basis_of = extract_function(self.source, "basisOf")
        result = self.run_node(f"""
{basis_of}
{needs}
const basis = {{status:"proposed",required_checks:["real_task_acceptance"]}};
const item = {{stage:"final",decision:"approve",experience_validation:{{validation_basis:basis}}}};
process.stdout.write(JSON.stringify({{
  finalApprove:needsBasisConfirmation(item),
  deliveryApprove:needsBasisConfirmation({{...item,stage:"delivery"}}),
  finalReject:needsBasisConfirmation({{...item,decision:"reject"}}),
  confirmed:needsBasisConfirmation({{...item,experience_validation:{{validation_basis:{{...basis,status:"confirmed"}}}}}}),
  sandbox:needsBasisConfirmation({{...item,experience_validation:{{validation_basis:{{status:"not_required",route:"sandbox",required_checks:["sandbox"]}}}}}}),
  missing:needsBasisConfirmation({{...item,experience_validation:{{validation_basis:{{status:"missing"}}}}}}),
  oldContract:needsBasisConfirmation({{stage:"final",decision:"approve",experience_validation:{{kind:"method",state:"ready_for_final"}}}})
}}));
""")
        self.assertEqual(result, {
            "finalApprove": True,
            "deliveryApprove": False,
            "finalReject": False,
            "confirmed": True,
            "sandbox": True,
            "missing": False,
            "oldContract": False,
        })

        # The request only sends a small binding; the evidence display object is
        # not trusted or copied back as user confirmation data.
        self.assertRegex(self.source, re.compile(
            r"item\.validation_basis_confirmation=\{basis_id:basis\.basis_id,"
            r"basis_sha256:basis\.basis_sha256,confirmed:true,classification_confirmed:true\}"
        ))
        self.assertIn("if(!syncBasisConfirmationState(submittedItems())) return;", self.source)

    def test_returned_items_are_folded_by_default_and_search_opens_the_group(self):
        render_list = extract_function(self.source, "renderList")
        setup = """
let state = {items:[
  {task_id:"active",title:"TEST active",status:"pending_delivery_review",status_label:"待初审",created_at:1},
  {task_id:"returned",title:"TEST returned",status:"revision_requested",status_label:"待修改",created_at:2}],reviews:[]};
let selected=null, saved={}, returnedExpanded=false;
let query="";
const list={_html:"",set innerHTML(value){this._html=value},querySelector(){return this._html.includes("<details data-returned-list") ? {open:this._html.includes("<details data-returned-list open"),addEventListener(){}} : null}};
const search={get value(){return query}};
const $=id=>id==="rcList"?list:search;
const esc=value=>String(value??"");
const day=value=>String(value??"");
const decisionName=value=>String(value??"");
"""
        helpers = "\n".join(extract_function(self.source, name) for name in ("renderList",))
        result = self.run_node(f"""
{setup}
{helpers}
renderList(); const collapsed=list._html;
query="待修改"; renderList(); const searched=list._html;
process.stdout.write(JSON.stringify({{collapsed,searched}}));
""")
        self.assertIn("<details data-returned-list>", result["collapsed"])
        self.assertNotIn("<details data-returned-list open", result["collapsed"])
        self.assertIn("TEST returned", result["searched"])
        self.assertIn("<details data-returned-list open", result["searched"])
        self.assertNotIn("TEST active", result["searched"])

    def test_trust_boundary_is_explicit_and_old_rules_remain_visible(self):
        self.assertIn("本机现有业务信任边界", self.source)
        self.assertIn("不构成独立的人类身份认证", self.source)
        self.assertIn('trial.required&&!trial.ready', self.source)
        self.assertIn('x.status==="delivery_approved"', self.source)

    def test_task_delivery_confirmation_requires_second_click(self):
        for element_id in (
            "taskDeliveryPanel", "taskDeliveryList", "taskDeliveryDetail",
            "taskDeliveryAccept", "taskDeliveryWithdraw", "taskDeliveryDialog",
            "taskDeliveryConfirm",
        ):
            self.assertEqual(1, self.source.count(f'id="{element_id}"'), element_id)

        confirm = extract_function(self.source, "openTaskDeliveryConfirmation")
        submit = extract_function(self.source, "submitTaskDelivery")
        self.assertIn('openTaskDeliveryConfirmation("accept")', self.source)
        self.assertIn('openTaskDeliveryConfirmation("withdraw")', self.source)
        self.assertIn('$("taskDeliveryConfirm").addEventListener("click",submitTaskDelivery)', self.source)
        self.assertIn('$("taskDeliveryDialog").showModal()', confirm)
        self.assertNotIn("fetch(", confirm)
        self.assertIn('fetch("/api/review/delivery"', submit)
        self.assertEqual(1, self.source.count('fetch("/api/review/delivery"'))

    def test_all_inline_scripts_parse_in_node(self):
        if not self.node:
            self.skipTest("Node.js is unavailable for inline script syntax checks")
        self.run_node(f"""
const fs=require("fs");
const html=fs.readFileSync({json.dumps(str(PAGE))},"utf8");
const scripts=[...html.matchAll(/<script[^>]*>([\\s\\S]*?)<\\/script>/gi)].map(match=>match[1]).filter(text=>text.trim());
if(!scripts.length) throw new Error("no inline scripts found");
scripts.forEach((script,index)=>new Function(script));
process.stdout.write(JSON.stringify({{parsed:scripts.length}}));
""")


if __name__ == "__main__":
    unittest.main()
