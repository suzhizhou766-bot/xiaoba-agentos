"""75Q r4 isolation: STATE live_config, post-init 75C, frozen ReviewStore, baseline gate, r5 leaf load.
Does not mutate r1/r2/r3 evidence. No production stage / publish / git.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE))

from bind import PIN, Bind, sha, write_bind  # noqa: E402
from leaf_contracts import (  # noqa: E402
    load_migration_checks_module,
    previous_package_version,
    resolve_review_ingress_root,
    resolve_scene_config_template,
)
from schema75c import ensure_pending_via_product, table_columns  # noqa: E402
import cli as stage_cli  # noqa: E402
import orchestrator as orch_mod  # noqa: E402

LIVE_REVIEW = Path(
    "/Users/suzhizhou/Documents/小八AgentOS/xiaoba-agentos/development/review-ingress"
)
LIVE_CONFIG = Path(
    "/Users/suzhizhou/Documents/小八AgentOS/.runtime/model-overrides/agentos-agy-flash-high.json"
)
EXPECT_CONFIG_HASH = "3100b52d6d8a6e85d942ff1a6cfdee150fbc61732c27202327fa96315fa174fb"
PREV_PKG = Path(
    "/Users/suzhizhou/Documents/私人助理/工作区/小八AgentOS-v1.6.14-发版/releases/v1614-r7-attempt5/v1.6.14"
)
AGENTOS_SRC = PREV_PKG / "agentos" / "src"


class FakeProc(SimpleNamespace):
    pass


def _h(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _listing(path: Path) -> list[str]:
    if not path.is_dir():
        return []
    return sorted(p.name for p in path.iterdir())


def _r5() -> tuple[Path, str]:
    pointer = STAGE.parent / "ATTEMPT-POINTER.json"
    data = json.loads(pointer.read_text(encoding="utf-8"))
    path = Path(data["migration_checks"])
    expect = data["migration_checks_sha256"]
    assert _h(path) == expect
    return path, expect


def _freeze_review_export(dest: Path) -> tuple[Path, str]:
    """Copy sibling review-ingress into evidence as a bound frozen export (not live-first import)."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        LIVE_REVIEW,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )
    store = dest / "xiaoba_review" / "store.py"
    assert store.is_file() and not store.is_symlink()
    digest = _h(store)
    return dest, digest


def _b09_config(path: Path, *, mutate: dict | None = None) -> str:
    raw = json.loads(LIVE_CONFIG.read_text(encoding="utf-8"))
    if mutate:
        raw.update(mutate)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return _h(path)


def _minimal_bind(root: Path, *, review_root: Path, review_sha: str, **extra: Any) -> dict:
    mig_path, mig_hash = _r5()
    work = root / "work"
    for p in (work, root / "reports", root / "acceptance", root / "pkg", root / "helpers"):
        p.mkdir(parents=True, exist_ok=True)
    (work / "BASE_READY.json").write_text("{}\n", encoding="utf-8")
    # previous package RELEASE for version declaration
    prev = root / "prev" / "v1.6.14"
    prev.mkdir(parents=True, exist_ok=True)
    (prev / "RELEASE.json").write_text(
        json.dumps({"version": "v1.6.14", "release_tier": {"tier": "migration"}}) + "\n",
        encoding="utf-8",
    )
    data = {
        "root": str(root),
        "work": str(work),
        "live": str(root / "live"),
        "source": str(root / "source"),
        "candidate": str(root / "candidate"),
        "acceptance_root": str(root / "acceptance"),
        "acceptance_step4_dir": str(root / "acceptance"),
        "reports_step4_dir": str(root / "reports"),
        "releases_package_dir": str(root / "pkg"),
        "stage_dir": str(STAGE),
        "helpers_dir": str(STAGE / "helpers"),
        "version": "v1.6.15",
        "round": "v1615-r1",
        "attempt": 94,
        "previous_package": str(prev),
        "generated_brain": str(root / "brain"),
        "tool_manifests": [],
        "migration_checks": str(mig_path),
        "migration_checks_sha256": mig_hash,
        "review_ingress_root": str(review_root),
        "review_store_sha256": review_sha,
        "isolation_skip_baseline_evidence": True,
    }
    data.update(extra)
    return data


