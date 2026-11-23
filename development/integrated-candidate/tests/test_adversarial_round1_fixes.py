"""Adversarial regression tests for r1 (5 P1 + 2 P2), r2 (2 P1 + 1 P2), and r3 (2 P1 + 2 P2) findings."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[1]
SCRIPTS = ROOT / "scripts"
for _p in (str(SCRIPTS), str(ROOT / "src"), str(ROOT.parent / "review-ingress")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _make_fully_isolated_config(proj: Path, agents: list[dict]):
    from xiaoba_agentos.config import AppConfig

    proj = proj.resolve()
    proj.mkdir(parents=True, exist_ok=True)
    runtime_dir = proj / "runtime"
    releases_dir = runtime_dir / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)
    index_path = releases_dir / "index.json"
    index_path.write_text('{"current_release": "v1.6.16", "releases": []}\n', encoding="utf-8")
    hex_zero = "0".ljust(64, "0")
    (proj / "SOURCE_MANIFEST.json").write_text(
        json.dumps({"algorithm": "sha256", "files": {"README.md": hex_zero}}, indent=2) + "\n",
        encoding="utf-8",
    )
    skills_dir = proj / "skills"
    skills_dir.mkdir(exist_ok=True)
    (skills_dir / "index.json").write_text('{"schema_version": 1, "entries": []}\n', encoding="utf-8")
    cfg_dir = proj / "config"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "content_baseline.json").write_text('{"schema_version": 1, "skills": {}}\n', encoding="utf-8")
    know_idx = proj / "knowledge_index.json"
    know_idx.write_text('{"schema_version": 1, "entries": []}\n', encoding="utf-8")
    vault = proj / "assistant/第二大脑"
    vault.mkdir(parents=True, exist_ok=True)
    graph_dir = runtime_dir / "knowlp"
    knowlp_mod = proj / "knowlp/runtime/knowlp_mcp.py"
    fake_bin = proj / "bin"
    fake_bin.mkdir(exist_ok=True)

    raw = {
        "schema_version": 2,
        "runtime_dir": str(runtime_dir),
        "project_dir": str(proj),
        "release": {
            "index_path": str(index_path),
            "expected_version": "v1.6.16",
            "final_publish_requires_human": True,
        },
        "worker": {
            "poll_seconds": 1,
            "lease_seconds": 60,
            "max_attempts": 3,
            "retry_base_seconds": 1,
            "retry_max_seconds": 5,
            "parallelism": 2,
            "minimum_successful_agents": 2,
            "health_max_age_seconds": 86400,
            "max_agent_invocations_per_task": 6,
            "log_heartbeat_polls": 10,
        },
        "recovery": {
            "repair_max_attempts": 2,
            "repair_retry_seconds": 5,
            "resume_requires_user": True,
        },
        "memory": {
            "obsidian_vault": str(vault),
            "obsidian_subdir": "70_复利规则/经验",
            "catalog_note": "70_复利规则/经验索引.md",
            "publish_enabled": False,
            "retire_after_failed_reuses": 2,
            "knowlp_python": str(fake_bin / "fake-python"),
            "knowlp_module": str(knowlp_mod),
            "knowlp_vault": str(vault),
            "knowlp_graph_dir": str(graph_dir),
            "knowlp_enabled": False,
            "knowlp_limit": 6,
        },
        "skills": {
            "root_dir": str(skills_dir),
            "index_path": str(skills_dir / "index.json"),
            "max_selected": 4,
        },
        "knowledge": {
            "index_path": str(know_idx),
            "allowed_roots": [str(proj)],
        },
        "integrations": {
            "node_command": str(fake_bin / "fake-node"),
        },
        "cli_routes": {},
        "agents": list(agents),
        "evolution": {
            "minimum_samples": 1,
            "promotion_margin": 0.05,
            "latency_penalty": 0.01,
        },
        "paths": {
            "obsidian_vault": str(vault),
            "knowlp_module": str(knowlp_mod),
            "knowlp_vault": str(vault),
            "knowlp_graph_dir": str(graph_dir),
            "agy_command": str(fake_bin / "fake-agy"),
        },
    }
    cfg_path = proj / "config.json"
    serialized = json.dumps(raw, ensure_ascii=False, indent=2)
    if str(Path.home()) in serialized:
        raise AssertionError("isolated config leaked home directory path")
    cfg_path.write_text(serialized + "\n", encoding="utf-8")
    return AppConfig.load(cfg_path)


class Finding1ManifestProjectionTests(unittest.TestCase):
    """P1-1 (r1 + r2): Manifest & runtime_manifest_sha256 cover monitor, architecture-center, ingress entrypoints, interface, and external tests."""

    def test_editing_monitor_architecture_ingress_or_external_tests_changes_both_manifests(self):
        import create_manifest as cm
        from xiaoba_agentos.review_ui_install import TARGETS

        self.assertEqual(
            {
                "agentos_config.py", "monitor_config.py", "monitor_overview.py", "monitor_review.py",
                "monitor_web.py", "workflow_query.py", "review_action.py", "server.py", "xiaoba-monitor.html",
            },
            set(TARGETS),
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve() / "repo"
            ic = repo / "development/integrated-candidate"
            ri = repo / "development/review-ingress"
            ac = repo / "development/architecture-center"
            iface = repo / "development/interface"
            mon = repo / "architecture-package-20261027/shared/工具/小八监看"
            for d in (ic / "src/xiaoba_agentos", ic / "docs", ic / "scripts",
                      ri / "xiaoba_review", ri / "tests",
                      ac / "tests", ac / "web",
                      iface,
                      mon / "monitor_core/modules", mon / "tests"):
                d.mkdir(parents=True, exist_ok=True)
            for rf in ("README.md", "pyproject.toml", ".gitignore", "CAPABILITY.json"):
                (ic / rf).write_text(rf + "\n", encoding="utf-8")
            (ic / "src/xiaoba_agentos/__init__.py").write_text("__version__ = '1.6.16'\n", encoding="utf-8")
            (ic / "docs/RELEASE_V1.6.16.md").write_text("# v1.6.16\n", encoding="utf-8")
            (ri / "run.py").write_text("RUN = 1\n", encoding="utf-8")
            (ri / "workday_session.py").write_text("WS = 1\n", encoding="utf-8")
            (ri / "xiaoba_review/store.py").write_text("X = 1\n", encoding="utf-8")
            (ri / "tests/test_ing.py").write_text("T = 1\n", encoding="utf-8")
            (ac / "__init__.py").write_text("\n", encoding="utf-8")
            (ac / "core.py").write_text("C = 1\n", encoding="utf-8")
            (ac / "MODULES.json").write_text('{"schema_version": 1, "modules": []}\n', encoding="utf-8")
            (ac / "tests/test_arch.py").write_text("A = 1\n", encoding="utf-8")
            (iface / "SKILL.md").write_text("# Interface Skill\n", encoding="utf-8")
            (mon / "monitor_core/modules/review.py").write_text("R = 1\n", encoding="utf-8")
            (mon / "tests/test_mon.py").write_text("M = 1\n", encoding="utf-8")

            base_entries = cm.collect_entries(ic)
            for expected_key in (
                "src/xiaoba_monitor_shared/monitor_core/modules/review.py",
                "src/xiaoba_monitor_shared/tests/test_mon.py",
                "tests/_external_suites/review_ingress/test_ing.py",
                "src/xiaoba_architecture_center/core.py",
                "src/xiaoba_architecture_center/MODULES.json",
                "src/xiaoba_architecture_center/tests/test_arch.py",
                "src/xiaoba_review_entrypoints/run.py",
                "src/xiaoba_review_entrypoints/workday_session.py",
                "src/xiaoba_interface_card/SKILL.md",
            ):
                self.assertIn(expected_key, base_entries)

            base_rt, _ = cm.split_manifest_entries(base_entries)
            base_full_sha = cm.manifest_digest(base_entries)
            base_rt_sha = cm.manifest_digest(base_rt)

            for target_file in (
                mon / "monitor_core/modules/review.py",
                ri / "tests/test_ing.py",
                mon / "tests/test_mon.py",
                ac / "core.py",
                ac / "MODULES.json",
                ri / "run.py",
                ri / "workday_session.py",
                iface / "SKILL.md",
            ):
                original = target_file.read_text(encoding="utf-8")
                try:
                    target_file.write_text(original + "\n# mutated\n", encoding="utf-8")
                    mut_entries = cm.collect_entries(ic)
                    mut_rt, _ = cm.split_manifest_entries(mut_entries)
                    self.assertNotEqual(base_full_sha, cm.manifest_digest(mut_entries), str(target_file))
                    self.assertNotEqual(base_rt_sha, cm.manifest_digest(mut_rt), str(target_file))
                finally:
                    target_file.write_text(original, encoding="utf-8")

    def test_copy_review_materializes_and_packaged_create_manifest_verify_roundtrips(self):
        import shutil
        import build_release
        import create_manifest as cm

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve() / "repo"
            ic = repo / "development/integrated-candidate"
            ri = repo / "development/review-ingress"
            ac = repo / "development/architecture-center"
            iface = repo / "development/interface"
            mon = repo / "architecture-package-20261027/shared/工具/小八监看"
            for d in (ic / "src/xiaoba_agentos", ic / "docs", ic / "scripts",
                      ri / "xiaoba_review", ri / "tests",
                      ac / "modules/task-planning", ac / "tests", ac / "web",
                      iface,
                      mon / "monitor_core/modules", mon / "tests"):
                d.mkdir(parents=True, exist_ok=True)
            for rf in ("README.md", "pyproject.toml", ".gitignore", "CAPABILITY.json"):
                (ic / rf).write_text(rf + "\n", encoding="utf-8")
            for mod_name in ("__init__.py", "config.py", "release.py"):
                shutil.copy2(ROOT / "src/xiaoba_agentos" / mod_name, ic / "src/xiaoba_agentos" / mod_name)
            shutil.copy2(ROOT / "scripts/create_manifest.py", ic / "scripts/create_manifest.py")
            (ic / "docs/RELEASE_V1.6.16.md").write_text("# v1.6.16\n", encoding="utf-8")
            (ri / "run.py").write_text("RUN = 1\n", encoding="utf-8")
            (ri / "workday_session.py").write_text("WS = 1\n", encoding="utf-8")
            (ri / "xiaoba_review/store.py").write_text("X = 1\n", encoding="utf-8")
            (ri / "tests/test_ing.py").write_text("T = 1\n", encoding="utf-8")
            (ac / "__init__.py").write_text("\n", encoding="utf-8")
            (ac / "core.py").write_text("C = 1\n", encoding="utf-8")
            (ac / "MODULES.json").write_text('{"schema_version": 1, "modules": []}\n', encoding="utf-8")
            (ac / "modules/task-planning/SPEC.md").write_text("# Spec\n", encoding="utf-8")
            (ac / "tests/test_arch.py").write_text("A = 1\n", encoding="utf-8")
            (iface / "SKILL.md").write_text("# Interface Skill\n", encoding="utf-8")
            (mon / "monitor_core/modules/review.py").write_text("R = 1\n", encoding="utf-8")
            (mon / "tests/test_mon.py").write_text("M = 1\n", encoding="utf-8")

            entries = cm.collect_entries(ic)
            (ic / "SOURCE_MANIFEST.json").write_text(
                json.dumps({"algorithm": "sha256", "files": entries}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            rt_entries, _ = cm.split_manifest_entries(entries)
            expected_source_sha = hashlib.sha256((ic / "SOURCE_MANIFEST.json").read_bytes()).hexdigest()
            expected_runtime_sha = cm.manifest_digest(rt_entries)

            packaged = repo / "packaged_agentos"
            build_release.copy_tree(ic, packaged)
            build_release.copy_review(ic, packaged)
            shutil.copy2(ic / "SOURCE_MANIFEST.json", packaged / "SOURCE_MANIFEST.json")

            for materialized in (
                "src/xiaoba_architecture_center/modules/task-planning/SPEC.md",
                "src/xiaoba_monitor_shared/monitor_core/modules/review.py",
                "src/xiaoba_review_entrypoints/run.py",
                "src/xiaoba_review/store.py",
                "src/xiaoba_interface_card/SKILL.md",
                "tests/_external_suites/review_ingress/test_ing.py",
            ):
                self.assertTrue((packaged / materialized).is_file(), materialized)

            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            env.pop("PYTHONPATH", None)
            verified = subprocess.run(
                [sys.executable, "-B", "scripts/create_manifest.py", "--verify", "--summary"],
                cwd=str(packaged), capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(0, verified.returncode, verified.stdout + verified.stderr)
            summary = json.loads(verified.stdout)
            self.assertTrue(summary["ok"])
            self.assertEqual(expected_source_sha, summary["source_manifest_sha256"])
            self.assertEqual(expected_runtime_sha, summary["runtime_manifest_sha256"])

    def test_cli_and_create_manifest_share_single_is_prose_doc_path_predicate(self):
        import create_manifest as cm
        from xiaoba_agentos import cli as core_cli
        from xiaoba_agentos import release as core_release

        self.assertIs(core_cli.is_prose_doc_path, core_release.is_prose_doc_path)
        for sample, expected in (
            ("README.md", True),
            ("docs/EXPERIENCE_LOOP.md", True),
            ("docs/DEPLOYMENT.json", False),
            ("src/xiaoba_architecture_center/README.md", False),
            ("src/xiaoba_architecture_center/modules/task-planning/SPEC.md", False),
            (".agents/skills/xiaoba-agentos/SKILL.md", False),
        ):
            self.assertEqual(expected, core_release.is_prose_doc_path(sample), sample)
            self.assertEqual(expected, cm.is_prose_doc_path(sample), sample)

        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp).resolve()
            manifest = {
                "algorithm": "sha256",
                "files": {
                    "README.md": "1" * 64,
                    "docs/GUIDE.md": "2" * 64,
                    "docs/DEPLOYMENT.json": "3" * 64,
                    "src/xiaoba_architecture_center/README.md": "4" * 64,
                    "src/xiaoba_agentos/cli.py": "5" * 64,
                },
            }
            (proj / "SOURCE_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(
                cm.runtime_manifest_sha256_from_file(proj / "SOURCE_MANIFEST.json"),
                core_cli._runtime_manifest_fingerprint(proj),
            )

    def test_run_py_packaged_and_repo_root_architecture_center_behavior(self):
        import importlib.util
        import shutil
        from types import SimpleNamespace

        run_spec = importlib.util.spec_from_file_location("review_ingress_run_test", REPO_ROOT / "development/review-ingress/run.py")
        run_mod = importlib.util.module_from_spec(run_spec)
        assert run_spec and run_spec.loader
        run_spec.loader.exec_module(run_mod)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            pkg = root / "packaged_agentos"
            pkg_src = pkg / "src"
            pkg_entry = pkg_src / "xiaoba_review_entrypoints"
            pkg_center = pkg_src / "xiaoba_architecture_center"
            for d in (pkg_src / "xiaoba_agentos", pkg_src / "xiaoba_monitor_shared", pkg_entry, pkg_center / "modules/task-planning"):
                d.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / "development/review-ingress/run.py", pkg_entry / "run.py")
            for name in ("__init__.py", "core.py", "monitor.py", "MODULES.json"):
                shutil.copy2(REPO_ROOT / "development/architecture-center" / name, pkg_center / name)
            (pkg_center / "modules/task-planning/SPEC.md").write_text("# Packaged Task Planning Spec\n", encoding="utf-8")

            worktree = root / "worktree"
            wt_spec = worktree / "development/architecture-center/modules/task-planning/SPEC.md"
            wt_spec.parent.mkdir(parents=True, exist_ok=True)
            wt_spec.write_text("# Worktree Task Planning Spec\n", encoding="utf-8")

            packaged_paths = run_mod.resolve_runtime_paths(pkg_entry)
            self.assertEqual(pkg_src, packaged_paths["core_source"])
            self.assertEqual(pkg_src / "xiaoba_monitor_shared", packaged_paths["monitor_source"])
            self.assertEqual(pkg_center, packaged_paths["center_root"])
            self.assertEqual(pkg_center, packaged_paths["spec_root"])
            self.assertIsNotNone(packaged_paths["readonly_reason"])

            monitor_root = str(REPO_ROOT / "architecture-package-20261027/shared/工具/小八监看")
            arch_spec = importlib.util.spec_from_file_location(
                "xiaoba_arch_pkg_test", pkg_center / "__init__.py", submodule_search_locations=[str(pkg_center)],
            )
            arch_mod = importlib.util.module_from_spec(arch_spec)
            assert arch_spec and arch_spec.loader
            added_sys = monitor_root not in sys.path
            if added_sys:
                sys.path.insert(0, monitor_root)
            saved_mod = sys.modules.get("xiaoba_arch_pkg_test")
            sys.modules["xiaoba_arch_pkg_test"] = arch_mod
            try:
                arch_spec.loader.exec_module(arch_mod)
                from xiaoba_arch_pkg_test.core import Center
                from xiaoba_arch_pkg_test.monitor import ArchitectureCenterModule

                ro_center = Center(
                    packaged_paths["repo_root"], root / "ws-ro", pkg_center / "MODULES.json",
                    spec_root=packaged_paths["spec_root"], readonly_reason=packaged_paths["readonly_reason"],
                )
                ro_http = ArchitectureCenterModule(SimpleNamespace(), ro_center)
                status_meta, body_meta = ro_http.meta(SimpleNamespace())
                self.assertEqual(200, status_meta)
                self.assertFalse(body_meta["writable"])
                self.assertEqual("readonly_specs", body_meta["mode"])

                status_mod, body_mod = ro_http.module(SimpleNamespace(param=lambda key: {"id": "task-planning"}[key]))
                self.assertEqual(200, status_mod, body_mod)
                self.assertEqual("# Packaged Task Planning Spec\n", body_mod["result"]["body"])

                status_create, body_create = ro_http.create(SimpleNamespace(json=lambda: {
                    "module_id": "task-planning",
                    "body": "# Edited\n",
                    "expected_sha256": body_mod["result"]["sha256"],
                    "goal": "test readonly rejection",
                    "base_version": "v1.6.16",
                    "base_source": "test",
                }))
                self.assertEqual(400, status_create)
                self.assertFalse(body_create["ok"])
                self.assertIn("--repo-root", body_create["error"])

                wt_paths = run_mod.resolve_runtime_paths(pkg_entry, repo_root_arg=worktree)
                self.assertEqual(worktree, wt_paths["repo_root"])
                self.assertIsNone(wt_paths["spec_root"])
                self.assertIsNone(wt_paths["readonly_reason"])

                rw_center = Center(
                    wt_paths["repo_root"], root / "ws-rw", pkg_center / "MODULES.json",
                    spec_root=wt_paths["spec_root"], readonly_reason=wt_paths["readonly_reason"],
                )
                rw_spec = rw_center.module("task-planning")
                self.assertEqual("# Worktree Task Planning Spec\n", rw_spec["body"])
                created = rw_center.create(
                    "task-planning", "# Proposal\n", rw_spec["sha256"], "update spec", "v1.6.16", "test",
                )
                self.assertEqual("awaiting_agent", created["state"])
                self.assertTrue(rw_center.state()["writable"])
            finally:
                if saved_mod is None:
                    sys.modules.pop("xiaoba_arch_pkg_test", None)
                else:
                    sys.modules["xiaoba_arch_pkg_test"] = saved_mod
                if added_sys and monitor_root in sys.path:
                    sys.path.remove(monitor_root)

            bad_link = root / "link_worktree"
            bad_link.symlink_to(worktree)
            for invalid in (root / "missing", bad_link):
                with self.assertRaises(ValueError):
                    run_mod.resolve_runtime_paths(pkg_entry, repo_root_arg=invalid)


class Finding2ValueEvidenceBypassTests(unittest.TestCase):
    """P1-2: Value receipt cannot bypass frozen evidence check with invented non-task strings."""

    def test_invented_non_task_evidence_ref_is_rejected_for_pool_and_host(self):
        from xiaoba_agentos.cli import _submit_task
        from xiaoba_agentos.host_completion import complete_host
        from xiaoba_agentos.store import EventStore
        from xiaoba_agentos.value_scoring import value_receipt_problem

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cfg = _make_fully_isolated_config(root / "proj", [
                {"name": "pool-a", "enabled": True, "pool_node": True, "type": "mock",
                 "entry": "cli:codex", "model": "gpt-test", "template": "mock"},
            ])
            store = EventStore(cfg.database_path)
            store.initialize()
            sub = _submit_task(
                cfg, store, "TEST method task",
                experience_kind="method",
                host={"id": "host-1", "entry": "cli:host", "model": "host-model"},
            )
            task_id = sub if isinstance(sub, str) else sub["task_id"]
            task = store.get_task(task_id)

            bad_pool = (
                "审查结论: PASS\n审查类型: new_experience\n评分: 8\n"
                "适用条件: 仅测试\n证据引用: invented-proof-never-in-frozen-snapshot\n---\n正文"
            )
            self.assertIn("证据引用未对应任务卡材料", value_receipt_problem(task, bad_pool))

            good_pool = (
                f"审查结论: PASS\n审查类型: new_experience\n评分: 8\n"
                f"适用条件: 仅测试\n证据引用: task:{task_id}\n---\n正文"
            )
            self.assertEqual("", value_receipt_problem(task, good_pool))
            run_id = store.start_agent_run(task_id, "pool-a", {"purpose": "review"})
            store.finish_agent_run(run_id, "completed", good_pool, None, 10)

            bad_host_receipt = {
                "receipt_id": "TEST-bad-host",
                "task_id": task_id,
                "snapshot_sha256": store.get_task(task_id)["execution_snapshot_hash"],
                "reviewer_id": "host-1",
                "verdict": "PASS",
                "findings": "TEST host findings",
                "evidence_refs": ["invented-proof-never-in-frozen-snapshot"],
                "review_type": "new_experience",
                "score": 9,
                "applicability": "TEST applicability",
                "duplicate_of": None,
                "started_at": time.time() - 2,
                "finished_at": time.time(),
            }
            out = complete_host(cfg, store, task_id, bad_host_receipt, user_chose_host=True)
            self.assertEqual("needs_info", out["status"])
            self.assertNotEqual("completed", store.get_task(task_id)["status"])
            self.assertIsNone(store.get_memory_candidate(task_id))


class Finding3And4DeliverySecurityAndWithdrawalTests(unittest.TestCase):
    """P1-3 & P1-4 (r1 + r2): message_id containment + withdrawal after drift (both native and shared-file direct)."""

    def test_escape_message_id_and_drifted_shared_artifact_result_withdrawal_via_subprocess_cli(self):
        from xiaoba_agentos.delivery import detail_tx, register_direct, submit as submit_delivery, user_decision
        from xiaoba_agentos.experience_loop import record_application
        from xiaoba_agentos.reuse_scores import projection_tx
        from xiaoba_agentos.store import EventStore

        mon_dir = REPO_ROOT / "architecture-package-20261027/shared/工具/小八监看"
        if str(mon_dir) not in sys.path:
            sys.path.insert(0, str(mon_dir))
        from monitor_core.modules import overview as overview_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cfg = _make_fully_isolated_config(root / "proj", [])
            store = EventStore(cfg.database_path)
            store.initialize()

            # Publish a formal method note inside isolated temp vault
            vault = Path(cfg.raw["memory"]["obsidian_vault"])
            cand_task = store.submit("TEST formal method shared file", 2)
            store.complete(cand_task, "TEST source answer", expected_attempt=0)
            pub_note = vault / "formal_shared_method.md"
            pub_bytes = b"# Formal Shared Method\nSteps.\n"
            pub_sha = hashlib.sha256(pub_bytes).hexdigest()
            pub_note.write_bytes(pub_bytes)
            outbox = cfg.runtime_dir / "memory-outbox"
            outbox.mkdir(parents=True, exist_ok=True)
            staged = outbox / f"{cand_task}.md"
            staged.write_bytes(pub_bytes)
            store.stage_memory_candidate(cand_task, str(staged), pub_sha, [cand_task])
            with store.connect() as conn:
                conn.execute(
                    "UPDATE memory_candidates SET status='published',published_path=?,published_at=? WHERE task_id=?",
                    (str(pub_note), time.time(), cand_task),
                )

            # Direct task where THE SAME Markdown file is registered as BOTH artifact AND result!
            ws = root / "direct-shared-ws"
            ws.mkdir()
            shared_md = ws / "deliverable_and_result.md"
            md_bytes = f"# Deliverable\nCompleted using {cand_task}.\n".encode("utf-8")
            shared_md.write_bytes(md_bytes)
            shared_sha = hashlib.sha256(md_bytes).hexdigest()
            art_ref = {"version": "v1", "path": str(shared_md), "sha256": shared_sha}
            res_ref = {"path": str(shared_md), "sha256": shared_sha}
            real_task = {
                "id": "direct-shared-1",
                "workspace": str(ws),
                "authorization_ref": "user://direct-shared-1",
                "description": "TEST direct shared artifact/result",
            }
            register_direct(store, {"version": 1, "real_task": real_task, "artifact": art_ref, "result": res_ref})
            record_application(store, {
                "application_id": "app-shared-1",
                "candidate_id": cand_task,
                "candidate_sha256": pub_sha,
                "formal_reuse": True,
                "conditions_checked": {"applicability": "matched", "exclusions": "none", "environment": "macos"},
                "application_location": str(shared_md),
                "steps_applied": "Applied steps cleanly",
                "deviations": "none",
                "reuse_assessment": {"outcome": "unchanged", "changed": "", "added": "", "discarded": ""},
                "real_task": dict(real_task, problem_key="shared-md", task_type="doc"),
                "artifact": art_ref,
                "execution_evidence": [art_ref],
            })

            with store.read_only() as conn:
                det = detail_tx(conn, "direct-shared-1", art_ref)

            # P1-3: Escape message_id must create ZERO external files
            outside_target = root / "outside_escaped_file"
            for evil_id in (str(outside_target), "../outside_escaped_file"):
                bad_req = {
                    "version": 1,
                    "task_id": "direct-shared-1",
                    "real_task": det["real_task"],
                    "artifact": art_ref,
                    "expected": det["expected"],
                    "decision": "accept",
                    "user_selected": True,
                    "message_id": evil_id,
                    "source_ref": "ui://test",
                }
                with self.assertRaises(ValueError):
                    submit_delivery(cfg, store, bad_req, authority=user_decision(bad_req, channel="local_csrf_post", explicit=True))
                self.assertFalse((root / "outside_escaped_file.json").exists())

            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            env["PYTHONPATH"] = str(ROOT / "src")

            # Accept via real subprocess CLI -> score == 1
            accept_json = root / "accept_shared.json"
            accept_json.write_text(json.dumps({
                "origin": "user",
                "user_message": "交付",
                "message_id": "msg-accept-shared-1",
                "source_ref": "chat://accept-shared",
                "real_task_id": "direct-shared-1",
                "artifact": art_ref,
            }, ensure_ascii=False), encoding="utf-8")
            p_acc = subprocess.run(
                [sys.executable, "-B", "-m", "xiaoba_agentos.cli", "--config", str(cfg.path),
                 "memory-delivery-record", "--receipt", str(accept_json), "--user-decision"],
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(0, p_acc.returncode, p_acc.stderr + p_acc.stdout)
            with store.read_only() as conn:
                self.assertEqual(1, projection_tx(conn)[cand_task]["score"])

            # Mutate the shared artifact/result Markdown file on disk!
            shared_md.write_text("# Edited after delivery — both artifact and result drifted!\n", encoding="utf-8")

            # Real subprocess task-delivery-view MUST exit 0 and return allowed_decisions == ['withdraw']
            p_view = subprocess.run(
                [sys.executable, "-B", "-m", "xiaoba_agentos.cli", "--config", str(cfg.path),
                 "task-delivery-view", "--task-id", "direct-shared-1"],
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(0, p_view.returncode, p_view.stderr + p_view.stdout)
            view_data = json.loads(p_view.stdout)
            self.assertEqual(["withdraw"], view_data["allowed_decisions"])

            # Real subprocess memory-delivery-record withdrawal MUST exit 0 and reverse score 1 -> 0!
            withdraw_json = root / "withdraw_shared.json"
            withdraw_json.write_text(json.dumps({
                "origin": "user",
                "user_message": "撤回验收",
                "message_id": "msg-withdraw-shared-1",
                "source_ref": "chat://withdraw-shared",
                "real_task_id": "direct-shared-1",
                "artifact": art_ref,
            }, ensure_ascii=False), encoding="utf-8")
            p_wd = subprocess.run(
                [sys.executable, "-B", "-m", "xiaoba_agentos.cli", "--config", str(cfg.path),
                 "memory-delivery-record", "--receipt", str(withdraw_json), "--user-decision"],
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(0, p_wd.returncode, p_wd.stderr + p_wd.stdout)

            with store.read_only() as conn:
                self.assertEqual(0, projection_tx(conn)[cand_task]["score"])
                mon_rank = overview_mod.permanent_total_ranking(conn)
                self.assertEqual(0, mon_rank["items"][0]["score"])
                rev_cnt = conn.execute("SELECT COUNT(1) FROM events WHERE event_type='memory.reuse_reversed'").fetchone()[0]
            self.assertEqual(1, rev_cnt)


class Finding5SingleSeatReplaceSettlementTests(unittest.TestCase):
    """P1-5: Replacing seat A -> C while seat B stays at plan_revision=1 settles value review cleanly."""

    def test_replacing_one_seat_settles_value_review_with_surviving_seat_receipt(self):
        from xiaoba_agentos.store import EventStore
        from xiaoba_review.agent_pool import AgentPool
        from xiaoba_review.backends import BackendRegistry
        from xiaoba_review.store import ReviewStore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            store = EventStore(root / "agentos.sqlite3")
            store.initialize()
            outbox = root / "memory-outbox"
            rs = ReviewStore(store, outbox)
            pool = AgentPool(rs, BackendRegistry(rs))

            cand = store.create_authored_candidate(
                "TEST single seat replace value review",
                "TEST candidate body",
                outbox,
                author="author-node",
                experience_kind="method",
            )
            task_id = cand["task_id"]
            cand_sha = cand["evidence_sha256"]
            staged_path = str(Path(cand["staged_path"]).resolve(strict=True))

            for sid, entry, model in (
                ("rev-A", "/TEST/codex", "gpt-5"),
                ("rev-B", "/TEST/qoder", "qwen-max"),
                ("rev-C", "/TEST/dsh", "deepseek-v4"),
            ):
                src = pool.add({
                    "node_id": sid,
                    "name": sid,
                    "agent": sid,
                    "kind": "review",
                    "interface": "cli",
                    "entry": entry,
                    "model": model,
                    "agent_type": "other",
                })
                pool.change(sid, src["node"]["revision"], "toggle", True)

            req = rs.create(
                task_id, "rev-A", "rev-B", "TEST value review scope",
                [{"ref": staged_path, "sha256": cand_sha}],
                review_type="new_experience", value_review=True,
            )
            updated = rs.replace_reviewer(req["id"], "rev-C", "rev-A offline", old_source_id="rev-A")
            active_cards = {a["source_id"]: a["card"] for a in updated["assignments"] if a["active"]}
            self.assertEqual(1, active_cards["rev-B"]["plan_revision"])
            self.assertEqual(2, active_cards["rev-C"]["plan_revision"])

            now = time.time()
            for idx, sid in enumerate(("rev-B", "rev-C"), 1):
                card = active_cards[sid]
                payload = {
                    "receipt_id": f"rcpt-{sid}-{idx}",
                    "card_digest": card["card_digest"],
                    "review_request_id": card["review_request_id"],
                    "assignment_id": card["assignment_id"],
                    "reviewer_role": card["reviewer_role"],
                    "plan_revision": card["plan_revision"],
                    "reviewer_id": card["reviewer_id"],
                    "source_ref": card["source_ref"],
                    "input_digest": card["input_digest"],
                    "candidate_sha256": card["candidate_sha256"],
                    "verdict": "PASS",
                    "findings": f"Independent review from {sid}",
                    "evidence_refs": [staged_path],
                    "review_type": "new_experience",
                    "score": 8,
                    "applicability": "Applicable to method tasks",
                    "started_at": now - 2,
                    "finished_at": now - 1,
                }
                rec = rs.receive(payload)
                self.assertEqual("identity_pending", rec["status"], rec)
                rs.attest(rec["id"], rec["digest"], f"Verified {sid}")

            settled = store.get_memory_candidate(task_id)
            self.assertIsNotNone(settled)
            self.assertEqual("pending_delivery_review", settled["status"])


class Finding6And7HostManifestAndMonitorReversalTests(unittest.TestCase):
    """P2-6 & P2-7 (r1 + r2): Pool+Host prose doc decoupling + Monitor unhashable/unbacked payload isolation."""

    def test_pool_plus_host_vote_survives_prose_doc_edit_and_rejects_runtime_edit(self):
        import build_release
        import create_manifest as cm

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp).resolve() / "candidate"
            for d in ("src/xiaoba_agentos", "config", "docs", "scripts", "tests"):
                (src / d).mkdir(parents=True)
            for rf in ("README.md", "pyproject.toml", ".gitignore", "CAPABILITY.json"):
                (src / rf).write_text(rf + "\n", encoding="utf-8")
            (src / "pyproject.toml").write_text('[project]\nname = "xiaoba"\nversion = "1.6.16"\n', encoding="utf-8")
            (src / "config/agentos.json").write_text(json.dumps({
                "agents": [],
                "worker": {"minimum_successful_agents": 2},
            }), encoding="utf-8")
            (src / "src/xiaoba_agentos/code.py").write_text("VALUE = 1\n", encoding="utf-8")
            (src / "docs/RELEASE_V1.6.16.md").write_text("# Initial notes\n", encoding="utf-8")

            entries_1 = cm.collect_entries(src)
            (src / "SOURCE_MANIFEST.json").write_text(
                json.dumps({"algorithm": "sha256", "files": entries_1}, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            full_sha_1 = hashlib.sha256((src / "SOURCE_MANIFEST.json").read_bytes()).hexdigest()
            rt_sha_1 = cm.runtime_manifest_sha256_from_file(src / "SOURCE_MANIFEST.json")

            snapshot = {
                "source_manifest_sha256": full_sha_1,
                "runtime_manifest_sha256": rt_sha_1,
                "experience_policy": {"version": 1, "kind": "audit"},
                "review_context": {
                    "release_acceptance": True,
                    "no_work_task": True,
                    "host": {"id": "host-agy", "entry": "cli:agy", "model": "gemini-pro"},
                },
            }
            snap_canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            snap_sha = hashlib.sha256(snap_canonical.encode("utf-8")).hexdigest()
            result_text = "宿主审查结论：PASS\n\nHost verified.\n\n池节点审查结论：\n\npool-qoder: FINAL_ACCEPTANCE: PASS\n证据引用: task:acc-host-1\n审查结论: PASS"
            result_sha = hashlib.sha256(result_text.encode("utf-8")).hexdigest()
            host_receipt = {
                "receipt_id": "host-r1",
                "task_id": "acc-host-1",
                "reviewer_id": "host-agy",
                "snapshot_sha256": snap_sha,
                "verdict": "PASS",
                "findings": "Host independent review findings",
                "evidence_refs": ["task:acc-host-1"],
                "review_type": "new_experience",
                "score": 9,
                "applicability": "v1.6.16 release",
                "started_at": 100.0,
                "finished_at": 110.0,
            }
            host_receipt_sha = hashlib.sha256(
                json.dumps(host_receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            prov = {
                "kind": "host_review",
                "host_id": "host-agy",
                "verdict": "PASS",
                "memory_disposition": "audit_only",
                "result_sha256": result_sha,
                "pool_run_ids": ["run-qoder-1"],
            }
            payload = {
                "task": {
                    "id": "acc-host-1",
                    "prompt": "Release acceptance",
                    "status": "completed",
                    "execution_mode": "production",
                    "result": result_text,
                    "provenance_json": json.dumps(prov),
                    "execution_snapshot_json": snap_canonical,
                    "execution_snapshot_hash": snap_sha,
                },
                "runs": [
                    {
                        "id": "run-qoder-1",
                        "agent_name": "pool-qoder",
                        "status": "completed",
                        "output": "证据引用: task:acc-host-1\n审查结论: PASS\nFINAL_ACCEPTANCE: PASS\nIndependent pool review.",
                    }
                ],
                "memory_candidate": None,
                "audit_settlement": {"result_sha256": result_sha},
                "host_settlement": {
                    "receipt": host_receipt,
                    "receipt_sha256": host_receipt_sha,
                    "provenance": prov,
                    "pool_run_ids": ["run-qoder-1"],
                },
                "pool_nodes": [
                    {"node_id": "pool-qoder", "role": "review", "enabled": True, "test_passed": True,
                     "interface": "cli", "entry": "cli:qoder", "model": "qwen-max", "agent_type": "qoder"},
                ],
                "execution": {"author_ids": ["codex"], "executor_ids": ["codex"]},
            }

            (src / "docs/RELEASE_V1.6.16.md").write_text("# Updated release prose\n", encoding="utf-8")
            entries_2 = cm.collect_entries(src)
            (src / "SOURCE_MANIFEST.json").write_text(
                json.dumps({"algorithm": "sha256", "files": entries_2}, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            checked = build_release._validate_acceptance_payload(payload, src)
            self.assertEqual("acc-host-1", checked["task_id"])

            (src / "src/xiaoba_agentos/code.py").write_text("VALUE = 2\n", encoding="utf-8")
            entries_3 = cm.collect_entries(src)
            (src / "SOURCE_MANIFEST.json").write_text(
                json.dumps({"algorithm": "sha256", "files": entries_3}, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "not bound to the current source manifest"):
                build_release._validate_acceptance_payload(payload, src)

    def test_monitor_permanent_total_ranking_isolates_unbacked_and_unhashable_payloads(self):
        from xiaoba_agentos.cli import main as cli_main
        from xiaoba_agentos.delivery import register_direct
        from xiaoba_agentos.experience_loop import record_application
        from xiaoba_agentos.reuse_scores import projection_tx
        from xiaoba_agentos.store import EventStore

        mon_dir = REPO_ROOT / "architecture-package-20261027/shared/工具/小八监看"
        if str(mon_dir) not in sys.path:
            sys.path.insert(0, str(mon_dir))
        from monitor_core.modules import overview as overview_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cfg = _make_fully_isolated_config(root / "proj", [])
            store = EventStore(cfg.database_path)
            store.initialize()

            vault = Path(cfg.raw["memory"]["obsidian_vault"])
            src_id = store.submit("TEST formal p7", 2)
            store.complete(src_id, "TEST source answer", expected_attempt=0)
            pub_note = vault / "formal_p7.md"
            pub_bytes = b"# Formal P7\nSteps.\n"
            pub_sha = hashlib.sha256(pub_bytes).hexdigest()
            pub_note.write_bytes(pub_bytes)
            outbox = cfg.runtime_dir / "memory-outbox"
            outbox.mkdir(parents=True, exist_ok=True)
            staged = outbox / f"{src_id}.md"
            staged.write_bytes(pub_bytes)
            store.stage_memory_candidate(src_id, str(staged), pub_sha, [src_id])
            with store.connect() as conn:
                conn.execute(
                    "UPDATE memory_candidates SET status='published',published_path=?,published_at=? WHERE task_id=?",
                    (str(pub_note), time.time(), src_id),
                )

            ws = root / "p7-ws"
            ws.mkdir()
            art = ws / "out.md"
            art_bytes = f"Output citing {src_id}.\n".encode("utf-8")
            art.write_bytes(art_bytes)
            art_ref = {"version": "v1", "path": str(art), "sha256": hashlib.sha256(art_bytes).hexdigest()}
            res_ref = {"path": str(art), "sha256": art_ref["sha256"]}
            real_task = {"id": "rt-p7", "workspace": str(ws), "authorization_ref": "u://1", "description": "p7"}
            register_direct(store, {"version": 1, "real_task": real_task, "artifact": art_ref, "result": res_ref})
            record_application(store, {
                "application_id": "app-p7",
                "candidate_id": src_id,
                "candidate_sha256": pub_sha,
                "formal_reuse": True,
                "conditions_checked": {"applicability": "matched", "exclusions": "none", "environment": "macos"},
                "application_location": str(art),
                "steps_applied": "Applied",
                "deviations": "none",
                "reuse_assessment": {"outcome": "unchanged", "changed": "", "added": "", "discarded": ""},
                "real_task": dict(real_task, problem_key="p7-key", task_type="doc"),
                "artifact": art_ref,
                "execution_evidence": [art_ref],
            })

            accept_json = root / "accept_p7.json"
            accept_json.write_text(json.dumps({
                "origin": "user",
                "user_message": "交付",
                "message_id": "msg-p7-accept",
                "source_ref": "chat://p7",
                "real_task_id": "rt-p7",
                "artifact": art_ref,
            }, ensure_ascii=False), encoding="utf-8")
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, cli_main(["--config", str(cfg.path), "memory-delivery-record", "--receipt", str(accept_json), "--user-decision"]))

            hex_c = "c".ljust(64, "c")
            with store.connect() as conn:
                deliv_row = conn.execute("SELECT event_id FROM events WHERE event_type='task.delivery_recorded' ORDER BY sequence DESC LIMIT 1").fetchone()
                score_row = conn.execute("SELECT event_id FROM events WHERE event_type='memory.reuse_scored' ORDER BY sequence DESC LIMIT 1").fetchone()
                # 1) Unbacked reversal (missing withdrawal event)
                store._append_event_tx(conn, src_id, "memory.reuse_reversed", {
                    "source_task_id": src_id,
                    "real_task_id": "rt-p7",
                    "score_event_id": score_row["event_id"],
                    "contribution": 1,
                    "delivery_event_id": "non-existent-withdrawal-event",
                    "withdraws_delivery_event_id": deliv_row["event_id"],
                })
                # 2) Unhashable application_ids=[{}] in reuse_scored (r2-3a)
                store._append_event_tx(conn, "other-src", "memory.reuse_scored", {
                    "source_task_id": "other-src",
                    "real_task_id": "rt-p7",
                    "score": 1,
                    "used_at": 1000.0,
                    "delivery_event_id": deliv_row["event_id"],
                    "application_ids": [{}],
                    "source_sha256": hex_c,
                })
                # 3) Unhashable delivery_event_id={} in reuse_reversed (r2-3b)
                store._append_event_tx(conn, src_id, "memory.reuse_reversed", {
                    "source_task_id": src_id,
                    "real_task_id": "rt-p7",
                    "score_event_id": score_row["event_id"],
                    "contribution": 1,
                    "delivery_event_id": {},
                    "withdraws_delivery_event_id": deliv_row["event_id"],
                })

                # 4) Huge integer used_at = pow(10, 400) causing OverflowError in math.isfinite (r3-P2-3)
                store._append_event_tx(conn, "huge-ts-src", "memory.reuse_scored", {
                    "source_task_id": "huge-ts-src",
                    "real_task_id": "rt-p7",
                    "score": 1,
                    "used_at": pow(10, 400),
                    "delivery_event_id": deliv_row["event_id"],
                    "application_ids": [],
                    "source_sha256": hex_c,
                })

            bundled_ov_path = ROOT / "src/xiaoba_agentos/review_ui_assets/monitor_overview.py"
            shared_ov_path = REPO_ROOT / "architecture-package-20261027/shared/工具/小八监看/monitor_core/modules/overview.py"
            self.assertEqual(shared_ov_path.read_bytes(), bundled_ov_path.read_bytes())

            self.assertTrue(store.verify_event_chain()["ok"])
            with store.read_only() as conn:
                core_net = projection_tx(conn)[src_id]["score"]
                mon_rank = overview_mod.permanent_total_ranking(conn)
            self.assertEqual(1, core_net)
            self.assertEqual(1, mon_rank["items"][0]["score"])
            self.assertGreaterEqual(len(mon_rank["problems"]), 4)


class Round3AdversarialFixesTests(unittest.TestCase):
    """r3 independent review findings: P1-1 (entrypoint binding), P1-2 (direct V2 vs accepted V1), P2-4 (strict isolation & disabled index)."""

    def test_r3_p1_1_tampered_stable_scripts_rejected_in_build_and_switch_all_tiers(self):
        import argparse
        import importlib.util
        from unittest.mock import patch
        from test_build_boundary import Fixture as BuildFixture, write as bwrite
        from test_switch_release import Fixture as SwitchFixture, seal as sseal, switch, write as swrite

        spec = importlib.util.spec_from_file_location("br_r3_check", SCRIPTS / "build_release.py")
        br = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(br)

        canonical_script = (
            '#!/usr/bin/env python3\n'
            'from pathlib import Path\n'
            'DEFAULT_CONFIG = Path("/canonical/agentos.json")\n'
            'print("ok")\n'
        )
        bound_script = (
            '#!/usr/bin/env python3\n'
            'from pathlib import Path\n'
            'DEFAULT_CONFIG = Path("/bound/runtime/active.json")\n'
            'print("ok")\n'
        )
        tampered_script = bound_script + 'print("tampered")\n'

        # 1) build_release._assemble_package with acceptance_complete=True
        with tempfile.TemporaryDirectory() as tmp:
            bfx = BuildFixture(Path(tmp).resolve())
            frozen_scripts = bfx.source / ".agents/skills/xiaoba-agentos/scripts"
            for name in ("xiaoba", "verify-release"):
                bwrite(frozen_scripts / name, canonical_script)
                bwrite(bfx.stable / name, tampered_script if name == "xiaoba" else bound_script)

            args_bad = bfx.args(acceptance_complete=True, test_result="passed")
            staging_bad = Path(tmp).resolve() / "staging-bad"
            staging_bad.mkdir()
            with patch.object(br, "run_verify"):
                with self.assertRaisesRegex(RuntimeError, "differs from the frozen script"):
                    br._assemble_package(args_bad, bfx.source, staging_bad, {"tier": "regular"}, False, {"ok": True})

            # Valid bound DEFAULT_CONFIG passes _assemble_package
            bwrite(bfx.stable / "xiaoba", bound_script)
            staging_ok = Path(tmp).resolve() / "staging-ok"
            staging_ok.mkdir()
            with patch.object(br, "run_verify"):
                rel = br._assemble_package(args_bad, bfx.source, staging_ok, {"tier": "regular"}, False, {"ok": True})
            self.assertEqual("verified_immutable_release", rel["status"])

        # 2) switch_release.verify_package & preflight reject tampered interface/scripts on non-patch (full/regular) tier
        with tempfile.TemporaryDirectory() as tmp:
            sfx = SwitchFixture(Path(tmp).resolve())
            for name in ("xiaoba", "verify-release"):
                swrite(sfx.layout.package / "agentos/.agents/skills/xiaoba-agentos/scripts" / name, canonical_script)
                swrite(sfx.layout.package / "interface/scripts" / name, tampered_script if name == "xiaoba" else bound_script)
            sseal(sfx.layout.package)
            with self.assertRaisesRegex(switch.SwitchError, "interface script differs from reviewed source: xiaoba"):
                switch.verify_package(sfx.layout.package)
            res = switch.preflight(sfx.layout, sfx.system, now=lambda: 1000)
            self.assertFalse(res["ok"], res)
            self.assertIn("interface script differs from reviewed source: xiaoba", res.get("error", ""))

            # Restoring matching script passes both verify_package and preflight
            swrite(sfx.layout.package / "interface/scripts/xiaoba", bound_script)
            sseal(sfx.layout.package)
            self.assertEqual("v9.0.1", switch.verify_package(sfx.layout.package)["version"])
            res_ok = switch.preflight(sfx.layout, sfx.system, now=lambda: 1000)
            self.assertTrue(res_ok["ok"], res_ok)

    def test_r3_p1_2_direct_v2_registration_keeps_accepted_v1_visible_and_withdrawable(self):
        import importlib.util
        from xiaoba_agentos.delivery import detail_tx, pending_tasks, register_direct
        from xiaoba_agentos.experience_loop import record_application
        from xiaoba_agentos.reuse_scores import projection_tx
        from xiaoba_agentos.store import EventStore

        mon_dir = REPO_ROOT / "architecture-package-20261027/shared/工具/小八监看"
        if str(mon_dir) not in sys.path:
            sys.path.insert(0, str(mon_dir))
        from monitor_core.modules import overview as overview_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cfg = _make_fully_isolated_config(root / "proj", [])
            store = EventStore(cfg.database_path)
            store.initialize()

            cand_id = store.submit("TEST source v1v2", 2)
            store.complete(cand_id, "TEST v1v2 source answer", expected_attempt=0)
            pub_note = Path(cfg.memory["obsidian_vault"]) / f"70_复利规则/经验/published-{cand_id}.md"
            pub_note.parent.mkdir(parents=True, exist_ok=True)
            pub_bytes = f"---\nstatus: active\nsource_task_id: {cand_id}\n---\n# Formal V1V2\n".encode("utf-8")
            pub_note.write_bytes(pub_bytes)
            pub_sha = hashlib.sha256(pub_bytes).hexdigest()
            outbox = cfg.runtime_dir / "memory-outbox"
            outbox.mkdir(parents=True, exist_ok=True)
            staged = outbox / f"{cand_id}.md"
            staged.write_bytes(pub_bytes)
            store.stage_memory_candidate(cand_id, str(staged), pub_sha, [cand_id])
            with store.connect() as conn:
                conn.execute(
                    "UPDATE memory_candidates SET status='published', published_path=?, published_at=? WHERE task_id=?",
                    (str(pub_note), time.time(), cand_id),
                )

            ws = root / "ws-v1v2"
            ws.mkdir()
            v1_md = ws / "out_v1.md"
            v1_bytes = f"# Output V1 citing {cand_id}\n".encode("utf-8")
            v1_md.write_bytes(v1_bytes)
            v1_sha = hashlib.sha256(v1_bytes).hexdigest()
            art_v1 = {"version": "v1", "path": str(v1_md), "sha256": v1_sha}
            res_v1 = {"path": str(v1_md), "sha256": v1_sha}
            real_task = {
                "id": "direct-v1v2",
                "workspace": str(ws),
                "authorization_ref": "user://direct-v1v2",
                "description": "TEST direct V1 then V2",
            }
            register_direct(store, {"version": 1, "real_task": real_task, "artifact": art_v1, "result": res_v1})
            record_application(store, {
                "candidate_id": cand_id,
                "candidate_sha256": pub_sha,
                "conditions_checked": {"applicability": "matched", "exclusions": "none", "environment": "macos"},
                "application_location": str(v1_md),
                "steps_applied": "Applied V1",
                "deviations": "none",
                "reuse_assessment": {"outcome": "unchanged", "changed": "", "added": "", "discarded": ""},
                "real_task": dict(real_task, problem_key="v1v2-key", task_type="doc"),
                "artifact": art_v1,
                "execution_evidence": [art_v1],
            })

            env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")
            acc_v1 = root / "accept_v1.json"
            acc_v1.write_text(json.dumps({
                "origin": "user",
                "user_message": "交付",
                "message_id": "msg-accept-v1",
                "source_ref": "chat://v1",
                "real_task_id": "direct-v1v2",
                "artifact": art_v1,
            }, ensure_ascii=False), encoding="utf-8")
            p_acc = subprocess.run(
                [sys.executable, "-B", "-m", "xiaoba_agentos.cli", "--config", str(cfg.path),
                 "memory-delivery-record", "--receipt", str(acc_v1), "--user-decision"],
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(0, p_acc.returncode, p_acc.stderr + p_acc.stdout)
            with store.read_only() as conn:
                self.assertEqual(1, projection_tx(conn)[cand_id]["score"])

            # Now register unaccepted direct V2!
            v2_md = ws / "out_v2.md"
            v2_bytes = f"# Output V2 citing {cand_id}\n".encode("utf-8")
            v2_md.write_bytes(v2_bytes)
            v2_sha = hashlib.sha256(v2_bytes).hexdigest()
            art_v2 = {"version": "v2", "path": str(v2_md), "sha256": v2_sha}
            res_v2 = {"path": str(v2_md), "sha256": v2_sha}
            register_direct(store, {"version": 1, "real_task": real_task, "artifact": art_v2, "result": res_v2})

            # 1) Default pending_tasks view shows BOTH V1 (withdraw) and V2 (accept)!
            listed = pending_tasks(store)
            self.assertEqual([], listed["problems"])
            by_ver = {it["artifact"]["version"]: it["allowed_decisions"] for it in listed["items"] if it["task_id"] == "direct-v1v2"}
            self.assertEqual({"v1": ["withdraw"], "v2": ["accept"]}, by_ver)

            # 2) Default task-delivery-view --task-id includes accepted_version=V1, and --version v1 queries V1 directly!
            p_default = subprocess.run(
                [sys.executable, "-B", "-m", "xiaoba_agentos.cli", "--config", str(cfg.path),
                 "task-delivery-view", "--task-id", "direct-v1v2"],
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(0, p_default.returncode, p_default.stderr + p_default.stdout)
            def_view = json.loads(p_default.stdout)
            self.assertEqual("v2", def_view["artifact"]["version"])
            self.assertEqual("v1", def_view["accepted_version"]["artifact"]["version"])
            self.assertEqual(["withdraw"], def_view["accepted_version"]["allowed_decisions"])

            p_v1_view = subprocess.run(
                [sys.executable, "-B", "-m", "xiaoba_agentos.cli", "--config", str(cfg.path),
                 "task-delivery-view", "--task-id", "direct-v1v2", "--version", "v1"],
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(0, p_v1_view.returncode, p_v1_view.stderr + p_v1_view.stdout)
            self.assertEqual(["withdraw"], json.loads(p_v1_view.stdout)["allowed_decisions"])

            with store.read_only() as conn:
                self.assertEqual(["withdraw"], detail_tx(conn, "direct-v1v2", art_v1)["allowed_decisions"])

            # 3) Real subprocess withdrawal of V1 succeeds and reverses score 1 -> 0!
            wd_v1 = root / "withdraw_v1.json"
            wd_v1.write_text(json.dumps({
                "origin": "user",
                "user_message": "撤回验收",
                "message_id": "msg-withdraw-v1",
                "source_ref": "chat://wd-v1",
                "real_task_id": "direct-v1v2",
                "artifact": art_v1,
            }, ensure_ascii=False), encoding="utf-8")
            p_wd = subprocess.run(
                [sys.executable, "-B", "-m", "xiaoba_agentos.cli", "--config", str(cfg.path),
                 "memory-delivery-record", "--receipt", str(wd_v1), "--user-decision"],
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(0, p_wd.returncode, p_wd.stderr + p_wd.stdout)

            with store.read_only() as conn:
                self.assertEqual(0, projection_tx(conn)[cand_id]["score"])
                self.assertEqual(0, overview_mod.permanent_total_ranking(conn)["items"][0]["score"])
                rev_cnt = conn.execute("SELECT COUNT(1) FROM events WHERE event_type='memory.reuse_reversed'").fetchone()[0]
                self.assertEqual(1, rev_cnt)
                # After V1 is withdrawn, trying to re-query/accept superseded V1 must fail with version changed!
                with self.assertRaisesRegex(ValueError, "direct completion product version changed"):
                    detail_tx(conn, "direct-v1v2", art_v1)

    def test_r3_p2_4_fully_isolated_config_and_disabled_retrieval_skip_index_reads(self):
        from unittest.mock import patch
        from xiaoba_agentos.knowledge import KnowledgeCatalog
        from xiaoba_agentos.memory import MemoryBridge

        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp).resolve() / "iso-proj"
            cfg = _make_fully_isolated_config(proj, [])
            dumped = json.dumps(cfg.raw, ensure_ascii=False)
            self.assertNotIn(str(Path.home()), dumped)

            bridge = MemoryBridge(cfg)
            with patch.object(MemoryBridge, "index_sha256", side_effect=AssertionError("must not read index when disabled")):
                m_res = bridge.retrieve_with_receipt("test query")
            self.assertEqual("disabled", m_res["retrieval_status"])
            self.assertIsNone(m_res["index_sha256"])

            cfg.knowledge_enabled = False
            catalog = KnowledgeCatalog(cfg)
            with patch.object(KnowledgeCatalog, "index_sha256", side_effect=AssertionError("must not read index when disabled")):
                k_res = catalog.retrieve_with_receipt("test query")
            self.assertEqual("disabled", k_res["retrieval_status"])
            self.assertIsNone(k_res["index_sha256"])


class Round4AdversarialFixesTests(unittest.TestCase):
    """r4 independent review findings: P1-1 (switch_release ticket-to-package manifest binding) & P2-2 (unpatched agent_contexts/_submit_task skip graph index when disabled)."""

    def test_r4_p1_1_manual_reseal_with_stale_acceptance_ticket_rejected_by_switch(self):
        from test_switch_release import Fixture as SwitchFixture, seal as sseal, switch, write as swrite

        with tempfile.TemporaryDirectory() as tmp:
            sfx = SwitchFixture(Path(tmp).resolve())
            pkg = sfx.layout.package
            # Baseline: valid ticket bound to real package manifest SHA passes preflight
            base_res = switch.preflight(sfx.layout, sfx.system, now=lambda: 1000)
            self.assertTrue(base_res["ok"], base_res)
            old_rel = json.loads((pkg / "RELEASE.json").read_text(encoding="utf-8"))
            old_sha = old_rel["agentos_source_manifest_sha256"]
            old_ev = old_rel["acceptance"]["end_to_end_evidence"]
            self.assertEqual(old_sha, old_ev["source_manifest_sha256"])
            self.assertTrue(switch.independent_acceptance(old_ev, expected_manifest_sha256=old_sha))

            # Mutate runtime source and manually reseal SOURCE_MANIFEST.json + RELEASE.json + FILES.sha256 while keeping old ticket
            swrite(pkg / "agentos/src/xiaoba_agentos/runtime.py", "VALUE = 'tampered-after-acceptance'\n")
            sseal(pkg, bind_evidence=False)
            new_rel = json.loads((pkg / "RELEASE.json").read_text(encoding="utf-8"))
            new_sha = new_rel["agentos_source_manifest_sha256"]
            self.assertNotEqual(old_sha, new_sha)
            self.assertEqual(old_sha, new_rel["acceptance"]["end_to_end_evidence"]["source_manifest_sha256"])

            # 1) independent_acceptance with expected_manifest_sha256 rejects stale ticket
            self.assertFalse(switch.independent_acceptance(
                new_rel["acceptance"]["end_to_end_evidence"], expected_manifest_sha256=new_sha
            ))
            # 2) verify_package standalone rejects stale ticket
            with self.assertRaisesRegex(switch.SwitchError, "acceptance evidence is not bound"):
                switch.verify_package(pkg)
            # 3) full-tier preflight rejects stale ticket
            bad_res = switch.preflight(sfx.layout, sfx.system, now=lambda: 1000)
            self.assertFalse(bad_res["ok"], bad_res)
            self.assertIn("not bound", bad_res.get("error", ""))

            # 4) r5-P2: Top-level source_manifest_sha256 updated to new_sha, but ONE conclusion retains old_sha!
            mixed_rel = json.loads((pkg / "RELEASE.json").read_text(encoding="utf-8"))
            mixed_ev = mixed_rel["acceptance"]["end_to_end_evidence"]
            mixed_ev["source_manifest_sha256"] = new_sha
            mixed_ev["conclusions"][0]["manifest_sha256"] = new_sha
            mixed_ev["conclusions"][1]["manifest_sha256"] = old_sha
            swrite(pkg / "RELEASE.json", switch.encoded(mixed_rel))
            files_payload = "".join(
                f"{sha}  ./{rel}\n" for rel, sha in switch.files(pkg).items() if rel != "FILES.sha256"
            )
            swrite(pkg / "FILES.sha256", files_payload)

            self.assertFalse(switch.independent_acceptance(mixed_ev, expected_manifest_sha256=new_sha))
            with self.assertRaisesRegex(switch.SwitchError, "acceptance conclusion is not bound"):
                switch.verify_package(pkg)
            bad_mixed = switch.preflight(sfx.layout, sfx.system, now=lambda: 1000)
            self.assertFalse(bad_mixed["ok"], bad_mixed)
            self.assertIn("not bound", bad_mixed.get("error", ""))

    def test_r4_p2_2_agent_contexts_and_submit_task_never_read_graph_index_when_knowlp_disabled(self):
        from xiaoba_agentos.cli import _submit_task
        from xiaoba_agentos.memory import MemoryBridge
        from xiaoba_agentos.store import EventStore

        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp).resolve() / "iso-r4"
            cfg = _make_fully_isolated_config(proj, [])
            store = EventStore(cfg.database_path)
            store.initialize()

            graph_dir = Path(cfg.memory["knowlp_graph_dir"])
            graph_dir.mkdir(parents=True, exist_ok=True)
            sentinel = graph_dir / "sentinel_graph.json"
            sentinel.write_text('{"sentinel": 1}\n', encoding="utf-8")
            # Make sentinel unreadable (0o000): any file read inside index_sha256() raises PermissionError immediately!
            os.chmod(sentinel, 0)
            try:
                bridge = MemoryBridge(cfg, store)
                r1 = bridge.retrieve_with_receipt("ordinary query")
                self.assertEqual("disabled", r1["retrieval_status"])
                self.assertIsNone(r1["index_sha256"])

                r2 = bridge.agent_contexts("ordinary query", ["node-a", "node-b"])
                self.assertEqual("disabled", r2["receipt"]["retrieval_status"])
                self.assertIsNone(r2["receipt"]["index_sha256"])

                tid = _submit_task(cfg, store, "TEST ordinary user task without patches")
                snap = json.loads(store.get_task(tid)["execution_snapshot_json"])
                self.assertEqual("disabled", snap["retrieval_receipt"]["retrieval_status"])
                self.assertIsNone(snap["retrieval_receipt"]["index_sha256"])

                env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")
                p_sub = subprocess.run(
                    [sys.executable, "-B", "-m", "xiaoba_agentos.cli", "--config", str(cfg.path),
                     "submit", "TEST subprocess submit when knowlp disabled"],
                    capture_output=True, text=True, env=env, check=False,
                )
                self.assertEqual(0, p_sub.returncode, p_sub.stderr + p_sub.stdout)
            finally:
                os.chmod(sentinel, 0o600)


if __name__ == "__main__":
    unittest.main()
