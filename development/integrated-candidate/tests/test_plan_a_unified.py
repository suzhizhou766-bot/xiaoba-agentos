"""Plan A unified end-to-end verification tests (A1, A2, A3 + C06/C07 integration)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

CANDIDATE = Path(__file__).resolve().parents[1]
REPO_ROOT = CANDIDATE.parents[1]
INGRESS = CANDIDATE.parent / "review-ingress"
MONITOR_ROOT = REPO_ROOT / "architecture-package-20261027/shared/工具/小八监看"

for p in (str(CANDIDATE / "src"), str(INGRESS), str(CANDIDATE / "scripts"), str(MONITOR_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class PlanADualInsuranceDeliveryTests(unittest.TestCase):
    """Plan A1 + C06 + C07: Monitor OpenReviewModule proxy + Review Center + CLI dual-insurance delivery."""

    def setUp(self):
        import test_card52_reuse_scores as reuse_fx
        self._fx = reuse_fx.Card52ReuseTests()
        self._fx.setUp()
        self.addCleanup(self._fx.tmp.cleanup)
        self.root = self._fx.root
        self.cfg = self._fx.cfg
        self.store = self._fx.store

    def _start_review_backend(self):
        from test_trial_review_center import module
        raw = json.loads(self.cfg.path.read_text(encoding="utf-8"))
        raw["memory"]["knowlp_graph_dir"] = str(self.root / "graph")
        self.cfg.path.write_text(json.dumps(raw), encoding="utf-8")
        server_mod = module("server")
        verifier = self.root / "fake-verifier.py"
        release_dir = self.root / "fake-release"
        (release_dir / "agentos").mkdir(parents=True, exist_ok=True)
        os.symlink(CANDIDATE / "src", release_dir / "agentos/src")
        verifier_payload = {
            "ok": True,
            "current_release": "v1.6.16",
            "config_path": str(self.cfg.path),
            "release_dir": str(release_dir),
            "files_manifest_sha256": "f".ljust(64, "f"),
        }
        verifier_code = (
            "#!" + sys.executable + "\n"
            "import json\n"
            "print(json.dumps(" + repr(verifier_payload) + "))\n"
        )
        verifier.write_text(verifier_code, encoding="utf-8")
        verifier.chmod(0o700)
        center = server_mod.Center(port=0, root=self.root, config_path=self.cfg.path)
        center.verifier = verifier
        center.sync = lambda force=False: None
        httpd = server_mod.Server(("127.0.0.1", 0), server_mod.Handler)
        center.port = httpd.server_port
        center.url = f"http://127.0.0.1:{httpd.server_port}"
        httpd.center = center
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        return httpd.server_port

    def test_monitor_proxy_and_chat_cli_share_idempotency_b19_and_withdrawal_reversal(self):
        from xiaoba_agentos.experience_loop import record_application
        from xiaoba_agentos.reuse_scores import projection_tx
        from monitor_core.modules import overview as ov
        from monitor_core.modules.review import ReviewModule

        formal = self._fx.formal()
        app = self._fx.app(formal)
        record_application(self.store, app)
        port = self._start_review_backend()

        proxy = object.__new__(ReviewModule)
        proxy.id = "review"
        proxy.mode = "managed"
        proxy.port = port
        proxy.process = SimpleNamespace(status=lambda: {"state": "running", "pid": os.getpid(), "restarts": 0})
        proxy.probe_warning = None
        proxy.script = CANDIDATE / "src/xiaoba_agentos/review_ui_assets/server.py"
        proxy.cli = Path("/nonexistent-cli")
        proxy.ctx = SimpleNamespace(log=lambda *a, **kw: None)

        route_paths = {(r.method, r.path): r.handler for r in proxy.routes()}
        self.assertIn(("GET", "/api/review/deliveries"), route_paths)
        self.assertIn(("GET", "/api/review/delivery"), route_paths)
        self.assertIn(("POST", "/api/review/delivery"), route_paths)

        code, list_payload = route_paths[("GET", "/api/review/deliveries")](None)
        self.assertEqual(200, code, list_payload)
        self.assertEqual([app["real_task"]["id"]], [i["task_id"] for i in list_payload["items"]])

        req_get = SimpleNamespace(param=lambda k: app["real_task"]["id"] if k == "id" else "")
        code, detail = route_paths[("GET", "/api/review/delivery")](req_get)
        self.assertEqual(200, code, detail)
        self.assertEqual(["accept"], detail["allowed_decisions"])
        with self.store.read_only() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM events WHERE event_type='task.delivery_recorded'").fetchone()[0])

        bad_post = SimpleNamespace(json=lambda: {"task_id": detail["task_id"], "user_selected": False})
        code, bad_res = route_paths[("POST", "/api/review/delivery")](bad_post)
        self.assertEqual(400, code)
        self.assertFalse(bad_res["ok"])

        accept_pkt = {
            "version": 1,
            "task_id": detail["task_id"],
            "real_task": detail["real_task"],
            "artifact": detail["artifact"],
            "expected": detail["expected"],
            "decision": "accept",
            "user_selected": True,
            "message_id": "ui-msg-001",
            "source_ref": "review-center:ui-msg-001",
        }
        with self.store.read_only() as db:
            reviews_before = db.execute("SELECT COUNT(*) FROM memory_reviews").fetchone()[0]
        code, accepted = route_paths[("POST", "/api/review/delivery")](SimpleNamespace(json=lambda: dict(accept_pkt)))
        self.assertEqual(200, code, accepted)
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual("separate_user_review_required", accepted["experience_approval"])
        self.assertTrue(Path(accepted["archive"]["path"]).is_file())

        code, replayed = route_paths[("POST", "/api/review/delivery")](SimpleNamespace(json=lambda: dict(accept_pkt)))
        self.assertEqual(200, code)
        self.assertTrue(replayed.get("replayed"))

        with self.store.read_only() as db:
            self.assertEqual(1, projection_tx(db)[formal["task_id"]]["score"])
            self.assertEqual(1, ov.permanent_total_ranking(db)["items"][0]["score"])
            self.assertEqual(reviews_before, db.execute("SELECT COUNT(*) FROM memory_reviews").fetchone()[0])

        _, refreshed = route_paths[("GET", "/api/review/delivery")](req_get)
        self.assertIn("withdraw", refreshed["allowed_decisions"])
        withdraw_pkt = {
            "version": 1,
            "task_id": refreshed["task_id"],
            "real_task": refreshed["real_task"],
            "artifact": refreshed["artifact"],
            "expected": refreshed["expected"],
            "decision": "withdraw",
            "user_selected": True,
            "message_id": "ui-msg-002",
            "source_ref": "review-center:ui-msg-002",
        }
        code, withdrawn = route_paths[("POST", "/api/review/delivery")](SimpleNamespace(json=lambda: dict(withdraw_pkt)))
        self.assertEqual(200, code, withdrawn)
        self.assertTrue(withdrawn["ok"], withdrawn)
        with self.store.read_only() as db:
            self.assertEqual(0, projection_tx(db)[formal["task_id"]]["score"])
            self.assertEqual(0, ov.permanent_total_ranking(db)["items"][0]["score"])


class PlanAManifestDecouplingAndParallelTests(unittest.TestCase):
    """Plan A2: Prose-only documentation changes preserve valid code acceptance votes; runtime edits invalidate them."""

    def test_prose_doc_edit_preserves_runtime_acceptance_while_code_edit_rejects(self):
        create_manifest = _load_script("create_manifest_plana", CANDIDATE / "scripts/create_manifest.py")
        build_release = _load_script("build_release_plana", CANDIDATE / "scripts/build_release.py")

        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "source"
            (tree / "src/xiaoba_agentos").mkdir(parents=True)
            (tree / "docs").mkdir(parents=True)
            (tree / "config").mkdir(parents=True)
            (tree / "README.md").write_text("# Xiaoba v1.6.16\nInitial prose.\n", encoding="utf-8")
            (tree / "docs/RELEASE_V1.6.16.md").write_text("# Release Notes\nDraft 1.\n", encoding="utf-8")
            (tree / "docs/DEPLOYMENT.json").write_text('{"schema_version":1}\n', encoding="utf-8")
            (tree / "config/agentos.json").write_text(
                json.dumps({"worker": {"minimum_successful_agents": 2}, "agents": []}),
                encoding="utf-8",
            )
            (tree / "pyproject.toml").write_text('[project]\nname="x"\nversion="1.6.16"\n', encoding="utf-8")
            (tree / ".gitignore").write_text("", encoding="utf-8")
            (tree / "CAPABILITY.json").write_text("{}\n", encoding="utf-8")
            (tree / "src/xiaoba_agentos/core.py").write_text("VALUE = 1\n", encoding="utf-8")

            def rebuild_manifest():
                entries = create_manifest.collect_entries(tree)
                (tree / "SOURCE_MANIFEST.json").write_text(
                    json.dumps({"algorithm": "sha256", "files": entries}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                rt, dc = create_manifest.split_manifest_entries(entries)
                return (
                    build_release.sha256(tree / "SOURCE_MANIFEST.json"),
                    create_manifest.manifest_digest(rt),
                    create_manifest.manifest_digest(dc),
                )

            src_sha_1, rt_sha_1, doc_sha_1 = rebuild_manifest()
            payload = {
                "task": {
                    "id": "acc-1",
                    "status": "completed",
                    "execution_mode": "production",
                    "result": "本回答：小八 AgentOS 双 Agent 裁判\n\nPASS conclusion",
                    "provenance_json": json.dumps({"kind": "agentos_multi_adjudicated", "minimum_successful_agents": 2}),
                    "execution_snapshot_json": json.dumps({
                        "source_manifest_sha256": src_sha_1,
                        "runtime_manifest_sha256": rt_sha_1,
                        "review_context": {"no_work_task": True, "release_acceptance": True},
                    }),
                },
                "runs": [
                    {"agent_name": "node-a", "status": "completed", "output": "FINAL_ACCEPTANCE: PASS\nOK"},
                    {"agent_name": "node-b", "status": "completed", "output": "FINAL_ACCEPTANCE: PASS\nOK"},
                ],
                "memory_candidate": {"status": "pending_delivery_review"},
                "pool_nodes": [
                    {"node_id": "node-a", "enabled": True, "test_passed": True, "independence": ["cli:a", "m1"]},
                    {"node_id": "node-b", "enabled": True, "test_passed": True, "independence": ["cli:b", "m2"]},
                ],
            }
            validated = build_release._validate_acceptance_payload(payload, tree)
            self.assertEqual(["node-a", "node-b"], validated["successful_agents"])

            (tree / "docs/RELEASE_V1.6.16.md").write_text("# Release Notes\nUpdated prose after review!\n", encoding="utf-8")
            (tree / "README.md").write_text("# Xiaoba v1.6.16\nUpdated README prose!\n", encoding="utf-8")
            src_sha_2, rt_sha_2, doc_sha_2 = rebuild_manifest()
            self.assertNotEqual(src_sha_1, src_sha_2)
            self.assertNotEqual(doc_sha_1, doc_sha_2)
            self.assertEqual(rt_sha_1, rt_sha_2)

            validated_after_doc = build_release._validate_acceptance_payload(payload, tree)
            self.assertEqual(src_sha_2, validated_after_doc["source_manifest_sha256"])

            (tree / "src/xiaoba_agentos/core.py").write_text("VALUE = 2\n", encoding="utf-8")
            src_sha_3, rt_sha_3, _ = rebuild_manifest()
            self.assertNotEqual(rt_sha_1, rt_sha_3)
            with self.assertRaisesRegex(RuntimeError, "not bound to the current source manifest"):
                build_release._validate_acceptance_payload(payload, tree)

    def test_parallel_runner_executes_architecture_suite_with_isolated_cwd_and_tmpdir(self):
        runner = _load_script("run_parallel_suites_plana", CANDIDATE / "scripts/run_parallel_suites.py")
        with tempfile.TemporaryDirectory() as log_root:
            report = runner.run_suites_parallel(
                REPO_ROOT,
                ("architecture",),
                python_exe=sys.executable,
                log_dir=Path(log_root),
            )
            self.assertTrue(report["ok"], report)
            self.assertGreaterEqual(report["tests_run"], 40)
            self.assertTrue((Path(log_root) / "architecture-baseline.log").is_file())


class PlanASeatlessPoolAndMediaGuardTests(unittest.TestCase):
    """Plan A3 + C03-03 P2: Seatless replacement by assignment_id/old_source_id + author block + stdio media guard."""

    def test_seatless_replace_by_assignment_and_source_id_and_blocks_author_swap(self):
        from xiaoba_agentos.store import EventStore
        from xiaoba_agentos.task_participants import record_participant
        from xiaoba_review.store import ReviewStore
        from xiaoba_review.resource_stdio import JsonStdioResource
        from fixtures_review import _admit_authored_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            native = EventStore(root / "db.sqlite3")
            native.initialize()
            rs = ReviewStore(native, root / "memory-outbox")
            for nid, kind in (("node-1", "dsh"), ("node-2", "peer"), ("node-3", "peer"), ("author-node", "peer")):
                rs.register_source({"reviewer_id": nid, "kind": kind, "source_ref": f"cli:{nid}", "provenance": "TEST"})
            cand = native.create_authored_candidate("TEST seatless", "TEST body", root / "memory-outbox", author="author-node")
            cand = _admit_authored_candidate(native, cand)
            record_participant(native, cand["task_id"], "author-node", "author")

            req = rs.create(
                cand["task_id"], "node-1", "node-2", "TEST scope",
                [{"ref": "cand", "sha256": cand["evidence_sha256"]}],
                review_type="new_experience",
            )
            first_assignment = req["assignments"][0]

            with self.assertRaisesRegex(ValueError, "作者|干过活"):
                rs.replace_reviewer(req["id"], "author-node", "try swap author", assignment_id=first_assignment["id"])

            after_1 = rs.replace_reviewer(req["id"], "node-3", "node-1 quota exhausted", assignment_id=first_assignment["id"])
            active_sources = {a["source_id"] for a in after_1["assignments"] if a["active"]}
            self.assertEqual({"node-2", "node-3"}, active_sources)

            after_2 = rs.replace_reviewer(req["id"], "node-1", "node-2 timeout", old_source_id="node-2")
            active_sources_2 = {a["source_id"] for a in after_2["assignments"] if a["active"]}
            self.assertEqual({"node-1", "node-3"}, active_sources_2)

            stdio = object.__new__(JsonStdioResource)
            stdio.protocol = "xiaoba-review-v1"
            with self.assertRaisesRegex(ValueError, "missing_materials"):
                stdio.run({"card_digest": "x", "media_review": {"version": 1}})


if __name__ == "__main__":
    unittest.main()