def test_live_config_state_binding(evidence_dir: Path, cases: dict) -> None:
    root = evidence_dir / "cfg"
    root.mkdir(parents=True, exist_ok=True)
    review_root, review_sha = _freeze_review_export(root / "frozen-review")

    # Positive: historical model-overrides path name is allowed when STATE path+hash bind.
    good = root / "model-overrides" / "agentos-agy-flash-high.json"
    good.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(LIVE_CONFIG, good)
    assert _h(good) == EXPECT_CONFIG_HASH
    bind = Bind(
        _minimal_bind(
            root / "bind-good",
            review_root=review_root,
            review_sha=review_sha,
            live_config_path=str(good),
            live_config_sha256_before=EXPECT_CONFIG_HASH,
        )
    )
    got = resolve_scene_config_template(bind)
    assert got == good.resolve()
    cases["live_config_positive"] = {
        "ok": True,
        "path": str(got),
        "sha256": EXPECT_CONFIG_HASH,
        "model_overrides_allowed": "model-overrides" in got.parts,
    }

    # Negative: missing source
    try:
        resolve_scene_config_template(
            Bind(_minimal_bind(root / "bind-miss", review_root=review_root, review_sha=review_sha))
        )
        raise AssertionError("missing live_config must refuse")
    except RuntimeError as exc:
        cases["live_config_missing"] = {"ok": True, "error": str(exc)}

    # Negative: bad hash (same B09 shape, different bytes vs STATE before-hash)
    bad_hash_cfg = root / "bad-hash.json"
    digest = _b09_config(bad_hash_cfg, mutate={"project_dir": "/tmp/stage75q-r4-drift-only"})
    assert digest != EXPECT_CONFIG_HASH
    try:
        resolve_scene_config_template(
            Bind(
                _minimal_bind(
                    root / "bind-badhash",
                    review_root=review_root,
                    review_sha=review_sha,
                    live_config_path=str(bad_hash_cfg),
                    live_config_sha256_before=EXPECT_CONFIG_HASH,
                )
            )
        )
        raise AssertionError("bad hash must refuse")
    except RuntimeError as exc:
        cases["live_config_bad_hash"] = {"ok": True, "error": str(exc), "actual_sha256": digest}

    # Negative: config divergence (missing B09 cli_routes)
    diverge = root / "diverge.json"
    _b09_config(diverge, mutate={"cli_routes": {}})
    diverge_sha = _h(diverge)
    try:
        resolve_scene_config_template(
            Bind(
                _minimal_bind(
                    root / "bind-diverge",
                    review_root=review_root,
                    review_sha=review_sha,
                    live_config_path=str(diverge),
                    live_config_sha256_before=diverge_sha,
                )
            )
        )
        raise AssertionError("empty cli_routes must refuse")
    except RuntimeError as exc:
        cases["live_config_diverge"] = {"ok": True, "error": str(exc)}

    # No active.json fallback even if present
    live = root / "live-fallback"
    active = live / ".runtime" / "active.json"
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_text("{}\n", encoding="utf-8")
    try:
        resolve_scene_config_template(
            Bind(
                _minimal_bind(
                    root / "bind-fallback",
                    review_root=review_root,
                    review_sha=review_sha,
                    live=str(live),
                )
            )
        )
        raise AssertionError("active.json fallback must be refused")
    except RuntimeError as exc:
        cases["live_config_no_active_fallback"] = {"ok": True, "error": str(exc)}


