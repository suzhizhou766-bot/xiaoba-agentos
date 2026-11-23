"""Temporary-data HTTP harness for manual Chromium review-center acceptance."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
SOURCE = PROJECT / "src"
ASSETS = SOURCE / "xiaoba_agentos" / "review_ui_assets"
INGRESS = PROJECT.parent / "review-ingress"
TOKEN = "TEST-local-browser-harness"

sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(INGRESS))

from fixtures_review import _config  # noqa: E402
from xiaoba_agentos.config import AppConfig  # noqa: E402
from xiaoba_agentos.experience_loop import record_application, record_delivery as _record_delivery  # noqa: E402
from xiaoba_agentos.experience_workflow import trial_summary  # noqa: E402
from xiaoba_agentos.store import EventStore  # noqa: E402
from xiaoba_agentos.validation_basis import validate_basis  # noqa: E402
from xiaoba_review.agent_pool import AgentPool  # noqa: E402
from xiaoba_review.backends import BackendRegistry  # noqa: E402
from xiaoba_review.store import DecisionInbox, ReviewStore  # noqa: E402


def record_delivery(store, value):
    from xiaoba_agentos.delivery import user_decision
    return _record_delivery(store, value, authority=user_decision(value, channel="cli_user_declaration", explicit=True))



def _load_server():
    spec = importlib.util.spec_from_file_location("j11b_browser_review_server", ASSETS / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _file_ref(root: Path, name: str, content: str) -> dict:
    path = root / name
    path.write_text(content, encoding="utf-8")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _admit_authored_candidate(store: EventStore, config: AppConfig, candidate: dict) -> None:
    review_store = ReviewStore(store, config.runtime_dir / "memory-outbox")
    pool = AgentPool(review_store, BackendRegistry(review_store))
    selected = []
    for node_id, kind in (("TEST-value-review", "review"), ("TEST-value-both", "both")):
        source = pool.add({
            "node_id": node_id,
            "name": node_id,
            "agent": node_id,
            "kind": kind,
            "interface": "cli",
            "entry": "/TEST/" + node_id,
            "model": "TEST model " + node_id,
            "agent_type": "other",
        })
        pool.change(node_id, source["node"]["revision"], "toggle", True)
        selected.append(node_id)

    candidate_path = config.runtime_dir / "memory-outbox" / f"{candidate['task_id']}.md"
    request = review_store.create(
        candidate["task_id"], selected[0], selected[1],
        "TEST 人工 method 候选价值评分：两份独立 Agent Pool 合成回执",
        [{"ref": str(candidate_path), "sha256": candidate["evidence_sha256"]}],
        dsh_channel="external_handoff", peer_channel="external_handoff",
        review_type="new_experience", value_review=True,
    )
    for assignment in request["assignments"]:
        card = assignment["card"]
        refs = [item["ref"] for item in card.get("evidence_refs", []) if isinstance(item, dict)]
        receipt = {
            key: card[key] for key in (
                "review_request_id", "assignment_id", "reviewer_role", "plan_revision",
                "reviewer_id", "source_ref", "candidate_sha256", "input_digest", "card_digest",
            )
        }
        receipt.update({
            "receipt_id": "TEST-value-receipt-" + assignment["id"],
            "verdict": "FAIL",
            "findings": "TEST 合成价值评分回执；没有调用模型或宣称用户批准。",
            "evidence_refs": refs,
            "started_at": time.time() - 1,
            "finished_at": time.time(),
            "review_type": "new_experience",
            "score": 8,
            "applicability": "TEST 仅用于隔离浏览器夹具。",
        })
        accepted = review_store.receive(receipt)
        review_store.attest(accepted["id"], accepted["digest"], "TEST 核对合成回执来源")


def _fixture(root: Path):
    config = _config(root)
    raw = json.loads(config.path.read_text(encoding="utf-8"))
    raw["runtime_dir"] = str(root / ".runtime")
    raw["memory"]["knowlp_graph_dir"] = str(root / "graph")
    raw["memory"]["obsidian_vault"] = str(root / "vault")
    raw["memory"]["publish_enabled"] = True
    raw["project_dir"] = str(PROJECT)
    config.path.write_text(json.dumps(raw), encoding="utf-8")
    config = AppConfig.load(config.path)
    vault = Path(config.memory["obsidian_vault"])
    vault.mkdir(parents=True, exist_ok=True)
    root_note = vault / "00_小八AgentOS.md"
    root_note.write_text(
        '---\nid: "root"\ntype: "root"\nparent: ""\nstatus: "active"\nauthority: true\n---\n\n'
        "# TEST root\n\n[[70_复利规则/经验索引]]\n",
        encoding="utf-8",
    )
    catalog = vault / "70_复利规则" / "经验索引.md"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        '---\nid: "experiences"\ntype: "branch"\nparent: "root"\n'
        'status: "active"\nauthority: true\n---\n\n# TEST 经验索引\n',
        encoding="utf-8",
    )
    store = EventStore(config.database_path)
    store.initialize()
    workspace = root / "project"
    workspace.mkdir()

    candidate = store.create_authored_candidate(
        "TEST 真实任务依据浏览器验收", "TEST artifact method claim for browser acceptance",
        config.runtime_dir / "memory-outbox", author="TEST harness", experience_kind="method",
        problem_key="j11b-browser-test",
    )
    _admit_authored_candidate(store, config, candidate)
    store.review_memory(candidate["task_id"], "delivery", "approve", "TEST 初审通过",
                        candidate["evidence_sha256"])

    task_id = store.submit(
        "TEST 在本机临时项目中应用经验方法", 2,
        snapshot={"task_purpose": "user_task", "experience_policy": {
            "version": 1, "kind": "method", "problem_key": "j11b-browser-test",
        }},
    )
    store.complete(task_id, "TEST 临时任务已完成", expected_attempt=0)
    artifact = _file_ref(workspace, "artifact.txt", "TEST delivered artifact v1") | {"version": "v1"}
    execution = _file_ref(workspace, "execution.txt", "TEST actual application execution record")
    delivery = _file_ref(workspace, "delivery.txt", "TEST user accepted the delivered result")
    with store.read_only() as conn:
        initial = conn.execute(
            "SELECT id FROM memory_reviews WHERE task_id=? AND stage='delivery' ORDER BY rowid DESC LIMIT 1",
            (candidate["task_id"],),
        ).fetchone()[0]
    record_application(store, {
        "candidate_id": candidate["task_id"],
        "candidate_sha256": candidate["evidence_sha256"],
        "delivery_review_id": initial,
        "real_task": {"id": task_id, "problem_key": "j11b-browser-test", "task_type": "TEST local artifact task",
                      "description": "TEST applied a reusable method to a completed local task",
                      "workspace": str(workspace), "authorization_ref": "TEST direct user task"},
        "conditions_checked": {"applicability": "TEST same input family", "exclusions": "TEST none omitted",
                               "environment": "TEST temporary local workspace"},
        "method_claims": [{"claim": "TEST inspect the delivered artifact", "class": "artifact"}],
        "artifact": artifact,
        "application_location": str(workspace),
        "steps_applied": "TEST applied the candidate method to the actual task",
        "deviations": "TEST no deviations",
        "execution_evidence": [execution],
    })
    record_delivery(store, {
        "origin": "user", "user_message": "交付",
        "message_id": "TEST-j11b-browser-delivery", "source_ref": "TEST local harness input",
        "real_task_id": task_id, "artifact": artifact, "delivery_record": delivery,
    })
    with store.read_only() as conn:
        basis = validate_basis(conn, store.get_memory_candidate(candidate["task_id"]))
    if basis.get("status") != "proposed" or basis.get("route") != "real_task_acceptance":
        raise RuntimeError("TEST fixture did not produce a proposed real-task basis")

    release = root / "test-release"
    (release / "agentos").mkdir(parents=True)
    (release / "agentos" / "src").symlink_to(SOURCE, target_is_directory=True)
    binding = {
        "ok": True,
        "current_release": "TEST-v1.6.16",
        "config_path": str(config.path),
        "release_dir": str(release),
        "files_manifest_sha256": "TEST-browser-harness-binding",
    }
    verifier = root / "verify-test-release"
    verifier.write_text(
        f"#!{sys.executable}\nimport json\nprint({json.dumps(binding, ensure_ascii=False)!r})\n",
        encoding="utf-8",
    )
    verifier.chmod(0o700)

    review_module = _load_server()

    class HarnessCenter(review_module.Center):
        def verify(self):
            self.binding = binding
            return binding

        def workflow_views(self, task_id=None):
            with store.read_only() as conn:
                rows = conn.execute("SELECT * FROM memory_candidates ORDER BY created_at").fetchall()
                return {row["task_id"]: trial_summary(conn, row) for row in rows
                        if task_id is None or row["task_id"] == task_id}

        def sync(self, force=False):
            self.sync_status = {"ok": True, "state": "TEST-only", "at": "TEST"}

    center = HarnessCenter(root=root, port=0, config_path=config.path)
    center.verifier = verifier
    center.sync_status = {"ok": True, "state": "TEST-only", "at": "TEST"}
    center.binding = binding
    return config, store, candidate, task_id, center


def _status(store: EventStore, candidate_id: str, task_id: str, requests: int, port: int) -> dict:
    with store.read_only() as conn:
        counts = {
            "basis_proposed": conn.execute(
                "SELECT COUNT(*) FROM events WHERE task_id=? AND event_type='memory.validation_basis_proposed'",
                (candidate_id,),
            ).fetchone()[0],
            "basis_confirmed": conn.execute(
                "SELECT COUNT(*) FROM events WHERE task_id=? AND event_type='memory.validation_basis_confirmed'",
                (candidate_id,),
            ).fetchone()[0],
            "final_approved": conn.execute(
                "SELECT COUNT(*) FROM memory_reviews WHERE task_id=? AND stage='final' AND decision='approve'",
                (candidate_id,),
            ).fetchone()[0],
        }
        publication_failures = []
        for row in conn.execute(
            "SELECT sequence,payload_json FROM events WHERE task_id=? AND event_type='memory.publication_failed' ORDER BY sequence",
            (candidate_id,),
        ):
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                payload = {}
            publication_failures.append({
                "sequence": row["sequence"],
                "error": str(payload.get("error") or "")[:500],
            })
        counts["publication_failed"] = len(publication_failures)
        chain = EventStore.verify_event_chain_tx(conn)
        candidate = conn.execute(
            "SELECT status,published_path FROM memory_candidates WHERE task_id=?", (candidate_id,)
        ).fetchone()
        task = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    published_file = bool(candidate and candidate["published_path"]
                          and Path(candidate["published_path"]).is_file())
    return {
        "scope": "TEST fixture only; browser review transaction and temporary vault publication; release verifier stubbed",
        "port": port,
        "candidate_id": candidate_id,
        "real_task_id": task_id,
        "candidate_status": candidate["status"] if candidate else None,
        "published_file": published_file,
        "real_task_status": task["status"] if task else None,
        "event_counts": counts,
        "publication_failures": publication_failures,
        "event_chain": chain,
        "http_requests": requests,
        "accepted": (counts["basis_confirmed"] == 1 and counts["final_approved"] == 1
                     and candidate is not None and candidate["status"] == "published"
                     and published_file and chain.get("ok") is True),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    evidence_path = args.evidence.expanduser().resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="j11b-browser-") as temp:
        root = Path(temp).resolve()
        config, store, candidate, task_id, center = _fixture(root)
        from xiaoba_agentos.review_ui_install import resolve_asset_path
        page_path = resolve_asset_path(ASSETS, "xiaoba-monitor.html")
        html = page_path.read_text(encoding="utf-8")
        html = f'<meta name="xiaoba-page-token" content="{TOKEN}">\n' + html
        request_count = 0
        stop_event = __import__("threading").Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def reply(self, status, payload, content_type="application/json; charset=utf-8"):
                body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "SAMEORIGIN")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                 "img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                self.wfile.write(body)

            def safe_host(self):
                expected = f"127.0.0.1:{self.server.server_port}"
                return self.headers.get("Host") == expected and self.headers.get("Sec-Fetch-Site") != "cross-site"

            def do_GET(self):
                nonlocal request_count
                request_count += 1
                if not self.safe_host():
                    return self.reply(403, {"error": "TEST same-origin only"})
                path = urlparse(self.path)
                if path.path == "/":
                    return self.reply(200, html.encode("utf-8"), "text/html; charset=utf-8")
                if path.path == "/api/review/state":
                    value = center.state()
                    value.pop("csrf", None)
                    return self.reply(200, value)
                if path.path == "/api/review/candidate":
                    ids = parse_qs(path.query).get("id", [])
                    if len(ids) != 1:
                        return self.reply(400, {"error": "TEST candidate id required"})
                    return self.reply(200, center.detail(ids[0]))
                if path.path == "/api/review/retained":
                    return self.reply(200, {"enumerationOk": True, "items": [], "anomalies": [], "problems": []})
                if path.path == "/api/review/frozen":
                    return self.reply(200, {"items": []})
                if path.path == "/api/agent-pool/value-review-queue":
                    items = store.value_review_queue()
                    return self.reply(200, {"ok": True, "items": [{
                        "task_id": item["task_id"],
                        "candidate_sha256": item["candidate_sha256"],
                        "reason": item["reason"],
                        "dispatch": item["dispatch"],
                    } for item in items]})
                if path.path == "/__test__/status":
                    return self.reply(200, _status(store, candidate["task_id"], task_id, request_count,
                                                   self.server.server_port))
                if path.path == "/__test__/stop":
                    self.reply(200, {"ok": True, "stopping": True})
                    from threading import Thread
                    stop_event.set()
                    Thread(target=self.server.shutdown, daemon=True).start()
                    return
                return self.reply(404, {"error": "TEST route not exposed"})

            def do_POST(self):
                nonlocal request_count
                request_count += 1
                origin = f"http://127.0.0.1:{self.server.server_port}"
                if (not self.safe_host() or self.headers.get("Origin") != origin
                        or self.headers.get("X-Xiaoba-Token") != TOKEN):
                    return self.reply(403, {"error": "TEST same-origin token required"})
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024 or not self.headers.get("Content-Type", "").startswith("application/json"):
                    return self.reply(400, {"error": "TEST invalid JSON request"})
                try:
                    value = json.loads(self.rfile.read(length))
                    path = urlparse(self.path).path
                    if path == "/api/review/decisions":
                        return self.reply(200, center.decide(value))
                    if path == "/api/review/sync":
                        center.sync(force=True)
                        return self.reply(200, center.sync_status)
                    return self.reply(404, {"error": "TEST route not exposed"})
                except Exception as exc:
                    return self.reply(400, {"ok": False, "error": f"TEST harness: {type(exc).__name__}: {exc}"})

        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        http.daemon_threads = True
        port = http.server_port
        url = f"http://127.0.0.1:{port}/#review"
        print(json.dumps({"event": "HARNESS_READY", "url": url, "port": port,
                          "candidate_id": candidate["task_id"], "real_task_id": task_id,
                          "config_path": str(config.path), "runtime_dir": str(config.runtime_dir),
                          "evidence_path": str(evidence_path)}, ensure_ascii=False), flush=True)
        from threading import Thread
        Thread(target=http.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        try:
            stop_event.wait()
        finally:
            http.shutdown()
            http.server_close()
            result = _status(store, candidate["task_id"], task_id, request_count, port)
            result.update({"url": url, "config_path": str(config.path), "runtime_dir": str(config.runtime_dir),
                           "candidate_page_sha256": hashlib.sha256(page_path.read_bytes()).hexdigest(),
                           "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            evidence_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"event": "HARNESS_STOPPED", **result}, ensure_ascii=False), flush=True)
        if not result["accepted"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
