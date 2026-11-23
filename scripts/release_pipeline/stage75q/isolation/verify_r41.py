"""75Q r4.1 isolation: parameterized r5 leaf, post-switch same-package import, real-runner baseline refuse.
Keeps r4 evidence untouched. No production stage.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE))

from bind import PIN, Bind, write_bind  # noqa: E402
from leaf_contracts import (  # noqa: E402
    load_migration_checks_module,
    load_migration_declaration,
    parameterize_migration_checks,
    resolve_post_switch_package,
    run_declared_migration_checks,
    verify_same_package_modules,
)
import cli as stage_cli  # noqa: E402

MANAGER_ZERO_DB = Path(
    "/Users/suzhizhou/Documents/私人助理/工作区/小八架构修复-20261030/任务卡/"
    "主力接手-v1614-20261111/75Q-真实构包与复制演练工具/"
    "主力-r4迁移零新增对象实际叶函数反例/schema7-zero-new-index.sqlite3"
)
MANAGER_ZERO_RESULT = Path(
    "/Users/suzhizhou/Documents/私人助理/工作区/小八架构修复-20261030/任务卡/"
    "主力接手-v1614-20261111/75Q-真实构包与复制演练工具/"
    "主力-r4迁移零新增对象实际叶函数反例/结果.json"
)
WORK_SPEC = STAGE.parent / "migration-spec.json"
PREV_PKG = Path(
    "/Users/suzhizhou/Documents/私人助理/工作区/小八AgentOS-v1.6.14-发版/releases/v1614-r7-attempt5/v1.6.14"
)


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


def _bind(root: Path, *, migration_spec: Path | None = None, **extra: Any) -> dict:
    mig_path, mig_hash = _r5()
    work = root / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "BASE_READY.json").write_text("{}\n", encoding="utf-8")
    spec_src = migration_spec or WORK_SPEC
    spec_dst = work / "migration-spec.json"
    if spec_src.resolve() != spec_dst.resolve():
        shutil.copy2(spec_src, spec_dst)
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
        "attempt": 95,
        "previous_package": str(prev),
        "generated_brain": str(root / "brain"),
        "tool_manifests": [],
        "migration_checks": str(mig_path),
        "migration_checks_sha256": mig_hash,
        "migration_spec": str(spec_dst),
        "isolation_skip_baseline_evidence": True,
    }
    data.update(extra)
    for p in (root / "reports", root / "acceptance", root / "pkg"):
        p.mkdir(parents=True, exist_ok=True)
    return data


def test_zero_new_indexes_parameterized(evidence_dir: Path, cases: dict) -> None:
    """Reproduce manager counterexample, then pass with declared parameterization."""
    root = evidence_dir / "mig-zero"
    root.mkdir(parents=True, exist_ok=True)
    db = root / "schema7-zero-new-index.sqlite3"
    shutil.copy2(MANAGER_ZERO_DB, db)
    assert MANAGER_ZERO_RESULT.is_file()
    manager = json.loads(MANAGER_ZERO_RESULT.read_text(encoding="utf-8"))
    assert manager["Q_leaf_accepted"] is False
    assert manager["normal_r4_parameterized_accepted"] is True

    payload = _bind(root / "world")
    bind = Bind(payload)
    mc = load_migration_checks_module(bind)
    # Reproduce: load only, expected{} with new_indexes has no effect — still default INDEXES.
    bare_expected = {"schema_version": 7, "sha256": "", "new_indexes": {}}
    try:
        mc.assert_fingerprint(db, bare_expected)
        raise AssertionError("unparameterized r5 must refuse zero-new-index DB")
    except RuntimeError as exc:
        cases["reproduce_manager_counterexample"] = {
            "ok": True,
            "error": repr(exc),
            "actual_INDEXES": list(mc.INDEXES),
            "manager_actual_INDEXES": manager["actual_loaded_INDEXES"],
        }
        assert list(mc.INDEXES) == manager["actual_loaded_INDEXES"]

    # Fix path: declare from migration-spec, parameterize globals, then assert.
    declared = run_declared_migration_checks(bind, db)
    assert declared["ok"] is True
    assert declared["parameterized"]["declared_indexes"] == 0
    assert declared["parameterized"]["INDEXES"] == []
    assert declared["parameterized"]["EXPECTED_INDEX_SQL"] == {}
    assert declared["fingerprint"]["schema_version"] == 7
    cases["zero_new_indexes_positive"] = {
        "ok": True,
        "declaration": declared["declaration"],
        "parameterized": declared["parameterized"],
        "fingerprint": declared["fingerprint"],
        "db_sha256": _h(db),
    }

    # Negative: wrong schema_version declaration
    bad_spec = root / "bad-schema-spec.json"
    bad_spec.write_text(
        json.dumps({"schema_version": 6, "new_indexes": {}}) + "\n", encoding="utf-8"
    )
    bad_payload = _bind(root / "world-bad-schema", migration_spec=bad_spec)
    try:
        run_declared_migration_checks(Bind(bad_payload), db)
        raise AssertionError("wrong schema must refuse")
    except RuntimeError as exc:
        cases["wrong_schema_negative"] = {"ok": True, "error": repr(exc)}

    # Negative: declaration requires an index the zero DB lacks
    miss_spec = root / "missing-index-spec.json"
    miss_spec.write_text(
        json.dumps(
            {
                "schema_version": 7,
                "new_indexes": {
                    "idx_events_event_type_seq": (
                        "CREATE INDEX idx_events_event_type_seq ON events(event_type, sequence)"
                    )
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    miss_payload = _bind(root / "world-miss", migration_spec=miss_spec)
    try:
        run_declared_migration_checks(Bind(miss_payload), db)
        raise AssertionError("missing declared index must refuse")
    except RuntimeError as exc:
        cases["declared_index_missing_negative"] = {"ok": True, "error": repr(exc)}


def test_post_switch_same_package(evidence_dir: Path, cases: dict) -> None:
    root = evidence_dir / "post-switch"
    root.mkdir(parents=True, exist_ok=True)
    # Synthetic new package with core + review under same agentos/src
    pkg = root / "releases" / "v1.6.15"
    src = pkg / "agentos" / "src"
    (src / "xiaoba_agentos").mkdir(parents=True, exist_ok=True)
    (src / "xiaoba_review").mkdir(parents=True, exist_ok=True)
    (src / "xiaoba_agentos" / "__init__.py").write_text("", encoding="utf-8")
    (src / "xiaoba_agentos" / "store.py").write_text("class EventStore: pass\n", encoding="utf-8")
    (src / "xiaoba_agentos" / "workday_ledger.py").write_text("VERSION=1\n", encoding="utf-8")
    (src / "xiaoba_review" / "__init__.py").write_text("", encoding="utf-8")
    (src / "xiaoba_review" / "store.py").write_text(
        "def _ensure_pending_reference_columns(self): return None\n", encoding="utf-8"
    )
    (pkg / "RELEASE.json").write_text(
        json.dumps({"version": "v1.6.15", "release_tier": {"tier": "migration"}}) + "\n",
        encoding="utf-8",
    )
    # previous package decoy with different store bytes
    prev = root / "prev" / "v1.6.14"
    (prev / "agentos" / "src" / "xiaoba_agentos").mkdir(parents=True, exist_ok=True)
    (prev / "agentos" / "src" / "xiaoba_agentos" / "store.py").write_text(
        "class EventStore: pass  # previous\n", encoding="utf-8"
    )
    (prev / "RELEASE.json").write_text(
        json.dumps({"version": "v1.6.14"}) + "\n", encoding="utf-8"
    )

    payload = _bind(
        root / "world",
        previous_package=str(prev),
        releases_package_dir=str(pkg),
        version="v1.6.15",
        review_ingress_root=str(src),
        review_store_sha256=_h(src / "xiaoba_review" / "store.py"),
    )
    bind = Bind(payload)
    state = {"verified_package": str(pkg)}
    scene = {"runtime": str(root / "runtime")}
    resolved = resolve_post_switch_package(bind, state, scene)
    assert Path(resolved["package"]) == pkg.resolve()
    assert Path(resolved["agentos_src"]) == src.resolve()
    cases["post_switch_resolve"] = {"ok": True, "resolved": {k: str(v) if isinstance(v, Path) else v for k, v in resolved.items()}}

    # Import from new package only
    sys.path[:0] = [str(src)]
    import importlib

    for name in ("xiaoba_agentos", "xiaoba_agentos.store", "xiaoba_agentos.workday_ledger", "xiaoba_review", "xiaoba_review.store"):
        if name in sys.modules:
            del sys.modules[name]
    es = importlib.import_module("xiaoba_agentos.store")
    wl = importlib.import_module("xiaoba_agentos.workday_ledger")
    rs = importlib.import_module("xiaoba_review.store")
    origins = verify_same_package_modules(
        src, src, prev, event_store_mod=es, review_store_mod=rs, workday_ledger_mod=wl
    )
    assert origins["ok"] is True
    cases["post_switch_module_origins"] = origins

    # Negative: resolving when only previous exists must refuse
    empty_payload = _bind(
        root / "world-empty",
        previous_package=str(prev),
        releases_package_dir=str(root / "missing-pkg"),
        version="v1.6.15",
    )
    try:
        resolve_post_switch_package(
            Bind(empty_payload),
            {"verified_package": str(prev)},  # deliberately wrong previous as verified
            {},
        )
        raise AssertionError("previous_package as verified must refuse")
    except RuntimeError as exc:
        cases["post_switch_previous_refused"] = {"ok": True, "error": str(exc)}


def test_real_runner_cannot_skip_baseline(evidence_dir: Path, cases: dict) -> None:
    root = evidence_dir / "baseline-real"
    root.mkdir(parents=True, exist_ok=True)
    write_dir = root / "bind-write"
    write_dir.mkdir(parents=True, exist_ok=True)
    before = _listing(write_dir)
    payload = _bind(root / "world")
    assert payload.get("isolation_skip_baseline_evidence") is True
    try:
        stage_cli.run_stage(
            allow_real=True,
            runner=None,  # real default runner
            bind_payload=payload,
            skip_live=True,
            bind_write_dir=write_dir,
        )
        raise AssertionError("real runner must refuse isolation_skip")
    except SystemExit as exc:
        cases["real_runner_skip_refused"] = {"ok": True, "error": str(exc)}
    assert _listing(write_dir) == before
    assert not (write_dir / "STAGE75Q-BIND.json").exists()
    cases["real_runner_skip_refused"]["bind_not_written"] = True

    # baseline_require_freeze=False also refused for real runner
    before = _listing(write_dir)
    payload2 = _bind(root / "world2")
    payload2.pop("isolation_skip_baseline_evidence")
    payload2["baseline_ctx"] = {
        "work": str(root / "no-work"),
        "acceptance": str(root / "no-acc"),
        "candidate": str(root / "no-cand"),
        "reports": str(root / "no-rep"),
        "assigned": {"base": "x" * 40, "source_manifest_sha256": "y" * 64},
        "state": {"expected_suites": {"core": {"tests": 1, "skipped": 0}}},
    }
    payload2["baseline_require_freeze"] = False
    try:
        stage_cli.run_stage(
            allow_real=True,
            runner=None,
            bind_payload=payload2,
            skip_live=True,
            bind_write_dir=write_dir,
        )
        raise AssertionError("real runner must refuse baseline_require_freeze=False")
    except SystemExit as exc:
        cases["real_runner_no_freeze_refused"] = {"ok": True, "error": str(exc)}
    assert _listing(write_dir) == before
    assert not (write_dir / "STAGE75Q-BIND.json").exists()
    cases["real_runner_no_freeze_refused"]["bind_not_written"] = True


def main(evidence_dir: Path) -> int:
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    reports_q = evidence_dir.parent
    old_hashes = {}
    for rel in (
        "isolation-result.json",
        "isolation-r2/isolation-result.json",
        "isolation-r3/isolation-result.json",
        "isolation-r4/isolation-result.json",
        "isolation-resume/isolation-result.json",
    ):
        p = reports_q / rel
        if p.is_file():
            old_hashes[str(p)] = _h(p)

    report: dict[str, Any] = {
        "ok": False,
        "revision": "r4.1-parameterized-r5-postswitch-samepkg-baseline-bound",
        "cases": {},
        "manager_counterexample": str(MANAGER_ZERO_RESULT),
        "work_migration_spec_sha256": _h(WORK_SPEC),
        "pointer_migration_checks_sha256": _r5()[1],
    }
    try:
        synth = evidence_dir / "synthetic" / time.strftime("%Y%m%d-%H%M%S")
        synth.mkdir(parents=True, exist_ok=True)
        report["synthetic_root"] = str(synth)
        test_zero_new_indexes_parameterized(synth, report["cases"])
        test_post_switch_same_package(synth, report["cases"])
        test_real_runner_cannot_skip_baseline(synth, report["cases"])
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
        tool_hashes = {
            p.relative_to(STAGE).as_posix(): _h(p)
            for p in sorted(STAGE.rglob("*.py"))
            if p.is_file() and "__pycache__" not in p.parts
        }
        (evidence_dir / "stage75q-tool-hashes.json").write_text(
            json.dumps(tool_hashes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (evidence_dir / "tool-and-source-hashes.json").write_text(
            json.dumps({"stage75q": tool_hashes, "count": len(tool_hashes)}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
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