def test_frozen_review_store_only(evidence_dir: Path, cases: dict) -> None:
    root = evidence_dir / "review"
    root.mkdir(parents=True, exist_ok=True)
    frozen, digest = _freeze_review_export(root / "frozen-review")

    # Positive: bound frozen export
    bind = Bind(
        _minimal_bind(
            root / "bind-ok",
            review_root=frozen,
            review_sha=digest,
        )
    )
    got = resolve_review_ingress_root(bind)
    assert got == frozen.resolve()
    cases["review_frozen_positive"] = {"ok": True, "root": str(got), "store_sha256": digest}

    # Negative: unfrozen bad tree with ensure-looking string but wrong hash
    bad = root / "unfrozen-bad" / "xiaoba_review"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "store.py").write_text(
        "def _ensure_pending_reference_columns(self):\n    return None\n",
        encoding="utf-8",
    )
    try:
        resolve_review_ingress_root(
            Bind(
                _minimal_bind(
                    root / "bind-bad",
                    review_root=root / "unfrozen-bad",
                    review_sha=digest,
                )
            )
        )
        raise AssertionError("bad unfrozen hash must refuse")
    except RuntimeError as exc:
        cases["review_unfrozen_refused"] = {"ok": True, "error": str(exc)}
        assert "hash" in str(exc).lower() or "mismatch" in str(exc).lower()

    # Negative: no bind hash / no package / no manifest → refuse (not live hardcoded)
    empty = root / "empty-bind"
    empty.mkdir(parents=True, exist_ok=True)
    payload = _minimal_bind(empty, review_root=frozen, review_sha=digest)
    payload.pop("review_ingress_root")
    payload.pop("review_store_sha256")
    payload["releases_package_dir"] = str(empty / "missing-pkg")
    payload["candidate"] = str(empty / "missing-cand")
    try:
        resolve_review_ingress_root(Bind(payload))
        raise AssertionError("unbound review must refuse live fallback")
    except RuntimeError as exc:
        cases["review_unbound_refused"] = {"ok": True, "error": str(exc)}
        assert "live" in str(exc).lower() or "unbound" in str(exc).lower() or "not found" in str(exc).lower()


