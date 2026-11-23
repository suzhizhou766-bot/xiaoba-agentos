"""卡49 第4步「本版新增五项」：只在切换后的演练现场验证卡 43/44/45/46/47 的新行为。

全部读写都落在现场目录和现场数据库里；不碰线上，不碰验收库，不改候选。
"""

# --- rebound by stage75q ---
"""stage75q helper — rebound from mature v1614-r7 source; paths via STAGE75Q_BIND_JSON."""
from pathlib import Path
import os, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bind import load_bind
_BIND = load_bind()
_BIND.require_ready()
R = _BIND.root
O = _BIND.O
A = _BIND.A
W = _BIND.W
C = _BIND.C
PKG = _BIND.PKG
VERSION = _BIND.version
LIVE = _BIND.live
P = LIVE
os.chdir(R)
# --- end bind header; original body follows with path overrides ---
import os
from pathlib import Path
# Pending v1614-r7 must never consume historical evidence or run before new-BASE preparation.
assert (_BIND.work / 'BASE_READY.json').is_file(), 'STOP: BASE_READY missing'

os.chdir(R)
import hashlib
import json
import logging
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = _BIND.root
C = _BIND.C
REPORT_DIR = _BIND.O
SCENE = json.loads((REPORT_DIR / "SCENE.json").read_text())
PREWORK = json.loads((REPORT_DIR / "40-prework-workbuddy-node.json").read_text())
PKG = Path(SCENE["runtime"]) / f".runtime/releases/{VERSION}"
sys.path[:0] = [str(PKG / "agentos/src"), str(_BIND.source / "monitor")]

from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.health import doctor
from xiaoba_agentos.memory import MemoryBridge
from xiaoba_agentos.review_flow import (execute_review, publish_approved_candidate,
                                        reconcile_failed_reuses)
from xiaoba_agentos.release import release_binding_status
from xiaoba_review.store import ReviewStore, DecisionInbox
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.resource_stdio import json_stdio_factory
from xiaoba_review.monitor import OpenReviewModule
from monitor_core.web import App, Context
from monitor_core import config as monitor_config

PIN = "/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14"
REPORT_PATH = REPORT_DIR / "41-v169-new-behaviors.json"
FAKE_SECRET = "sk-c49REHEARSALfake0000000000"
FAKE_RAW_STDOUT = "C49_REHEARSAL_RAW_STDOUT"
EXPERIENCE_DIR = "70_复利规则/经验"
CATALOG_REL = "70_复利规则/经验索引.md"


def dump(report):
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def note_status(note):
    text = Path(note).read_text(encoding="utf-8")
    return re.search(r'^status:\s*"([^"]+)"', text, re.M).group(1)


# 假 CLI：冷启动 / 报错带假密钥 / 交合格审查回执，三种夹具都写现场工作回执。
RECEIPT_TAIL = r'''
if "XIAOBA_TASK_CARD=" not in stdin:
    sys.exit(2)
path = json.loads(stdin.split("XIAOBA_TASK_CARD=", 1)[1].split("\n", 1)[0])
card = Path(path).read_text()
logpath = Path(re.search(r"^工作回执文件: (.+)$", card, re.M)[1])
taskid = re.search(r"^任务ID: (.+)$", card, re.M)[1]
step = re.search(r"^步骤码: (.+)$", card, re.M)[1]
nonce = re.search(r"^本轮标识: (.+)$", card, re.M)[1]
ctx = json.loads(card.split("## 材料\n", 1)[1].split("\n\n## 范围与回执", 1)[0])
body = ctx["challenge"] if "challenge" in ctx else "RECEIPT_BODY_FROM_" + Path(__file__).name
if not logpath.exists():
    logpath.write_text(card.split("先写以下文件头：\n\n", 1)[1].split("\n文件已存在时", 1)[0])
meta = "任务ID: " + taskid + "\n步骤码: " + step + "\n状态: completed\n"
if step == "REVIEW-01":
    meta += ("审查结论: PASS\n证据引用: TEST rehearsal fixture receipt\n审查类型: new_experience\n"
             "评分: 8\n适用条件: 仅演练现场夹具\n重复经验: \n")
if step == "REVIEW-01":
    body += "\nFINAL_ACCEPTANCE: PASS"
with logpath.open("a") as out:
    out.write("\n<!-- task:" + nonce + " -->\n" + meta + "---\n" + body + "\n<!-- done:" + nonce + " -->\n")
print(json.dumps({"event": "result", "result": {"status": "SUCCESS"}}))
'''

# 卡49 第五轮：正例票必须写 FINAL_ACCEPTANCE: PASS（r3 起宿主补票也查池票正文）；
# 反例票照同一条回执格式只改结论，池节点写 FAIL，用来验证构包拒收。
RECEIPT_TAIL_FAIL = (RECEIPT_TAIL.replace("审查结论: PASS", "审查结论: FAIL")
                     .replace("FINAL_ACCEPTANCE: PASS", "FINAL_ACCEPTANCE: FAIL"))

CLIS = {
    "cold120": "import json,re,sys,time\nfrom pathlib import Path\n"
               "root = Path(__file__).resolve().parent\n"
               "stdin = sys.stdin.read()\n"
               "time.sleep(120)\n" + RECEIPT_TAIL,
    "boom": "import json,sys\nfrom pathlib import Path\n"
            "root = Path(__file__).resolve().parent\n"
            "stdin = sys.stdin.read()\n"
            "print(json.dumps({'event': 'assistant', 'message': {'content': " + repr(FAKE_RAW_STDOUT) + "}}), flush=True)\n"
            "sys.stderr.write('error: unexpected response from upstream secret=" + FAKE_SECRET + "\\n')\n"
            "sys.exit(2)\n",
    "review": "import json,re,sys\nfrom pathlib import Path\n"
              "root = Path(__file__).resolve().parent\n"
              "stdin = sys.stdin.read()\n" + RECEIPT_TAIL,
    "review_no_score": "import json,re,sys\nfrom pathlib import Path\nroot = Path(__file__).resolve().parent\nstdin = sys.stdin.read()\n" + RECEIPT_TAIL.replace("评分: 8\\n", ""),
    "review_fail": "import json,re,sys\nfrom pathlib import Path\n"
                   "root = Path(__file__).resolve().parent\n"
                   "stdin = sys.stdin.read()\n" + RECEIPT_TAIL_FAIL,
}

