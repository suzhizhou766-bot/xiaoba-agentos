"""Counterexamples for dispatching from the Agent pool instead of fixed names."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaoba_agentos.adapters import AgentResult
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.pool_dispatch import (
    SHORTAGE, release_acceptance, select_reviewers,
)
from xiaoba_agentos.recovery import classify_failure
from xiaoba_agentos.store import EventStore


def _config(root, agents):
    template = Path(__file__).resolve().parents[1] / "config" / "agentos.json"
    raw = json.loads(template.read_text(encoding="utf-8"))
    raw["runtime_dir"] = str(root / "runtime")
    raw["agents"] = agents
    raw["memory"]["knowlp_enabled"] = False
    raw["memory"]["publish_enabled"] = False
    path = root / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return AppConfig.load(path)


def _source(node_id, entry, model, kind="review", tested=True):
    return {
        "reviewer_id": node_id,
        "kind": kind,
        "source_ref": "agent-entry:" + node_id,
        "node": {
            "version": 1, "revision": 1, "enabled": True, "removed": False,
            "name": node_id, "agent": node_id, "interface": "cli",
            "entry": entry, "model": model, "entry_program": entry,
            "agent_type": "other",
            "test": {"status": "passed" if tested else "failed"},
            "adapter_type": "mock",
            "template": "任务 {prompt} 由 " + node_id + " 验证完成",
        },
    }


def _seed(store, sources):
    with store.connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS ri_sources (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        for source in sources:
            conn.execute(
                "INSERT OR REPLACE INTO ri_sources(id, data) VALUES (?, ?)",
                (source["reviewer_id"], json.dumps(source, ensure_ascii=False)),
            )


class _FakePoolResource:
    def __init__(self, source, workspace=None, **_k):
        self.source = source
        self.workspace = workspace

    def run_task(self, task_id, step_code, instruction, materials):
        name = str(self.source.get("reviewer_id") or "node")
        entry = Path(str((self.source.get("node") or {}).get("entry") or ""))
        if entry.is_file():
            marker = Path(str(entry) + ".called")
            marker.write_text(marker.read_text() + "called\n" if marker.exists() else "called\n")
            if Path(str(entry) + ".no-receipt").exists():
                return {"status": "running", "body": ""}
        return {
            "status": "completed",
            "body": "RECEIPT_BODY_FROM_" + name,
            "execution_evidence": {"provider": "fake", "route_source": "test"},
        }


def _fake_factory(source, workspace=None, **_k):
    return _FakePoolResource(source, workspace)


def _patch_pool_cli():
    return patch("xiaoba_agentos.adapters._pool_cli_factory", return_value=_fake_factory)


LEGACY = [
    {"name": "agy_review", "type": "local_cli", "driver": "agy", "cli_route": "local_agy", "enabled": True, "timeout_seconds": 30, "role": "historical"},
    {"name": "dsh_review", "type": "local_cli", "driver": "dsh", "cli_route": "local_dsh", "enabled": True, "timeout_seconds": 30, "role": "historical"},
    {"name": "qoder_review", "type": "local_cli", "driver": "qoder", "cli_route": "local_qoder", "enabled": True, "timeout_seconds": 30, "standby": True, "role": "historical"},
]


class PoolDispatchTest(unittest.TestCase):
    def test_empty_pool_does_not_call_legacy_names_and_keeps_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root, LEGACY)
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("需要审查", 2, snapshot={"prompt": "需要审查", "needs_review": True})
            calls = []
            with patch("xiaoba_agentos.orchestrator.release_binding_status", return_value={"ok": True}), \
                 patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=lambda agent: calls.append(agent.name)):
                result = Orchestrator(config, store).run_once()
            self.assertEqual("blocked_review_nodes", result["status"])
            self.assertIn("不足 2 个", result["error"])
            self.assertEqual([], calls)
            self.assertEqual("pending", store.get_task(task_id)["status"])
            self.assertEqual(0, store.get_task(task_id)["attempts"])

    def test_task_without_review_completes_on_the_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root, LEGACY)
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("不用审查", 2, snapshot={"prompt": "不用审查", "needs_review": False})
            calls = []
            with patch("xiaoba_agentos.orchestrator.release_binding_status", return_value={"ok": True}), \
                 patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=lambda agent: calls.append(agent.name)):
                result = Orchestrator(config, store).run_once()
            self.assertEqual("awaiting_host", result["status"])
            self.assertTrue(result["host"])
            self.assertEqual([], calls)
            self.assertEqual("pending", store.get_task(task_id)["status"])
            self.assertNotIn("宿主完成", str(result.get("result") or ""))

    def test_two_independent_nodes_review_without_legacy_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root, [])
            store = EventStore(config.database_path)
            store.initialize()
            _seed(store, [
                _source("north-node", "cli:codex", "alpha"),
                _source("south-node", "cli:claude", "beta"),
            ])
            task_id = store.submit("审查走通", 2, snapshot={"prompt": "审查走通", "needs_review": True})
            with patch("xiaoba_agentos.orchestrator.release_binding_status", return_value={"ok": True}), \
                 _patch_pool_cli():
                result = Orchestrator(config, store).run_once()
            self.assertEqual("completed", result["status"])
            self.assertEqual(task_id, result["task_id"])
            names = {item["agent_name"] for item in store.latest_agent_runs(task_id).values()}
            self.assertEqual({"north-node", "south-node"}, names)
            self.assertFalse(any("agy" in name or "dsh" in name for name in names))

    def test_same_entry_and_model_are_not_independent(self):
        nodes = [
            {"node_id": "one", "role": "review", "interface": "cli", "ready_review": True,
             "independence": ("cli:codex", "same"), "enabled": True, "test_passed": True},
            {"node_id": "two", "role": "review", "interface": "cli", "ready_review": True,
             "independence": ("cli:codex", "same"), "enabled": True, "test_passed": True},
        ]
        decision = select_reviewers(nodes)
        self.assertEqual("blocked", decision["route"])
        self.assertIn("不派", decision["message"])

    def test_executor_is_not_sent_to_review(self):
        nodes = [
            {"node_id": "worked", "role": "both", "interface": "cli", "ready_review": True,
             "independence": ("cli:codex", "a"), "enabled": True, "test_passed": True},
            {"node_id": "fresh", "role": "review", "interface": "cli", "ready_review": True,
             "independence": ("cli:claude", "b"), "enabled": True, "test_passed": True},
            {"node_id": "other", "role": "review", "interface": "cli", "ready_review": True,
             "independence": ("cli:qoder", "c"), "enabled": True, "test_passed": True},
        ]
        decision = select_reviewers(nodes, exclude_ids=["worked"])
        self.assertEqual({"fresh", "other"}, {item["node_id"] for item in decision["reviewers"]})

    def test_collaborator_cannot_also_review(self):
        nodes = [
            {"node_id": "collab", "role": "both", "interface": "cli", "ready_review": True,
             "independence": ("cli:codex", "a"), "enabled": True, "test_passed": True},
            {"node_id": "only", "role": "review", "interface": "cli", "ready_review": True,
             "independence": ("cli:claude", "b"), "enabled": True, "test_passed": True},
        ]
        decision = select_reviewers(nodes, exclude_ids=["collab"])
        self.assertNotEqual("run", decision["route"])
        self.assertNotIn("collab", {item["node_id"] for item in decision["reviewers"]})

    def test_stale_attempt_cannot_settle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root, [])
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("结算", 3)
            leased = store.lease_next(30)
            self.assertEqual(1, leased["attempts"])
            with self.assertRaises(ValueError):
                store.complete(task_id, "太早的尝试", expected_attempt=0)
            with self.assertRaises(ValueError):
                store.fail(task_id, "太早的尝试", expected_attempt=0)
            self.assertEqual("running", store.get_task(task_id)["status"])
            store.complete(task_id, "这一次", expected_attempt=1)
            self.assertEqual("completed", store.get_task(task_id)["status"])

    def test_permission_denied_is_not_upstream(self):
        classified = classify_failure(PermissionError("operation not permitted"))
        self.assertEqual("permission_denied", classified.name)
        self.assertFalse(classified.external)

    def test_release_gate_rejects_legacy_names_and_accepts_pool_plus_host(self):
        self.assertFalse(release_acceptance({"successful_agents": ["agy_review", "dsh_review"]})[0])
        self.assertFalse(release_acceptance({
            "successful_agents": ["dsh_review", "qoder_review"],
            "standby_substituted": True, "replaced": "agy_review",
        })[0])
        bound = {"prompt_sha256": "p", "manifest_sha256": "m"}
        nodes = [{"node_id": "north-node", "enabled": True, "test_passed": True,
                  "independence": ("cli:codex", "alpha")}]
        evidence = {
            "frozen_prompt_sha256": "p", "source_manifest_sha256": "m",
            "conclusions": [
                {"source": "pool", "node_id": "north-node", "passed": True, **bound},
                {"source": "host", "host_id": "host-a", "passed": True, "model": "grok", **bound},
            ],
            "execution": {"author_ids": [], "executor_ids": [], "checked_idle_ids": ["host-a"]},
            "pool_nodes": nodes,
        }
        self.assertTrue(release_acceptance(evidence, nodes)[0])
        two_hosts = dict(evidence, conclusions=[
            {"source": "host", "host_id": "host-a", "passed": True, "model": "a", **bound},
            {"source": "host", "host_id": "host-b", "passed": True, "model": "b", **bound},
        ])
        self.assertFalse(release_acceptance(two_hosts, nodes)[0])
        author = dict(evidence, execution={"author_ids": ["host-a"], "executor_ids": [], "checked_idle_ids": ["host-a"]})
        self.assertFalse(release_acceptance(author, nodes)[0])


def _page_source(node_id, entry, model, kind="review", tested=True, agent_type="codex", bound=False):
    """Shape written by the Agent-pool page: no adapter_type, no template."""
    test = {"status": "passed" if tested else "failed"}
    if bound and tested:
        test.update(spec_digest="spec", runner_digest="runner", event_id="evt-1",
                    execution_evidence={"provider": "test", "route_source": "test"})
    return {
        "reviewer_id": node_id,
        "kind": kind,
        "source_ref": "agent-entry:" + node_id,
        "node": {
            "version": 1, "revision": 1, "enabled": True, "removed": False,
            "name": node_id, "agent": node_id, "interface": "cli",
            "entry": entry, "model": model, "agent_type": agent_type,
            "test": test,
        },
    }


class Card36Counterexamples(unittest.TestCase):
    """Fail-first cases for card 36. These must fail on the card-35 workspace."""

    def test_page_shaped_nodes_do_not_complete_via_mock_without_calling_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "codex"
            calls = root / "codex.called"
            entry.write_text("#!/bin/sh\necho called >> \"$0.called\"\nexit 0\n")
            entry.chmod(0o700)
            config = _config(root, [])
            store = EventStore(config.database_path)
            store.initialize()
            _seed(store, [
                _page_source("真实Codex节点", str(entry), "alpha", agent_type="codex", bound=True),
                _page_source("south-node", str(root / "claude"), "beta", agent_type="claude", bound=True),
            ])
            (root / "claude").write_text("#!/bin/sh\nexit 0\n")
            (root / "claude").chmod(0o700)
            (root / "codex.no-receipt").touch()
            (root / "claude.no-receipt").touch()
            store.submit("审查走通", 2, snapshot={"prompt": "审查走通", "needs_review": True})
            with patch("xiaoba_agentos.orchestrator.release_binding_status", return_value={"ok": True}), \
                 patch("xiaoba_agentos.pool_dispatch.node_readiness", return_value={"ready": True, "reason": "tested_fixture"}), \
                 _patch_pool_cli():
                result = Orchestrator(config, store).run_once()
            self.assertNotEqual("completed", result.get("status"), result)
            self.assertTrue(calls.exists(), "page-shaped node must invoke its entry")

    def test_task_without_review_is_not_marked_completed_by_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root, LEGACY)
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("不用审查", 2, snapshot={"prompt": "不用审查", "needs_review": False})
            with patch("xiaoba_agentos.orchestrator.release_binding_status", return_value={"ok": True}):
                result = Orchestrator(config, store).run_once()
            self.assertNotEqual("completed", result.get("status"), result)
            self.assertNotEqual("completed", store.get_task(task_id)["status"])
            self.assertNotIn("宿主完成", str(store.get_task(task_id).get("result") or ""))

    def test_host_vote_without_checked_idle_ids_is_refused(self):
        bound = {"prompt_sha256": "p", "manifest_sha256": "m"}
        nodes = [{"node_id": "north-node", "enabled": True, "test_passed": True,
                  "independence": ("cli:codex", "alpha")}]
        evidence = {
            "frozen_prompt_sha256": "p", "source_manifest_sha256": "m",
            "conclusions": [
                {"source": "pool", "node_id": "north-node", "passed": True, **bound},
                {"source": "host", "host_id": "host-a", "passed": True, "model": "grok", **bound},
            ],
        }
        ok, reason = release_acceptance(evidence, nodes)
        self.assertFalse(ok, reason)

    def test_independence_uses_independence_key_and_infers_type(self):
        from xiaoba_agentos.pool_dispatch import independence_pair
        flash = {"interface": "cli", "agent_type": "codex", "entry": "/tmp/codex-a",
                 "model": "Gemini Flash"}
        folded = {"interface": "cli", "agent_type": "codex", "entry": "/tmp/codex-b",
                  "model": "gemini flash"}
        self.assertEqual(independence_pair(flash), independence_pair(folded))
        unnamed = {"interface": "cli", "entry": "/opt/bin/codex", "model": "same"}
        other_path = {"interface": "cli", "entry": "/usr/local/bin/codex", "model": "same"}
        self.assertEqual(independence_pair(unnamed), independence_pair(other_path))

    def test_legacy_name_mixed_into_completed_agents_is_refused(self):
        from xiaoba_agentos.pool_dispatch import legacy_names
        self.assertTrue(legacy_names(["agy_review", "north-node"]))

    def test_worker_roster_excludes_task_executors_at_run_once(self):
        nodes = [
            {"node_id": "worked", "role": "both", "interface": "cli", "ready_review": True,
             "independence": ("cli:codex", "a"), "enabled": True, "test_passed": True},
            {"node_id": "fresh", "role": "review", "interface": "cli", "ready_review": True,
             "independence": ("cli:claude", "b"), "enabled": True, "test_passed": True},
            {"node_id": "other", "role": "review", "interface": "cli", "ready_review": True,
             "independence": ("cli:qoder", "c"), "enabled": True, "test_passed": True},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root, [])
            store = EventStore(config.database_path)
            store.initialize()
            sources = []
            for node, entry, model, kind in (
                ("worked", "cli:codex", "a", "both"),
                ("fresh", "cli:claude", "b", "review"),
                ("other", "cli:qoder", "c", "review"),
            ):
                sources.append(_page_source(node, entry, model, kind=kind, agent_type="other"))
            _seed(store, sources)
            store.submit(
                "排除干过活的", 2,
                snapshot={"prompt": "排除干过活的", "needs_review": True,
                          "executor_ids": ["worked"], "collaborator_ids": ["worked"]},
            )
            from xiaoba_agentos.pool_dispatch import worker_roster, peek_runnable
            task = peek_runnable(store)
            roster = worker_roster(config, store, task=task)
            names = [agent.name for agent in roster.get("agents") or []]
            self.assertNotIn("worked", names)
            self.assertEqual({"fresh", "other"}, set(names))

    def test_complete_without_expected_attempt_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root, [])
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("结算", 3)
            store.lease_next(30)
            with self.assertRaises((TypeError, ValueError)):
                store.complete(task_id, "忘了传尝试次数")
            with self.assertRaises((TypeError, ValueError)):
                store.fail(task_id, "忘了传尝试次数")
            staged = root / "cand.md"
            staged.write_text("x")
            digest = __import__("hashlib").sha256(b"x").hexdigest()
            with self.assertRaises((TypeError, ValueError)):
                store.complete_with_memory_candidate(task_id, "结果", str(staged), digest, [])

    def test_active_names_does_not_fall_back_to_primary_agents(self):
        from xiaoba_agentos.pool_dispatch import active_names
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root, LEGACY)
            store = EventStore(config.database_path)
            store.initialize()
            self.assertEqual([], active_names(config, store, purpose="review"))

    def test_review_plan_reviewers_are_disjoint_from_collaborators(self):
        source = Path(__file__).resolve().parents[1] / "src/xiaoba_agentos/workflow.py"
        text = source.read_text(encoding="utf-8")
        self.assertNotIn('if names != plan["agents"]:', text)

    def test_page_copy_drops_dsh_peer_seat_labels(self):
        ingress = Path(__file__).resolve().parents[2] / "review-ingress/xiaoba_review"
        html = (ingress / "page.html").read_text(encoding="utf-8")
        js = (ingress / "page.js").read_text(encoding="utf-8")
        self.assertNotIn("固定 DSH", html)
        self.assertNotIn("固定 DSH", js)
        self.assertNotIn("第二审查 Agent", html)

    def test_ready_review_requires_readiness_not_just_test_status(self):
        from xiaoba_agentos.pool_dispatch import node_view
        view = node_view(_page_source("loose", "/missing/codex", "alpha"))
        self.assertFalse(view["ready_review"])

    def test_build_release_always_calls_release_acceptance(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "build_release.py"
        text = script.read_text(encoding="utf-8")
        self.assertNotIn(
            "if isinstance(payload, dict) and (payload.get(\"conclusions\") or payload.get(\"pool_nodes\")):",
            text,
        )

    def test_host_fallback_at_run_once_completes_one_pool_node(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root, [])
            store = EventStore(config.database_path)
            store.initialize()
            _seed(store, [_page_source("only", "cli:codex", "alpha", agent_type="codex")])
            store.submit(
                "宿主补票", 2,
                snapshot={"prompt": "宿主补票", "needs_review": True,
                          "host": {"id": "host-a", "worked": False, "author": False}},
            )
            with patch("xiaoba_agentos.orchestrator.release_binding_status", return_value={"ok": True}), \
                 _patch_pool_cli():
                result = Orchestrator(config, store).run_once()
            self.assertEqual("awaiting_host", result.get("status"), result)
            text = store.get_task(result["task_id"])["result"] or ""
            self.assertNotEqual("completed", store.get_task(result["task_id"])["status"])
            self.assertNotIn("无独立审查", text)
            names = {item["agent_name"] for item in store.latest_agent_runs(result["task_id"]).values()}
            self.assertEqual({"only"}, names)

    def test_work_phase_without_collab_is_not_marked_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root, [])
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit(
                "协作", 2,
                snapshot={"prompt": "协作", "needs_review": False,
                          "workflow": {"version": 1, "phase": "work", "agents": [], "assignments": {}}},
            )
            with patch("xiaoba_agentos.orchestrator.release_binding_status", return_value={"ok": True}):
                result = Orchestrator(config, store).run_once()
            self.assertNotEqual("completed", result.get("status"), result)
            self.assertNotEqual("completed", store.get_task(task_id)["status"])

    def test_build_release_output_feeds_switch_release(self):
        import importlib.util
        import sys
        root = Path(__file__).resolve().parents[1]
        build_spec = importlib.util.spec_from_file_location("build_release", root / "scripts" / "build_release.py")
        build = importlib.util.module_from_spec(build_spec)
        sys.modules[build_spec.name] = build
        build_spec.loader.exec_module(build)
        switch_spec = importlib.util.spec_from_file_location("switch_release", root / "scripts" / "switch_release.py")
        switch = importlib.util.module_from_spec(switch_spec)
        sys.modules[switch_spec.name] = switch
        switch_spec.loader.exec_module(switch)
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            (tree / "config").mkdir()
            (tree / "config" / "agentos.json").write_text(json.dumps({
                "worker": {"minimum_successful_agents": 2},
                "agents": [
                    {"name": "south-node", "enabled": True},
                    {"name": "north-node", "enabled": True},
                ],
            }), encoding="utf-8")
            (tree / "SOURCE_MANIFEST.json").write_text("{}\n", encoding="utf-8")
            manifest = __import__("hashlib").sha256((tree / "SOURCE_MANIFEST.json").read_bytes()).hexdigest()
            nodes = [
                {"node_id": "south-node", "enabled": True, "test_passed": True,
                 "independence": ["cli:codex", "alpha"]},
                {"node_id": "north-node", "enabled": True, "test_passed": True,
                 "independence": ["cli:claude", "beta"]},
            ]
            payload = {
                "task": {
                    "id": "task-1", "status": "completed", "execution_mode": "production",
                    "result": "本回答：小八 AgentOS 双 Agent 裁判\n\n有效结论",
                    "provenance_json": json.dumps({"kind": "agentos_multi_adjudicated",
                                                   "minimum_successful_agents": 2}),
                    "execution_snapshot_json": json.dumps({"source_manifest_sha256": manifest}),
                },
                "runs": [
                    {"agent_name": "south-node", "status": "completed", "output": "FINAL_ACCEPTANCE: PASS\nA"},
                    {"agent_name": "north-node", "status": "completed", "output": "FINAL_ACCEPTANCE: PASS\nB"},
                ],
                "memory_candidate": {"status": "pending_delivery_review"},
                "pool_nodes": nodes,
                "execution": {"author_ids": [], "executor_ids": [], "checked_idle_ids": []},
            }
            produced = build._validate_acceptance_payload(payload, tree)
            self.assertIn("conclusions", produced)
            self.assertIn("pool_nodes", produced)
            self.assertIn("execution", produced)
            self.assertTrue(switch.independent_acceptance(produced))

    def test_host_fallback_at_run_once_asks_user_when_host_also_worked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root, [])
            store = EventStore(config.database_path)
            store.initialize()
            _seed(store, [
                _page_source("only", "/tmp/codex", "alpha", tested=False),
            ])
            store.submit(
                "宿主也干过", 2,
                snapshot={"prompt": "宿主也干过", "needs_review": True,
                          "host": {"id": "host-a", "worked": True, "author": False},
                          "user_chose_host": True},
            )
            with patch("xiaoba_agentos.orchestrator.release_binding_status", return_value={"ok": True}):
                result = Orchestrator(config, store).run_once()
            self.assertIn(result.get("status"), {"ask_user", "blocked_review_nodes"})
            self.assertIn("干过活", str(result.get("error") or result.get("message") or ""))


if __name__ == "__main__":
    unittest.main()