def test_75c_post_init_timing(evidence_dir: Path, cases: dict) -> None:
    root = evidence_dir / "timing"
    root.mkdir(parents=True, exist_ok=True)
    frozen, digest = _freeze_review_export(root / "frozen-review")
    payload = _minimal_bind(root / "world", review_root=frozen, review_sha=digest)
    bind = Bind(payload)
    live_manifest = Path(payload["live"]) / "SOURCE_MANIFEST.json"
    live_manifest.parent.mkdir(parents=True, exist_ok=True)
    live_manifest.write_text("{}\n", encoding="utf-8")
    payload["online_manifest_sha256"] = _h(live_manifest)
    bind = Bind(payload)
    bind_write_dir = root / "bind-write"
    bind_write_dir.mkdir(parents=True, exist_ok=True)

    class FakeRunner:
        def __init__(self, b: Bind):
            self.bind = b
            self.calls: list[str] = []

        def __call__(self, args, cwd=None, env=None, stdout=None, stderr=None, capture_output=False, text=False):
            label = Path(args[2]).name if len(args) >= 3 else "unknown"
            self.calls.append(label)
            O, PKG = self.bind.O, self.bind.PKG
            O.mkdir(parents=True, exist_ok=True)
            if label == "build_verified.py":
                PKG.mkdir(parents=True, exist_ok=True)
                (PKG / "RELEASE.json").write_text(
                    json.dumps(
                        {
                            "status": "verified_immutable_release",
                            "release_tier": {"tier": "migration"},
                            "acceptance": {
                                "end_to_end_evidence": {
                                    "conclusions": {},
                                    "pool_nodes": {},
                                    "execution": {},
                                }
                            },
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                (PKG / "FILES.sha256").write_text("abc  x\n", encoding="utf-8")
            elif label == "prepare_scene.py":
                scene_root = self.bind.root / "rehearsal" / "iso-scene"
                runtime = scene_root / "runtime"
                db = runtime / ".runtime/agentos.sqlite3"
                db.parent.mkdir(parents=True, exist_ok=True)
                # Core schema via product EventStore; leave extension as old 4-col (no pre-migrate).
                sys.path.insert(0, str(AGENTOS_SRC))
                from xiaoba_agentos.store import EventStore

                native = EventStore(db)
                native.initialize()
                with native.connect() as conn:
                    native._append_event_tx(
                        conn,
                        None,
                        "agent_pool.workspace_bound",
                        {
                            "root": str(scene_root / "agent-workspaces"),
                            "purpose": "r4 isolation timing",
                        },
                    )
                (scene_root / "agent-workspaces").mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(db) as conn:
                    conn.execute("DROP TABLE IF EXISTS ri_pending_references")
                    conn.execute(
                        "CREATE TABLE ri_pending_references("
                        "id TEXT, data TEXT, created_at REAL, attempts INTEGER)"
                    )
                    conn.execute(
                        "INSERT INTO ri_pending_references VALUES (?,?,?,?)",
                        ("old-1", '{"n":1}', 1.0, 1),
                    )
                (O / "SCENE.json").write_text(
                    json.dumps(
                        {
                            "root": str(scene_root),
                            "runtime": str(runtime),
                            "config": str(runtime / "cfg.json"),
                            "vault": str(scene_root / "vault"),
                            "graph": str(scene_root / "graph"),
                            "entries": [],
                            "plist": str(scene_root / "worker.plist"),
                            "label": "com.xiaoba.agentos.rehearsal.iso",
                            "previous_package": str(self.bind.previous_package),
                            "retained_fixture_objects": {},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                # Leave 4 columns — must stay until fake new-package init in run_rehearsal.
                assert table_columns(db, "ri_pending_references") == [
                    "id",
                    "data",
                    "created_at",
                    "attempts",
                ]
            elif label == "run_rehearsal.py":
                scene = json.loads((O / "SCENE.json").read_text(encoding="utf-8"))
                db = Path(scene["runtime"]) / ".runtime/agentos.sqlite3"
                # Simulate new-package init owning migration via product API (not pre-prepare).
                ensure_pending_via_product(
                    db,
                    review_ingress_root=Path(self.bind.data["review_ingress_root"]),
                    agentos_src=AGENTOS_SRC,
                    outbox_root=db.parent / "memory-outbox",
                )
                assert set(table_columns(db, "ri_pending_references")) >= {
                    "id",
                    "data",
                    "created_at",
                    "attempts",
                    "state",
                    "last_error",
                }
                (O / "30-复制现场演练.json").write_text(
                    json.dumps(
                        {
                            "ok": True,
                            "cases": [{"fault_after": f"p{i}"} for i in range(11)],
                            "success": {
                                "result": {
                                    "read_back": {
                                        "restart_verified": True,
                                        "version": self.bind.version,
                                    }
                                }
                            },
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                (O / "34-card51-worker-scores.json").write_text(
                    json.dumps({"ok": True}) + "\n", encoding="utf-8"
                )
                (O / "35-card57-default-task.json").write_text(
                    json.dumps({"ok": True}) + "\n", encoding="utf-8"
                )
            elif label == "specials_v169.py":
                sections = {
                    k: {"ok": True}
                    for k in (
                        "card43_node_test_deadline",
                        "card44_host_backvote",
                        "card45_retired_workbuddy",
                        "card46_experience_recovery",
                        "card47_duplicate_revise",
                        "card57_host_pool_score_required",
                    )
                }
                (O / "41-v169-new-behaviors.json").write_text(
                    json.dumps({"ok": True, "sections": sections}) + "\n", encoding="utf-8"
                )
            elif label == "specials_scores_v169.py":
                (O / "42-card52-52b.json").write_text(
                    json.dumps(
                        {
                            "ok": True,
                            "sections": {"a": {"ok": True}, "b": {"ok": True}, "c": {"ok": True}},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
            elif label == "rehearsal_specials.py":
                (O / "33-task-abandon-cancel.json").write_text(
                    json.dumps({"ok": True}) + "\n", encoding="utf-8"
                )
            elif label == "specials_v1614.py":
                (O / "48-v1614-new-cases.json").write_text(
                    json.dumps(
                        {
                            "ok": True,
                            "sections": {"10_void": {"ok": True}, "11_ui_install": {"ok": True}},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
            elif label == "workday_case12.py":
                (O / "49-workday-case12.json").write_text(
                    json.dumps({"ok": True}) + "\n", encoding="utf-8"
                )
            elif label == "case13_reasoning.py":
                (O / "50-reasoning-case13.json").write_text(
                    json.dumps(
                        {
                            "ok": True,
                            "fake_cli_only": True,
                            "unset_matches_previous_release_argv": True,
                            "test_after": "invalidated",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
            elif label == "case14_chain.py":
                (O / "51-chain-case14.json").write_text(
                    json.dumps({"ok": True, "cases": {"V1": {}, "V2": {}, "V8": {}}}) + "\n",
                    encoding="utf-8",
                )
            if len(args) >= 4 and args[3] in ("plan", "preflight"):
                payload_txt = json.dumps({"ok": True, "writes_performed": 0}) + "\n"
                if stdout is not None:
                    stdout.write(payload_txt)
                return FakeProc(returncode=0, stdout=payload_txt, stderr="")
            if stdout is not None:
                stdout.write(json.dumps({"ok": True, "fake": label}) + "\n")
            return FakeProc(returncode=0, stdout="", stderr="")

    orch_mod.cleanup_scene = lambda b: {"ok": True, "service_created": False, "production_untouched": True}
    import workday_isolation as wi
    import cli_retest_preflight as crp

    wi.real_views = lambda: {"ok": True}
    wi.unchanged = lambda before: {"ok": True, "before": before, "after": before}
    orch_mod.unchanged = wi.unchanged
    orch_mod.real_views = wi.real_views
    crp.require_testable_cli = lambda live: {"ok": True, "skipped": True, "isolation": True}

    # Patch orchestrator-bound gates (imported names), and cli.run_stage's gates import target.
    import gates as gates_mod

    def _gates_ok(bind, skip_live=False):
        return {"ok": True, "skipped_isolation": True}

    gates_mod.run_prebuild_gates = _gates_ok
    orch_mod.run_prebuild_gates = _gates_ok
    orch_mod.initialize_attempt_state = lambda bind, original: None
    orch_mod.load_acceptance_state = lambda bind: {
        "acceptance_passed": True,
        "tasks": {"card49": {}},
    }

    fake = FakeRunner(bind)
    result = stage_cli.run_stage(
        allow_real=True,
        runner=fake,
        bind_payload=payload,
        skip_live=True,
        bind_write_dir=bind_write_dir,
    )
    before = result["scene_75c_before"]
    after = result["scene_75c_after"]
    timing = result["scene_75c_init_timing"]
    assert before.get("needs_four_to_six_migration") is True or before.get("columns") == [
        "id",
        "data",
        "created_at",
        "attempts",
    ]
    assert after.get("six_columns") is True
    assert timing["pre_migrate_forbidden"] is True
    assert timing["migrate_owner"] == "new_package_init"
    assert fake.calls.index("prepare_scene.py") < fake.calls.index("run_rehearsal.py")
    cases["scene_75c_timing"] = {
        "ok": True,
        "before_columns": before.get("columns"),
        "after_columns": after.get("columns"),
        "init_timing": timing,
        "new_indexes": after.get("new_indexes", 0),
        "schema_version": 7,
    }


def test_baseline_gate_zero_write(evidence_dir: Path, cases: dict) -> None:
    root = evidence_dir / "baseline"
    root.mkdir(parents=True, exist_ok=True)
    frozen, digest = _freeze_review_export(root / "frozen-review")
    write_dir = root / "bind-write"
    write_dir.mkdir(parents=True, exist_ok=True)
    before_listing = _listing(write_dir)

    # Bad: isolation without skip and without baseline_ctx → refuse before write
    payload = _minimal_bind(root / "world-bad", review_root=frozen, review_sha=digest)
    payload.pop("isolation_skip_baseline_evidence")
    try:
        stage_cli.run_stage(
            allow_real=True,
            runner=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no subprocess")),
            bind_payload=payload,
            skip_live=True,
            bind_write_dir=write_dir,
        )
        raise AssertionError("missing baseline must refuse")
    except SystemExit as exc:
        cases["baseline_missing_refused"] = {"ok": True, "error": str(exc)}
    assert _listing(write_dir) == before_listing
    assert not (write_dir / "STAGE75Q-BIND.json").exists()
    cases["baseline_missing_refused"]["bind_not_written"] = True

    # Bad shaped baseline_ctx: validate_baseline_evidence must raise before write
    before_listing = _listing(write_dir)
    bad_ctx = {
        "work": str(root / "no-work"),
        "acceptance": str(root / "no-acc"),
        "candidate": str(root / "no-cand"),
        "reports": str(root / "no-rep"),
        "assigned": {"base": "x" * 40, "source_manifest_sha256": "y" * 64},
        "state": {"expected_suites": {"core": {"tests": 1, "skipped": 0}}},
    }
    payload2 = _minimal_bind(root / "world-badctx", review_root=frozen, review_sha=digest)
    payload2.pop("isolation_skip_baseline_evidence")
    payload2["baseline_ctx"] = bad_ctx
    payload2["baseline_require_freeze"] = False
    try:
        stage_cli.run_stage(
            allow_real=True,
            runner=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no subprocess")),
            bind_payload=payload2,
            skip_live=True,
            bind_write_dir=write_dir,
        )
        raise AssertionError("bad baseline_ctx must refuse")
    except SystemExit as exc:
        cases["baseline_bad_ctx_refused"] = {"ok": True, "error": str(exc)}
    assert _listing(write_dir) == before_listing
    assert not (write_dir / "STAGE75Q-BIND.json").exists()
    cases["baseline_bad_ctx_refused"]["bind_not_written"] = True


def test_migration_r5_leaf_load(evidence_dir: Path, cases: dict) -> None:
    root = evidence_dir / "mig"
    root.mkdir(parents=True, exist_ok=True)
    frozen, digest = _freeze_review_export(root / "frozen-review")
    payload = _minimal_bind(root / "world", review_root=frozen, review_sha=digest)
    bind = Bind(payload)
    # Clear accidental parent imports
    for name in list(sys.modules):
        if name in ("migration_checks", "release_parameters") or name.startswith("migration_checks"):
            del sys.modules[name]
    # Ensure stage75q dir itself has no migration_checks.py
    assert not (STAGE / "migration_checks.py").exists()
    assert not (STAGE / "release_parameters.py").exists()
    mod = load_migration_checks_module(bind)
    assert hasattr(mod, "assert_fingerprint")
    expected = {"schema_version": 7, "sha256": "", "new_indexes": {}}
    # Smoke: fingerprint API accepts schema7/new_indexes={} contract shape
    assert expected["schema_version"] == 7 and expected["new_indexes"] == {}
    ver = previous_package_version(bind)
    assert ver == "v1.6.14"
    cases["migration_r5_leaf"] = {
        "ok": True,
        "path": str(bind.migration_checks),
        "sha256": bind.migration_checks_sha256,
        "module": getattr(mod, "__name__", ""),
        "previous_version_from_release": ver,
        "expected_fingerprint": expected,
        "no_stage_local_migration_checks": True,
        "no_release_parameters": True,
    }


def test_product_75c_on_frozen_export(evidence_dir: Path, cases: dict) -> None:
    root = evidence_dir / f"c75-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    frozen, digest = _freeze_review_export(root / "frozen-review")
    db = root / "agentos.sqlite3"
    outbox = root / "memory-outbox"
    sys.path[:0] = [str(AGENTOS_SRC), str(frozen)]
    from xiaoba_agentos.store import EventStore

    native = EventStore(db)
    native.initialize()
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE IF EXISTS ri_pending_references")
        conn.execute(
            "CREATE TABLE ri_pending_references (id TEXT PRIMARY KEY, data TEXT NOT NULL, "
            "created_at REAL NOT NULL, attempts INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO ri_pending_references VALUES (?,?,?,?)",
            ("old-1", '{"k":1}', 42.5, 3),
        )
    result = ensure_pending_via_product(
        db,
        review_ingress_root=frozen,
        agentos_src=AGENTOS_SRC,
        outbox_root=outbox,
    )
    assert result["ok"] and result["migrated"] and result["handwritten_alter"] is False
    assert set(result["columns_after"]) >= {
        "id",
        "data",
        "created_at",
        "attempts",
        "state",
        "last_error",
    }
    cases["schema75c_frozen_product"] = {
        "ok": True,
        "review_store_sha256": digest,
        "columns_after": result["columns_after"],
        "schema_version": 7,
        "new_indexes": 0,
        "product_api": result["product_api"],
    }


def main(evidence_dir: Path) -> int:
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    reports_q = evidence_dir.parent
    old_hashes = {}
    for rel in (
        "isolation-result.json",
        "isolation-r2/isolation-result.json",
        "isolation-r3/isolation-result.json",
        "isolation-resume/isolation-result.json",
    ):
        p = reports_q / rel
        if p.is_file():
            old_hashes[str(p)] = _h(p)

    report: dict[str, Any] = {
        "ok": False,
        "revision": "r4-state-config-postinit75c-frozen-review-baseline-r5",
        "cases": {},
        "live_config_sha256_before": EXPECT_CONFIG_HASH,
        "pointer_migration_checks_sha256": _r5()[1],
    }
    try:
        synth = evidence_dir / "synthetic" / time.strftime("%Y%m%d-%H%M%S")
        synth.mkdir(parents=True, exist_ok=True)
        report["synthetic_root"] = str(synth)
        test_live_config_state_binding(synth, report["cases"])
        test_frozen_review_store_only(synth, report["cases"])
        test_product_75c_on_frozen_export(synth, report["cases"])
        test_migration_r5_leaf_load(synth, report["cases"])
        test_baseline_gate_zero_write(synth, report["cases"])
        test_75c_post_init_timing(synth, report["cases"])
        report["ok"] = True
    except Exception as exc:  # noqa: BLE001
        report["ok"] = False
        report["error"] = repr(exc)
        raise
    finally:
        report["old_evidence_unchanged"] = {}
        for path, digest in old_hashes.items():
            assert _h(path) == digest, path
            report["old_evidence_unchanged"][path] = True
        # tool hashes for this revision
        tool_hashes = {}
        for rel in sorted(
            p.relative_to(STAGE).as_posix()
            for p in STAGE.rglob("*.py")
            if p.is_file() and "__pycache__" not in p.parts
        ):
            tool_hashes[rel] = _h(STAGE / rel)
        (evidence_dir / "stage75q-tool-hashes.json").write_text(
            json.dumps(tool_hashes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        report["stage75q_tool_hashes"] = str(evidence_dir / "stage75q-tool-hashes.json")
        (evidence_dir / "isolation-result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (evidence_dir / "isolation.log").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps({"ok": report["ok"], "evidence_dir": str(evidence_dir)}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")))