report = {
    "scene_root": SCENE["root"], "switched_package": str(PKG),
    "package_source_manifest_sha256": sha(PKG / "agentos/SOURCE_MANIFEST.json"),
    "candidate_source_manifest_sha256": sha(C / "SOURCE_MANIFEST.json"),
    "started_at": time.time(), "sections": {}, "ok": False,
}

runtime = Path(SCENE["runtime"])
cfg = AppConfig.load(Path(SCENE["config"]))
native = EventStore(cfg.database_path)
native.initialize()
binding = release_binding_status(cfg)
report["release_binding"] = binding
assert binding["ok"], binding
report["scene_project_dir_manifest_sha256"] = sha(Path(cfg.project_dir) / "SOURCE_MANIFEST.json")

store = ReviewStore(native, cfg.runtime_dir / "memory-outbox", Path(SCENE["root"]) / "agent-workspaces")
backends = BackendRegistry(store, resource_factory=lambda source: json_stdio_factory(source, store.workspace_root))
ctx = Context(monitor_config.load(cfg.runtime_dir / "unused-monitor.json", {"port": 0}), logging.getLogger("card49-specials"), 0)
app = App(ctx)
module = OpenReviewModule(ctx, store, DecisionInbox(False), backends)
app.register(module)
server = app.server()
port = server.server_address[1]
base = "http://127.0.0.1:" + str(port)
ctx.port = port
ctx.hosts = {"127.0.0.1:" + str(port)}
ctx.origins = {base}
http_thread = threading.Thread(target=server.serve_forever)
http_thread.start()


def post(path, value, timeout=600):
    req = urllib.request.Request(base + path, json.dumps(value).encode(), headers={
        "Content-Type": "application/json", "Origin": base, "X-Xiaoba-Token": ctx.page_token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return {"code": response.status, "body": json.loads(response.read())}
    except urllib.error.HTTPError as exc:
        return {"code": exc.code, "body": json.loads(exc.read())}


def pool_node(node_id):
    return next(v for v in module.pool.state()["nodes"] if v["node_id"] == node_id)


def add_fake_node(node_id, entry, limit):
    node = {"node_id": node_id, "name": node_id, "agent": "CODEX", "kind": "review", "interface": "cli",
            "entry": str(entry), "model": "TEST-" + node_id + "-model", "agent_type": "codex"}
    if limit is not None:
        node["no_progress_seconds"] = limit
    added = post("/api/agent-pool/nodes", node)
    assert added["code"] == 200, added
    revision = added["body"]["result"]["node"]["revision"]
    enabled = post("/api/agent-pool/toggle", {"node_id": node_id, "expected_revision": revision, "enabled": True})
    assert enabled["code"] == 200, enabled
    return node_id, enabled["body"]["result"]["node"]["revision"]


def stored_text():
    """现场库里能读到的全部文字（事件、任务、运行记录、Agent 池记录）+ 工作区文件。"""
    chunks = []
    with native.read_only() as db:
        for table in ("tasks", "agent_runs", "events", "ri_sources", "memory_candidates"):
            for row in db.execute("SELECT * FROM " + table):
                chunks.extend(str(item) for item in row if item is not None)
    for path in Path(store.workspace_root).rglob("*"):
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


fake_root = Path(SCENE["root"]) / "fake-cli-c49"
fake_root.mkdir(exist_ok=True)
entries = {}
for name, source in CLIS.items():
    path = fake_root / name
    path.write_text("#!" + PIN + "\n" + source)
    path.chmod(0o700)
    entries[name] = path


def section(name):
    def decorator(fn):
        def wrapped():
            started = time.time()
            try:
                report["sections"][name] = fn()
            except Exception as exc:
                report["sections"][name] = {"error": type(exc).__name__ + ": " + str(exc),
                                            "error_traceback": "".join(
                                                str(line) + "\n" for line in
                                                __import__("traceback").format_exception(type(exc), exc, exc.__traceback__))}
                report["finished_at"] = time.time()
                dump(report)
                raise
            report["sections"][name]["elapsed_seconds"] = round(time.time() - started, 1)
            report["finished_at"] = time.time()
            dump(report)
            print(json.dumps({"section": name, "ok": report["sections"][name].get("ok")}, ensure_ascii=False), flush=True)
        return wrapped
    return decorator


def scene_cli(*argv, cwd=None, timeout=300):
    q = subprocess.run([PIN, "-B", "-m", "xiaoba_agentos.cli", "--config", str(SCENE["config"])] + list(argv),
                       cwd=str(cwd or runtime), env=dict(os.environ, PYTHONPATH=str(PKG / "agentos/src"),
                                                         PYTHONDONTWRITEBYTECODE="1"),
                       capture_output=True, text=True, timeout=timeout)
    payload = None
    if q.stdout.strip().startswith("{"):
        payload = json.loads(q.stdout)
    assert q.returncode == 0, {"argv": list(argv), "stdout": q.stdout[-3000:], "stderr": q.stderr[-3000:]}
    return payload


def leaked(text):
    return {"fake_secret_in_store": FAKE_SECRET in text, "raw_stdout_in_store": FAKE_RAW_STDOUT in text}


# --- 1. 卡43：节点测试期限跟掉线时限走，失败留脱敏错误尾部 ----------------------
@section("card43_node_test_deadline")
def card43():
    out = {"intent": "假 CLI 冷启动 120 秒：掉线时限 1800 秒的节点应通过；未设时限的节点仍按 90 秒期限结束"}
    _, rev = add_fake_node("card49-sp-cold-long", entries["cold120"], 1800)
    started = time.monotonic()
    passed = post("/api/agent-pool/test", {"node_id": "card49-sp-cold-long", "expected_revision": rev,
                                           "test_id": "card63-v1614-r7-sp-cold-long"})
    out["cold_start_120s_with_1800_limit"] = {"wall_seconds": round(time.monotonic() - started, 1),
                                              "response": passed}
    assert passed["code"] == 200 and passed["body"]["result"]["status"] == "passed", passed
    assert out["cold_start_120s_with_1800_limit"]["wall_seconds"] >= 120

    _, rev = add_fake_node("card49-sp-cold-default", entries["cold120"], None)
    started = time.monotonic()
    timed_out = post("/api/agent-pool/test", {"node_id": "card49-sp-cold-default", "expected_revision": rev,
                                             "test_id": "card63-v1614-r7-sp-cold-default"})
    elapsed = round(time.monotonic() - started, 1)
    result = timed_out["body"]["result"] if timed_out["code"] == 200 else timed_out
    out["cold_start_120s_without_limit"] = {"wall_seconds": elapsed, "response": timed_out}
    assert result.get("status") == "failed" and result.get("reason") == "cli_timeout", timed_out
    assert 85 <= elapsed < 115, elapsed

    _, rev = add_fake_node("card49-sp-boom", entries["boom"], 1800)
    failed = post("/api/agent-pool/test", {"node_id": "card49-sp-boom", "expected_revision": rev,
                                           "test_id": "card63-v1614-r7-sp-boom"})
    body = failed["body"]["result"]
    tail = body.get("error_tail") or pool_node("card49-sp-boom")["test"].get("error_tail")
    out["failed_test_error_tail"] = {"response": failed, "error_tail": tail}
    assert body.get("status") == "failed" and body.get("reason") == "cli_failed", failed
    assert tail and FAKE_SECRET not in tail, tail
    page_state = module.pool.state()
    listed = next(v for v in page_state["nodes"] if v["node_id"] == "card49-sp-boom")
    out["agent_pool_page_state"] = {"node_test": listed.get("test"),
                                    "error_tail_visible": bool(listed.get("test", {}).get("error_tail"))}
    assert listed["test"]["error_tail"] == tail
    checks = doctor(cfg, native)
    out["doctor_error_tails"] = checks["review_node_error_tails"]
    assert any(item["node_id"] == "card49-sp-boom" and item["error_tail"] == tail
               for item in checks["review_node_error_tails"]), checks["review_node_error_tails"]
    out["secret_leak_scan"] = leaked(stored_text())
    assert out["secret_leak_scan"] == {"fake_secret_in_store": False, "raw_stdout_in_store": False}, out["secret_leak_scan"]
    out["ok"] = True
    return out


# --- 2. 卡44：宿主补票取证，1 池节点 + 1 宿主被门禁接受；两票都是宿主被拒收 --------
@section("card44_host_backvote")
def card44():
    from xiaoba_agentos.orchestrator import Orchestrator
    import importlib.util

    spec = importlib.util.spec_from_file_location("scene_build_release", PKG / "agentos/scripts/build_release.py")
    build_release = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = build_release
    spec.loader.exec_module(build_release)

    out = {"intent": "现场用 1 个假池节点 + 宿主回执走一次发布验收取证；再把池节点全部关掉走纯宿主票"}
    material = Path(SCENE["root"]) / "specials44"
    material.mkdir(exist_ok=True)
    fixture_material = material / "fixture-material.md"
    fixture_material.write_text("# 卡49 演练夹具材料\n\n这是一份只给现场宿主补票取证用的小材料，没有任何线上内容。\n",
                                encoding="utf-8")
    package_config = json.loads((PKG / "agentos/config/agentos.json").read_text())
    out["minimum_successful_agents"] = package_config["worker"]["minimum_successful_agents"]
    assert out["minimum_successful_agents"] >= 2, out["minimum_successful_agents"]

    # 演练现场的其它假节点先关闭，保证这一轮只有一个合格池审查员，触发宿主补第二票。
    # 第五轮按新 BASE 复核：卡43 建的 card49-sp-* 也是 review 节点且测试通过，一并关掉，
    # 否则 select_reviewers 会挑到两个池节点，走不到宿主补票路径。
    out["disabled_before"] = []
    for node in module.pool.state()["nodes"]:
        if (node["node_id"] != PREWORK["node_id"]
                and node["node_id"].startswith(("card49-fake-", "card49-sp-")) and node.get("enabled")):
            off = post("/api/agent-pool/toggle", {"node_id": node["node_id"], "expected_revision": node["revision"],
                                                  "enabled": False})
            assert off["code"] == 200, off
            out["disabled_before"].append(node["node_id"])

    node_id, revision = add_fake_node("card49-sp-pool1", entries["review"], 1800)
    tested = post("/api/agent-pool/test", {"node_id": node_id, "expected_revision": revision,
                                           "test_id": "card63-v1614-r7-sp-pool1"})
    assert tested["code"] == 200 and tested["body"]["result"]["status"] == "passed", tested
    out["pool_node"] = {"node_id": node_id, "test": tested["body"]["result"]}

    prompt = ("卡49 演练夹具：审查上面这份现场材料，结论写进工作回执。"
              "这只是演练现场的一条假验收任务，不代表对 v1.6.14 的真实结论。\n")
    accepted = scene_cli("submit", prompt, "--release-acceptance", "--no-work-task", "--material", str(fixture_material),
                         "--host-id", "rehearsal-host", "--host-entry", "cli:rehearsal-host",
                         "--host-model", "TEST-rehearsal-host-model")
    task_id = accepted["task_id"]
    out["accept_task_id"] = task_id
    orchestrator = Orchestrator(cfg, native)
    statuses = []
    for _ in range(40):
        step = orchestrator.run_once()
        statuses.append(step.get("status"))
        if step.get("status") in {"awaiting_host", "completed", "failed", "blocked"}:
            break
        time.sleep(1)
    out["orchestrator_statuses"] = statuses
    task = native.get_task(task_id)
    out["task_before_host_vote"] = {"status": task["status"], "attempts": task["attempts"],
                                    "runs": [{"id": r["id"], "agent_name": r["agent_name"], "status": r["status"]}
                                             for r in native.task_runs(task_id)]}
    assert task["status"] == "pending", out["task_before_host_vote"]

    receipt = {"receipt_id": "rehearsal-host-" + task_id, "task_id": task_id,
               "snapshot_sha256": task["execution_snapshot_hash"], "reviewer_id": "rehearsal-host",
               "verdict": "PASS",
               "findings": ("宿主在演练现场实际读过 " + str(fixture_material) + " 后给出的现场结论：夹具材料与题面一致，"
                            "没有发现缺陷。这一票只用于验证发布验收取证的宿主补票门禁，不构成对 v1.6.14 的结论。"),
               "evidence_refs": ["task:" + task_id], "review_type": "new_experience", "score": 8,
               "applicability": "仅演练现场夹具任务", "duplicate_of": None,
               "started_at": time.time() - 30, "finished_at": time.time()}
    receipt_path = material / "host-receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    settled = scene_cli("task-host-complete", task_id, "--receipt", str(receipt_path))
    out["host_complete"] = settled
    task = native.get_task(task_id)
    out["accept_task_after"] = {"status": task["status"], "provenance": json.loads(task["provenance_json"]),
                                "result_head": str(task["result"])[:200]}
    assert task["status"] == "completed", out["accept_task_after"]

    evidence = build_release.validate_acceptance_database(cfg.database_path, task_id, PKG / "agentos")
    out["gate_accept"] = {"conclusions": evidence["conclusions"], "host_vote": evidence["host_vote"],
                          "pool_nodes": [n.get("node_id") for n in evidence["pool_nodes"]],
                          "execution": evidence["execution"], "candidate_status": evidence["candidate_status"]}
    assert [c["source"] for c in evidence["conclusions"]] == ["pool", "host"], out["gate_accept"]
    assert evidence["host_vote"]["pool_run_ids"], out["gate_accept"]
    assert evidence["execution"]["checked_idle_ids"] == ["rehearsal-host"], out["gate_accept"]

    # 两票都是宿主：把唯一那个合格池节点也关掉，再走一次宿主兜底结算。
    off = post("/api/agent-pool/toggle", {"node_id": node_id, "expected_revision": pool_node(node_id)["revision"],
                                          "enabled": False})
    assert off["code"] == 200, off
    out["pool_node_disabled"] = node_id
    refused = scene_cli("submit", prompt, "--release-acceptance", "--no-work-task", "--material", str(fixture_material),
                        "--host-id", "rehearsal-host", "--host-entry", "cli:rehearsal-host",
                        "--host-model", "TEST-rehearsal-host-model")
    refuse_task_id = refused["task_id"]
    out["refuse_task_id"] = refuse_task_id
    receipt2 = dict(receipt, receipt_id="rehearsal-host-" + refuse_task_id, task_id=refuse_task_id,
                    snapshot_sha256=native.get_task(refuse_task_id)["execution_snapshot_hash"],
                    evidence_refs=["task:" + refuse_task_id],
                    started_at=time.time() - 20, finished_at=time.time())
    receipt2["findings"] = ("宿主在演练现场给出的第二张现场结论：仍然只验证门禁，不构成对 v1.6.14 的结论。")
    receipt2_path = material / "host-receipt-no-pool.json"
    assert receipt2["task_id"] == refuse_task_id and receipt2["evidence_refs"] == ["task:" + refuse_task_id]
    assert receipt2["snapshot_sha256"] == native.get_task(refuse_task_id)["execution_snapshot_hash"]
    receipt2_path.write_text(json.dumps(receipt2, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    settled2 = scene_cli("task-host-complete", refuse_task_id, "--receipt", str(receipt2_path), "--user-chose-host")
    task2 = native.get_task(refuse_task_id)
    out["refuse_settlement"] = {"complete": settled2, "provenance": json.loads(task2["provenance_json"]),
                                "status": task2["status"]}
    assert task2["status"] == "completed", out["refuse_settlement"]
    assert json.loads(task2["provenance_json"])["pool_run_ids"] == [], out["refuse_settlement"]
    try:
        build_release.validate_acceptance_database(cfg.database_path, refuse_task_id, PKG / "agentos")
    except RuntimeError as exc:
        out["gate_refuse"] = {"raised": "RuntimeError", "message": str(exc)}
    else:
        raise AssertionError("纯宿主两票没有被构包门禁拒收")

    # 卡 49 第五轮新增（用户 11-07）：池节点那一票是 FAIL 时，构包拒收。
    # 走和正例完全相同的取证路径，只把假池节点换成交 FAIL 回执的那个。
    fail_node_id, fail_rev = add_fake_node("card49-sp-pool-fail", entries["review_fail"], 1800)
    fail_tested = post("/api/agent-pool/test", {"node_id": fail_node_id,
                                                "expected_revision": fail_rev,
                                                "test_id": "card63-v1614-r7-sp-pool-fail"})
    assert fail_tested["code"] == 200 and fail_tested["body"]["result"]["status"] == "passed", fail_tested
    out["pool_fail_node"] = {"node_id": fail_node_id, "test": fail_tested["body"]["result"]}
    fail_vote = scene_cli("submit", prompt, "--release-acceptance", "--no-work-task", "--material", str(fixture_material),
                          "--host-id", "rehearsal-host", "--host-entry", "cli:rehearsal-host",
                          "--host-model", "TEST-rehearsal-host-model")
    fail_task_id = fail_vote["task_id"]
    out["pool_fail_task_id"] = fail_task_id
    fail_statuses = []
    for _ in range(40):
        step = orchestrator.run_once()
        fail_statuses.append(step.get("status"))
        if step.get("status") in {"awaiting_host", "completed", "failed", "blocked"}:
            break
        time.sleep(1)
    out["pool_fail_orchestrator_statuses"] = fail_statuses
    receipt3 = dict(receipt, receipt_id="rehearsal-host-" + fail_task_id, task_id=fail_task_id,
                    snapshot_sha256=native.get_task(fail_task_id)["execution_snapshot_hash"],
                    evidence_refs=["task:" + fail_task_id],
                    started_at=time.time() - 20, finished_at=time.time())
    receipt3["findings"] = ("宿主在演练现场给出的第三张现场结论：只用于验证池票 FAIL 时构包拒收，"
                            "不构成对 v1.6.14 的结论。")
    receipt3_path = material / "host-receipt-pool-fail.json"
    assert receipt3["task_id"] == fail_task_id and receipt3["evidence_refs"] == ["task:" + fail_task_id]
    assert receipt3["snapshot_sha256"] == native.get_task(fail_task_id)["execution_snapshot_hash"]
    receipt3_path.write_text(json.dumps(receipt3, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    settled3 = scene_cli("task-host-complete", fail_task_id, "--receipt", str(receipt3_path))
    task3 = native.get_task(fail_task_id)
    out["pool_fail_settlement"] = {"complete": settled3, "status": task3["status"],
                                   "pool_run_ids": json.loads(task3["provenance_json"]).get("pool_run_ids")}
    assert task3["status"] == "completed", out["pool_fail_settlement"]
    assert out["pool_fail_settlement"]["pool_run_ids"], out["pool_fail_settlement"]
    fail_runs = [r for r in native.task_runs(fail_task_id) if r["agent_name"] == fail_node_id]
    assert len(fail_runs) == 1, fail_runs
    assert out["pool_fail_settlement"]["pool_run_ids"] == [fail_runs[0]["id"]], out["pool_fail_settlement"]
    out["pool_fail_pool_vote"] = {
        "run_id": fail_runs[0]["id"] if fail_runs else None,
        "has_fail_marker": any("FINAL_ACCEPTANCE: FAIL" in (r.get("output") or "") for r in fail_runs),
        "has_pass_marker": any("FINAL_ACCEPTANCE: PASS" in (r.get("output") or "") for r in fail_runs),
    }
    assert (out["pool_fail_pool_vote"]["has_fail_marker"]
            and not out["pool_fail_pool_vote"]["has_pass_marker"]), out["pool_fail_pool_vote"]
    try:
        build_release.validate_acceptance_database(cfg.database_path, fail_task_id, PKG / "agentos")
    except RuntimeError as exc:
        out["gate_refuse_pool_fail"] = {"raised": "RuntimeError", "message": str(exc)}
    else:
        raise AssertionError("池节点那一票是 FAIL 时构包没有被拒收")
    assert fail_runs[0]["id"] in out["gate_refuse_pool_fail"]["message"], out["gate_refuse_pool_fail"]
    assert fail_node_id in out["gate_refuse_pool_fail"]["message"], out["gate_refuse_pool_fail"]

    out["ok"] = True
    return out


# --- 3. 卡45：切换前登记的 workbuddy 节点被标出、测试返回 engine_retired、数据不变 --
@section("card45_retired_workbuddy")
def card45():
    out = {"intent": "现场预放的旧 workbuddy 节点：doctor 标出、测试运行 engine_retired、节点记录逐字节不变"}
    node_id = PREWORK["node_id"]
    checks = doctor(cfg, native)
    retired_warnings = [w for w in checks["warnings"] if isinstance(w, str) and w.strip()]
    out["doctor"] = {"ok": checks["ok"], "retired_warnings": retired_warnings}
    assert checks["ok"] is True, checks["warnings"]
    listed = pool_node(node_id)
    assert listed["node_id"] == node_id, listed
    assert listed.get("name") == PREWORK["submitted_node"]["name"], listed
    # Every retired node must be identified by its stored display name or id.
    # Surrounding warning prose may change without changing the behavior.
    retired_nodes = [v for v in module.pool.state()["nodes"] if v.get("agent_type") == "workbuddy"]
    assert any(v["node_id"] == node_id for v in retired_nodes), retired_nodes
    for retired in retired_nodes:
        label = str(retired.get("name") or retired["node_id"])
        assert any(label in warning for warning in retired_warnings), (label, retired_warnings)
    out["doctor"]["matched_node_id"] = node_id
    out["doctor"]["matched_display_name"] = listed.get("name") or node_id
    out["pool_state_node"] = listed
    assert listed.get("enabled") is True and listed.get("agent_type") == "workbuddy", listed
    tested = post("/api/agent-pool/test", {"node_id": node_id, "expected_revision": listed["revision"],
                                           "test_id": "card63-v1614-r7-sp-workbuddy"})
    body = tested["body"]["result"] if tested["code"] == 200 else tested
    out["test_run"] = {"response": tested}
    assert body.get("status") == "blocked" and body.get("reason") == "engine_retired", tested
    with native.read_only() as db:
        row = db.execute("SELECT data FROM ri_sources WHERE id=?", (node_id,)).fetchone()
        events = [r[0] for r in db.execute(
            "SELECT event_type FROM events WHERE payload_json LIKE ? ORDER BY sequence", ('%' + node_id + '%',))]
    out["data_unchanged"] = {
        "stored_sha256_before_switch": PREWORK["stored"]["sha256"], "stored_sha256_after_switch":
            hashlib.sha256(row[0].encode()).hexdigest(),
        "raw_json_equal": row[0] == PREWORK["stored"]["raw_json"],
        "revision_after": json.loads(row[0])["node"]["revision"],
        "test_record_after": json.loads(row[0])["node"].get("test"),
        "agent_pool_events_after_switch": events,
    }
    assert out["data_unchanged"]["raw_json_equal"], out["data_unchanged"]
    assert out["data_unchanged"]["revision_after"] == PREWORK["stored"]["revision"], out["data_unchanged"]
    new_node_id = "card49-sp-new-workbuddy"
    before_add = json.loads(json.dumps(module.pool.state()["nodes"]))
    assert all(node["node_id"] != new_node_id for node in before_add), before_add
    add_attempt = post("/api/agent-pool/nodes", {"node_id": new_node_id, "name": "新建旧引擎",
                                                 "agent": "WORKBUDDY", "kind": "review", "interface": "cli",
                                                 "entry": "/TEST/bin/workbuddy", "model": "", "agent_type": "workbuddy"})
    out["new_node_is_refused"] = {"code": add_attempt["code"],
                                  "body": json.dumps(add_attempt["body"], ensure_ascii=False)[:600]}
    after_add = module.pool.state()["nodes"]
    out["new_node_is_refused"]["node_absent"] = all(node["node_id"] != new_node_id for node in after_add)
    out["new_node_is_refused"]["pool_state_unchanged"] = after_add == before_add
    assert add_attempt["code"] == 400 and add_attempt["body"].get("ok") is False, add_attempt
    assert out["new_node_is_refused"]["node_absent"], after_add
    assert out["new_node_is_refused"]["pool_state_unchanged"], (before_add, after_add)
    out["ok"] = True
    return out


# --- 4. 卡46：已发布→待复验的经验批准后走恢复路径；驳回后归档 --------------------
@section("card46_experience_recovery")
def card46():
    out = {"intent": "现场造一条显式构造历史已发布后待复验的假经验：批准后走恢复路径；另一条驳回后归档"}
    with native.read_only() as db:
        before_scene_candidates = [tuple(r) for r in db.execute("SELECT * FROM memory_candidates ORDER BY task_id")]
    scratch = Path(SCENE["root"]) / "specials46"
    scratch_runtime = scratch / "runtime"
    vault = scratch / "vault"
    scratch.mkdir()
    scratch_runtime.mkdir()
    (vault / EXPERIENCE_DIR).mkdir(parents=True, exist_ok=True)
    (vault / "root.md").write_text('---\nid: "root"\nparent: ""\nstatus: "active"\n---\n\n# Root\n\n- [['
                                   + str(Path(CATALOG_REL).with_suffix("")) + ']]\n', encoding="utf-8")
    catalog = vault / CATALOG_REL
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text('---\nid: "experiences"\nparent: "root"\nstatus: "active"\n---\n\n# 经验索引\n', encoding="utf-8")
    raw = json.loads(Path(SCENE["config"]).read_text())
    raw.update(project_dir=str(PKG / "agentos"), runtime_dir=str(scratch_runtime))
    raw["memory"].update({
        "publish_enabled": True, "obsidian_vault": str(vault), "knowlp_vault": str(vault),
        "obsidian_subdir": EXPERIENCE_DIR, "catalog_note": CATALOG_REL,
        "knowlp_graph_dir": str(scratch_runtime / "graph"), "knowlp_enabled": True,
        "retire_after_failed_reuses": 2,
    })
    raw["paths"].update(obsidian_vault=str(vault), knowlp_vault=str(vault),
                        knowlp_graph_dir=str(scratch_runtime / "graph"))
    raw["worker"].update(minimum_successful_agents=2)
    scratch_config = scratch / "config.json"
    scratch_config.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sp_cfg = AppConfig.load(scratch_config)
    sp_store = EventStore(sp_cfg.database_path)
    sp_store.initialize()
    bridge = MemoryBridge(sp_cfg, sp_store)
    out["scratch"] = {"config": str(scratch_config), "database": str(sp_cfg.database_path), "vault": str(vault),
                      "publish_enabled": sp_cfg.raw["memory"]["publish_enabled"],
                      "scene_config_publish_enabled": json.loads(Path(SCENE["config"]).read_text())["memory"]["publish_enabled"]}
    assert Path(SCENE["config"]) != scratch_config
    assert json.loads(Path(SCENE["config"]).read_text())["memory"]["publish_enabled"] is False

    def candidate(title, body):
        created = sp_store.create_authored_candidate(title, body, bridge.outbox, author="card49-rehearsal")
        return created["task_id"], Path(created["staged_path"]), created["evidence_sha256"]

    def publish(task_id):
        for stage in ("delivery", "final"):
            code, payload = execute_review(sp_cfg, sp_store, task_id, stage, "approve", "")
            assert code == 0, payload
        row = sp_store.get_memory_candidate(task_id)
        assert row["status"] == "published", row
        return row

    def quarantine(source_task_id, source_note):
        # Explicit legacy fixture, as current test_card46 does: card52 no longer auto-demotes.
        bridge.set_validation_status(source_task_id, str(source_note), "revalidation_required")
        with sp_store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE memory_candidates SET status='revalidation_required' WHERE task_id=?", (source_task_id,))
            sp_store._append_event_tx(db, source_task_id, "rehearsal.legacy_quarantine_fixture", {"fixture": True})
        row = sp_store.get_memory_candidate(source_task_id)
        assert row["status"] == "revalidation_required", row
        assert note_status(row["published_path"]) == "revalidation_required", row
        assert source_note.stem not in catalog.read_text(encoding="utf-8")
        return row

    def state(task_id):
        row = sp_store.get_memory_candidate(task_id)
        note = Path(row["published_path"]) if row["published_path"] else None
        return {"candidate_status": row["status"], "note_path": str(note) if note else None,
                "note_exists": bool(note and note.is_file()),
                "note_status": note_status(note) if note and note.is_file() else None,
                "catalog_links": catalog.read_text(encoding="utf-8").count(note.stem) if note else 0,
                "note_files_in_active_tree": sorted(p.name for p in (vault / EXPERIENCE_DIR).glob("*.md")),
                "archived": sorted(p.name for p in (sp_cfg.runtime_dir / "memory-retired").glob("*.md"))
                if (sp_cfg.runtime_dir / "memory-retired").is_dir() else []}

    recovered_task, _, _ = candidate("卡49 现场恢复测试经验", "正文：先核对再发布（演练夹具）。")
    publish(recovered_task)
    out["recovered"] = {"task_id": recovered_task, "published": state(recovered_task)}
    quarantine(recovered_task, Path(out["recovered"]["published"]["note_path"]))
    out["recovered"]["quarantined"] = state(recovered_task)
    code, payload = execute_review(sp_cfg, sp_store, recovered_task, "final", "approve", "")
    after = state(recovered_task)
    out["recovered"]["after_approve"] = {"code": code, "state": after}
    assert code == 0, payload
    assert after["candidate_status"] == "published" and after["note_status"] == "active", after
    assert after["catalog_links"] == 1, after
    assert after["note_path"] == out["recovered"]["published"]["note_path"], after
    assert payload.get("publication", {}).get("recovered") == 1, payload
    index = json.loads((sp_cfg.runtime_dir / "graph/meta_index.json").read_text(encoding="utf-8"))
    notes = [p for p in (vault / EXPERIENCE_DIR).glob("*-" + recovered_task[:8] + ".md")]
    out["recovered"]["index_hits"] = sum(1 for item in index if item["name"] == Path(after["note_path"]).stem)
    out["recovered"]["note_files"] = [p.name for p in notes]
    assert out["recovered"]["index_hits"] == 1, out["recovered"]
    assert len(notes) == 1, out["recovered"]

    archived_task, _, _ = candidate("卡49 现场驳回归档经验", "正文：这条经验在演练里被驳回（演练夹具）。")
    publish(archived_task)
    archived_note = Path(sp_store.get_memory_candidate(archived_task)["published_path"])
    quarantine(archived_task, archived_note)
    out["archived"] = {"task_id": archived_task, "quarantined": state(archived_task)}
    code, payload = execute_review(sp_cfg, sp_store, archived_task, "final", "reject", "演练现场不再采用")
    rejected = state(archived_task)
    out["archived"]["after_reject"] = {"code": code, "state": rejected}
    assert code == 0, payload
    assert rejected["candidate_status"] == "final_rejected", rejected
    assert rejected["note_exists"] is False and rejected["catalog_links"] == 0, rejected
    assert archived_task + ".md" in rejected["archived"], rejected
    assert bridge.audit_lifecycle([sp_store.get_memory_candidate(archived_task)])["ok"] is True
    again_code, again = execute_review(sp_cfg, sp_store, archived_task, "final", "reject", "演练现场再点一次")
    out["archived"]["repeat_reject"] = {"code": again_code, "state": state(archived_task)}
    assert again_code == 0, again
    assert out["archived"]["repeat_reject"]["state"]["archived"] == rejected["archived"], out["archived"]
    with native.read_only() as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM memory_candidates ORDER BY task_id")] == before_scene_candidates
        scene_candidates = db.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
        scene_rows = db.execute("SELECT task_id, status FROM memory_candidates ORDER BY task_id").fetchall()
    out["scene_database"] = {"note": "本项只在 scratch 副本库里发布经验；现场正式库的经验表不受影响",
                            "scene_memory_candidate_rows": scene_candidates,
                            "scene_memory_candidate_status_values": sorted({r[1] for r in scene_rows})}
    out["ok"] = True
    return out


# --- 5. 卡47：已退回的条目再退回返回 already_revision_requested，不多写审计记录 ----
@section("card47_duplicate_revise")
def card47():
    out = {"intent": "现场库里对已退回的条目连续三次点「需要修改」，只留一条裁决和一条审计事件"}
    bridge = MemoryBridge(cfg, native)
    created = native.create_authored_candidate(
        "卡49 现场重复退回条目", "正文：演练夹具，用来验证重复退回不叠加记录。", bridge.outbox, author="card49-rehearsal")

    task_id = created["task_id"]
    staged = Path(created["staged_path"])
    evidence_sha = created["evidence_sha256"]

    def counts():
        with native.read_only() as db:
            reviews = [dict(r) for r in db.execute(
                "SELECT id,decision,note,created_at FROM memory_reviews WHERE task_id=? ORDER BY created_at", (task_id,))]
            events = db.execute("SELECT COUNT(*) FROM events WHERE task_id=? AND event_type='memory.reviewed'",
                                (task_id,)).fetchone()[0]
        return reviews, events

    first_code, first = execute_review(cfg, native, task_id, "delivery", "revise", "请补充证据")
    second_code, second = execute_review(cfg, native, task_id, "delivery", "revise", "请补充证据")
    third_code, third = execute_review(cfg, native, task_id, "delivery", "revise", "请补充证据")
    reviews, events = counts()
    out["calls"] = [{"code": first_code, "payload": first}, {"code": second_code, "payload": second},
                    {"code": third_code, "payload": third}]
    out["reviews"] = reviews
    out["review_events"] = events
    out["candidate_status"] = native.get_memory_candidate(task_id)["status"]
    assert out["candidate_status"] == "revision_requested", out
    assert len(reviews) == 1 and events == 1, out
    assert first_code == second_code == third_code == 0, out["calls"]
    for duplicate in (second, third):
        assert "already_revision_requested" in json.dumps(duplicate, ensure_ascii=False), duplicate

    rejected_code, rejected = execute_review(cfg, native, task_id, "delivery", "reject", "演练现场不予采用")
    reviews2, events2 = counts()
    out["reject_after_revise"] = {"code": rejected_code, "payload": rejected, "reviews": len(reviews2),
                                  "events": events2,
                                  "candidate_status": native.get_memory_candidate(task_id)["status"]}
    assert rejected_code == 0 and len(reviews2) == 2 and events2 == 2, out["reject_after_revise"]
    assert out["reject_after_revise"]["candidate_status"] == "delivery_rejected", out["reject_after_revise"]
    assert hashlib.sha256(staged.read_bytes()).hexdigest() == evidence_sha
    out["ok"] = True
    return out



@section("card57_host_pool_score_required")
def card57_host_no_score():
    from xiaoba_agentos.orchestrator import Orchestrator
    out = {"intent": "宿主补票池票缺评分必须拒绝，宿主自己的8分不能掩盖池票缺失"}
    for node in module.pool.state()["nodes"]:
        if node["node_id"] != PREWORK["node_id"] and node.get("enabled") and node["node_id"].startswith(("card49-fake-", "card49-sp-")):
            off=post("/api/agent-pool/toggle", {"node_id":node["node_id"], "expected_revision":node["revision"], "enabled":False})
            assert off["code"]==200,off
    nid,rev=add_fake_node("card49-sp-pool-no-score",entries["review_no_score"],1800)
    tested=post("/api/agent-pool/test",{"node_id":nid,"expected_revision":rev,"test_id":"card63-v1614-r7-missing-score"})
    assert tested["code"]==200 and tested["body"]["result"]["status"]=="passed",tested
    accepted=scene_cli("submit","TEST card58 host review missing pool score counterexample", "--no-work-task", "--host-id","rehearsal-host", "--host-entry","cli:rehearsal-host","--host-model","TEST-rehearsal-host-model")
    tid=accepted["task_id"];orchestrator=Orchestrator(cfg,native)
    for _ in range(40):
        result=orchestrator.run_once()
        if result.get("status") in {"awaiting_host","completed","failed","blocked"}:break
        time.sleep(1)
    task=native.get_task(tid);runs=native.task_runs(tid)
    assert task["status"]=="pending" and len(runs)==1 and runs[0]["agent_name"]==nid and runs[0]["status"]=="completed",(task,runs)
    from xiaoba_agentos.pool_dispatch import review_vote_problem
    assert review_vote_problem(runs[0]["output"])=="",runs
    assert not re.search(r"(?im)^\s*(评分|score)\s*[:：]",runs[0]["output"]),runs
    receipt={"receipt_id":"card58-host-"+tid,"task_id":tid,"snapshot_sha256":task["execution_snapshot_hash"],"reviewer_id":"rehearsal-host","verdict":"PASS","findings":"TEST card58 isolated host vote; pool vote lacks score, so the value gate must refuse settlement.","evidence_refs":["task:"+tid],"review_type":"new_experience","score":8,"applicability":"TEST isolated fixture only","duplicate_of":None,"started_at":time.time()-20,"finished_at":time.time()}
    path=Path(SCENE["root"])/"card57-host-no-score.json"
    with path.open('x') as f:json.dump(receipt,f,ensure_ascii=False,indent=2)
    with native.read_only() as db:
        before_events=[tuple(r) for r in db.execute('SELECT * FROM events WHERE task_id=? ORDER BY sequence',(tid,))]
    env=dict(os.environ,PYTHONPATH=str(PKG/'agentos/src'),PYTHONDONTWRITEBYTECODE='1')
    argv=[PIN,'-B','-m','xiaoba_agentos.cli','--config',SCENE['config'],'task-host-complete',tid,'--receipt',str(path)]
    q=subprocess.run(argv,cwd=Path(SCENE['runtime']),env=env,capture_output=True,text=True)
    out.update(task_id=tid,pool_runs=runs,receipt=receipt,argv=argv,exit=q.returncode,stdout=q.stdout,stderr=q.stderr)
    assert q.returncode!=0,out
    response=json.loads(q.stdout)
    assert response.get('status')=='blocked' and isinstance(response.get('error'),str) and response['error'].strip(),out
    assert native.get_task(tid)['status']=='pending' and native.get_memory_candidate(tid) is None,out
    with native.read_only() as db:
        assert [tuple(r) for r in db.execute('SELECT * FROM events WHERE task_id=? ORDER BY sequence',(tid,))]==before_events
    out['ok']=True
    return out

try:
    card43()
    card44()
    card45()
    card46()
    card47()
    card57_host_no_score()
    report["ok"] = True
finally:
    server.shutdown()
    server.server_close()
    http_thread.join()
    backends.close()
    report["secret_leak_scan_after_all"] = leaked(stored_text())
    report["finished_at"] = time.time()
    dump(report)
    print(json.dumps({"ok": report["ok"], "sections": {k: v.get("ok") for k, v in report["sections"].items()},
                      "secret_leak_scan_after_all": report["secret_leak_scan_after_all"]}, ensure_ascii=False), flush=True)
assert report["ok"] is True
