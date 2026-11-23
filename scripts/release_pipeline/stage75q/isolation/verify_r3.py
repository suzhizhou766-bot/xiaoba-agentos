"""75Q r3 isolation: real acceptance schema/validator, product 75C migrate, leaf contracts, pre-write gates.
Does not mutate r2 evidence or historical acceptance DB. No production stage.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE))

from bind import PIN, Bind, clean_env, sha, write_bind  # noqa: E402
from gates import run_prebuild_gates, verify_two_independent_votes  # noqa: E402
from leaf_contracts import (  # noqa: E402
    acceptance_marked_passed,
    load_scene_drain_task_ids,
    require_acceptance_passed,
    resolve_fake_dispatch,
    resolve_review_ingress_root,
    resolve_scene_config_template,
    resolve_scene_drain_path,
)
from schema75c import table_columns  # noqa: E402
import cli as stage_cli  # noqa: E402

HIST_STATE = Path(
    "/Users/suzhizhou/Documents/私人助理/工作区/小八AgentOS-v1.6.14-发版/acceptance-v1614-r7/STATE.json"
)
HIST_SOURCE = Path(
    "/Users/suzhizhou/Documents/私人助理/工作区/小八AgentOS-v1.6.14-发版/source-v1614-r7/development/integrated-candidate"
)
EXPECT_DB_HASH = "677c32cd8552894c2b9ba234a6e9a7435972432c4e59f379b4794309f7a39135"
FORBIDDEN_IN_LEAVES = (
    "work/v1614-r7/scene_drain.json",
    "model-overrides/agentos-agy-flash-high.json",
    "work/v1614-r7/round7b/fake_dispatch.py",
)


PRODUCT_CANDIDATE = Path('/Users/suzhizhou/Documents/小八AgentOS/xiaoba-agentos/development/integrated-candidate')
PRODUCT_REVIEW = PRODUCT_CANDIDATE.parent / 'review-ingress'


def _current_product_database(database: Path, action: str) -> dict[str, Any]:
    """Keep current migration imports separate from the historical validator."""
    if action not in {'initialize', 'migrate'}:
        raise ValueError('unknown product database action')
    program = '''
import json, sys
from pathlib import Path
stage, candidate, review, database, action = map(str, sys.argv[1:])
sys.path[:0] = [stage, str(Path(candidate) / 'src')]
if action == 'initialize':
    from xiaoba_agentos.store import EventStore
    EventStore(Path(database)).initialize()
    result = {'ok': True, 'product_api': 'xiaoba_agentos.store.EventStore'}
else:
    from schema75c import migrate_four_to_six
    result = migrate_four_to_six(
        Path(database), review_ingress_root=Path(review),
        agentos_src=Path(candidate) / 'src', outbox_root=Path(database).parent / 'outbox',
    )
print(json.dumps(result, ensure_ascii=False))
'''
    proc = subprocess.run(
        [str(PIN), '-B', '-c', program, str(STAGE), str(PRODUCT_CANDIDATE),
         str(PRODUCT_REVIEW), str(database), action],
        env=clean_env(), capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


def _h(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _listing(path: Path) -> list[str]:
    if not path.is_dir():
        return []
    return sorted(p.name for p in path.iterdir())


def _minimal_bind(
    candidate: Path,
    *,
    acceptance_root: Path,
    database: str,
    attempt: int = 99,
    synth_root: Path | None = None,
) -> dict:
    # Synthetic root ONLY under evidence_dir — never under historical DB parent.
    synth = Path(synth_root) if synth_root is not None else Path("/tmp/stage75q-r3-unused-root")
    work = synth / "work"
    for p in (work, synth / "reports", synth / "acceptance", synth / "pkg", synth / "helpers"):
        p.mkdir(parents=True, exist_ok=True)
    (work / "BASE_READY.json").write_text("{}\n", encoding="utf-8")
    return {
        "root": str(synth),
        "work": str(work),
        "live": str(synth / "live"),
        "source": str(candidate.parent.parent if candidate.name == "integrated-candidate" else candidate),
        "candidate": str(candidate),
        "acceptance_root": str(acceptance_root),
        "acceptance_step4_dir": str(synth / "acceptance"),
        "reports_step4_dir": str(synth / "reports"),
        "releases_package_dir": str(synth / "pkg"),
        "stage_dir": str(STAGE),
        "helpers_dir": str(STAGE / "helpers"),
        "version": "v1.6.15",
        "round": "v1615-r1",
        "attempt": attempt,
        "previous_package": str(synth / "prev"),
        "generated_brain": str(synth / "brain"),
        "tool_manifests": [],
        "review_ingress_root": str(resolve_review_ingress_root(candidate=candidate)),
    }


def test_reproduce_counterexample_then_pass(evidence_dir: Path, cases: dict) -> None:
    state = json.loads(HIST_STATE.read_text(encoding="utf-8"))
    db = Path(state["database"])
    before = _h(db)
    assert before == EXPECT_DB_HASH, (before, EXPECT_DB_HASH)
    side_before = _listing(db.parent)
    # Schema must be product: no entry/model
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(agent_runs)")]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "entry" not in cols and "model" not in cols
    assert "run_identity" not in tables
    cases["counterexample_schema"] = {"agent_runs": cols, "run_identity": False}

    bind = Bind(
        _minimal_bind(
            HIST_SOURCE,
            acceptance_root=HIST_STATE.parent,
            database=str(db),
            attempt=14,
            synth_root=evidence_dir / "bind-positive",
        )
    )
    # Positive: current gates must accept real artifact via product validator
    votes = verify_two_independent_votes(bind, state)
    after = _h(db)
    side_after = _listing(db.parent)
    assert after == before == EXPECT_DB_HASH
    assert side_after == side_before
    cases["historical_positive"] = {
        "ok": True,
        "votes": {k: votes[k] for k in ("ok", "validator", "task_id", "fictional_identity_columns", "agent_names")},
        "database_sha256": after,
        "database_unchanged": True,
        "parent_listing_unchanged": True,
    }


def test_negative_same_entry_model_real_validator(evidence_dir: Path, cases: dict) -> None:
    state = json.loads(HIST_STATE.read_text(encoding="utf-8"))
    src_db = Path(state["database"])
    before = _h(src_db)
    copy_dir = evidence_dir / "negative-same-indep"
    copy_dir.mkdir(parents=True, exist_ok=True)
    dst = copy_dir / "agentos.sqlite3"
    # Copy via backup so original stays untouched
    with closing(sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)) as src, closing(sqlite3.connect(dst)) as out:
        src.backup(out)
    assert _h(src_db) == before == EXPECT_DB_HASH

    bad_state = dict(state)
    bad_state["database"] = str(dst)
    bind = Bind(
        _minimal_bind(
            HIST_SOURCE,
            acceptance_root=HIST_STATE.parent,
            database=str(dst),
            attempt=15,
            synth_root=evidence_dir / "bind-negative",
        )
    )
    # Establish the positive control on this exact copy and binding. An already
    # invalid fixture must fail here, before any rejection can count as evidence
    # about independence. The validator must leave the copy unchanged as well.
    baseline_hash = _h(dst)
    baseline = verify_two_independent_votes(bind, bad_state)
    assert baseline["ok"] is True
    assert _h(dst) == baseline_hash

    # Mutate ONLY the copy: force both pool sources to identical independence_pair
    # (same agent_type + entry_program + model). Distinct agent_name alone must not pass.
    with closing(sqlite3.connect(dst)) as db, db:
        rows = list(db.execute("SELECT rowid, data FROM ri_sources ORDER BY rowid"))
        assert len(rows) >= 2
        shared_entry = "/bin/echo"
        shared_model = "SameModel-R3"
        for rowid, raw in rows[:2]:
            data = json.loads(raw)
            node = data.get("node") if isinstance(data.get("node"), dict) else data
            node["entry"] = shared_entry
            node["model"] = shared_model
            node["agent_type"] = "qoder"
            node["interface"] = "cli"
            node["entry_program"] = "cli:qoder"
            if "node" in data:
                data["node"] = node
            else:
                data = node
            db.execute("UPDATE ri_sources SET data=? WHERE rowid=?", (json.dumps(data, ensure_ascii=False), rowid))

    try:
        verify_two_independent_votes(bind, bad_state)
        raise AssertionError("same entry+model must fail via product validator")
    except RuntimeError as exc:
        # Only independence fields changed after the passing positive control;
        # the refusal is the result, and its prose is diagnostic rather than a
        # correctness predicate.
        cases["negative_same_entry_model"] = {
            "ok": True, "error": str(exc), "validator": "product",
            "same_copy_baseline_passed": True,
        }
    assert _h(src_db) == EXPECT_DB_HASH


def test_leaf_contracts(evidence_dir: Path, cases: dict) -> None:
    leaves = [
        STAGE / "helpers/prepare_scene.py",
        STAGE / "helpers/run_rehearsal.py",
        STAGE / "helpers/fake_dispatch.py",
        STAGE / "build_verified.py",
        STAGE / "leaf_contracts.py",
    ]
    forbidden_hits = {}
    for path in leaves:
        text = path.read_text(encoding="utf-8")
        hits = [s for s in FORBIDDEN_IN_LEAVES if s in text]
        if hits:
            forbidden_hits[path.name] = hits
    assert not forbidden_hits, forbidden_hits

    # acceptance_passed (P field) accepted
    require_acceptance_passed({"acceptance_passed": True})
    require_acceptance_passed({"card49_acceptance_passed": True})
    try:
        require_acceptance_passed({})
        raise AssertionError("must fail")
    except RuntimeError:
        pass

    # synthetic bind for path resolvers
    root = evidence_dir / "leaf-contract-world"
    work = root / "work"
    helpers = root / "helpers"
    live = root / "live/.runtime"
    for p in (work, helpers, live):
        p.mkdir(parents=True, exist_ok=True)
    (work / "BASE_READY.json").write_text("{}\n", encoding="utf-8")
    (work / "scene_drain.json").write_text(json.dumps({"task_ids": []}) + "\n", encoding="utf-8")
    (live / "active.json").write_text("{}\n", encoding="utf-8")
    config = root / 'model-overrides' / 'fixture.json'
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({'cli_routes': {'local_agy': {'mode': 'TEST'}},
                                  'release': {}, 'worker': {}}) + '\n', encoding='utf-8')
    shutil.copy2(STAGE / "helpers/fake_dispatch.py", helpers / "fake_dispatch.py")
    bind = Bind(
        {
            "root": str(root),
            "work": str(work),
            "live": str(root / "live"),
            "source": str(root),
            "candidate": str(root),
            "acceptance_root": str(root),
            "acceptance_step4_dir": str(root / "a"),
            "reports_step4_dir": str(root / "o"),
            "releases_package_dir": str(root / "pkg"),
            "stage_dir": str(STAGE),
            "helpers_dir": str(helpers),
            "version": "v1.6.15",
            "round": "v1615-r1",
            "attempt": 1,
            "previous_package": str(root / "prev"),
            "generated_brain": str(root / "brain"),
            'live_config_path': str(config),
            'live_config_sha256_before': _h(config),
        }
    )
    assert resolve_scene_drain_path(bind) == work / "scene_drain.json"
    assert load_scene_drain_task_ids(bind) == []
    assert resolve_scene_config_template(bind) == config.resolve()
    assert resolve_fake_dispatch(bind) == helpers / "fake_dispatch.py"
    leaf = cases.setdefault('leaf', {})
    leaf['bound_model_overrides_allowed'] = True
    # Directory names do not determine trust; STATE/bind path, hash and B09
    # shape do. An existing active.json cannot supply a missing binding.
    invalid_bindings = {
        'active_fallback_refused': {'live_config_path': None},
        'unbound_hash_refused': {'live_config_sha256_before': None},
        'config_hash_drift_refused': {'live_config_sha256_before': '0' * 64},
        'missing_config_refused': {'live_config_path': str(root / 'absent.json')},
    }
    for name, changes in invalid_bindings.items():
        try:
            resolve_scene_config_template(Bind({**bind.data, **changes}))
        except RuntimeError as exc:
            leaf[name] = str(exc)
        else:
            raise AssertionError(name + ' must fail')
    malformed = root / 'malformed-config.json'
    malformed.write_text(json.dumps({'cli_routes': {}, 'release': {}, 'worker': {}}) + '\n', encoding='utf-8')
    try:
        resolve_scene_config_template(Bind({**bind.data, 'live_config_path': str(malformed),
                                           'live_config_sha256_before': _h(malformed)}))
    except RuntimeError as exc:
        leaf['b09_routes_refused'] = str(exc)
    else:
        raise AssertionError('empty B09 cli_routes must fail')
    cases["leaf"]["ok"] = True
    cases["leaf"]["forbidden_cleared"] = True
    cases["leaf"]["acceptance_passed_ok"] = acceptance_marked_passed({"acceptance_passed": True})


def test_product_75c_migration(evidence_dir: Path, cases: dict) -> None:
    db = evidence_dir / f"c75-{uuid.uuid4().hex[:8]}" / "agentos.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    _current_product_database(db, 'initialize')
    # Seed old 4-column table with real rows BEFORE product ensure
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
        conn.execute(
            "INSERT INTO ri_pending_references VALUES (?,?,?,?)",
            ("old-2", '{"k":2}', 43.5, 0),
        )
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    assert version == 7, version
    assert table_columns(db, "ri_pending_references") == list(
        ("id", "data", "created_at", "attempts")
    )
    result = _current_product_database(db, 'migrate')
    assert result["ok"] and result["migrated"] and result["handwritten_alter"] is False
    assert float(result["rows_after"][0]["created_at"]) == 42.5
    assert json.loads(result["rows_after"][0]["data"]) == {"k": 1}
    assert int(result["rows_after"][0]["attempts"]) >= 3
    assert result["rows_after"][0]["state"] in ("pending", "dead")
    assert set(result["columns_after"]) >= {
        "id", "data", "created_at", "attempts", "state", "last_error"
    }
    cases["schema75c_product"] = {
        "ok": True,
        "product_api": result["product_api"],
        "columns_after": result["columns_after"],
        "schema_version": 7,
        "new_indexes": 0,
        "kept_ids": [r["id"] for r in result["rows_after"]],
        "states": [r["state"] for r in result["rows_after"]],
    }


def test_prewrite_gate_no_bind(evidence_dir: Path, cases: dict) -> None:
    world = evidence_dir / "prewrite"
    world.mkdir(parents=True, exist_ok=True)
    write_dir = world / "bind-write"
    write_dir.mkdir(parents=True, exist_ok=True)
    before_listing = _listing(write_dir)
    before_hashes = {str(p): _h(p) for p in write_dir.iterdir() if p.is_file()}
    # Bad payload: missing acceptance evidence / empty manifests — gates must fail before write
    bind_payload = _minimal_bind(
        HIST_SOURCE,
        acceptance_root=world / "missing-acceptance",
        database=str(world / "no.db"),
        attempt=77,
        synth_root=world / "bind-root",
    )
    (world / "missing-acceptance").mkdir(parents=True, exist_ok=True)
    calls = {"n": 0}

    def runner(*a, **k):
        calls["n"] += 1
        raise RuntimeError("subprocess must not run")

    try:
        stage_cli.run_stage(
            allow_real=True,
            runner=runner,
            bind_payload=bind_payload,
            skip_live=True,
            bind_write_dir=write_dir,
        )
    except SystemExit as exc:
        baseline_error = str(exc)
    else:
        raise AssertionError('missing baseline must fail before bind write')
    assert calls['n'] == 0 and _listing(write_dir) == before_listing

    # The custom runner permits this explicit isolation-only baseline skip.
    # Then the deliberately invalid prebuild evidence must still fail before
    # bind/marker creation, rather than being masked by the baseline gate.
    bind_payload['isolation_skip_baseline_evidence'] = True
    try:
        stage_cli.run_stage(
            allow_real=True, runner=runner, bind_payload=bind_payload,
            skip_live=True, bind_write_dir=write_dir,
        )
    except RuntimeError as exc:
        cases['prewrite'] = {'ok': True, 'error': repr(exc), 'subprocess_calls': calls['n'],
                             'missing_baseline_refused': baseline_error,
                             'prebuild_reached_after_isolation_baseline_skip': True}
    else:
        raise AssertionError('bad prebuild evidence must fail')
    assert calls["n"] == 0
    assert _listing(write_dir) == before_listing
    assert not (write_dir / "STAGE75Q-BIND.json").exists()
    after_hashes = {str(p): _h(p) for p in write_dir.iterdir() if p.is_file()}
    assert after_hashes == before_hashes
    cases["prewrite"]["listing_unchanged"] = True
    cases["prewrite"]["bind_not_written"] = True


def test_plan_zero_write(evidence_dir: Path, cases: dict) -> None:
    world = evidence_dir / "plan"
    world.mkdir(parents=True, exist_ok=True)
    payload = _minimal_bind(
        HIST_SOURCE,
        acceptance_root=HIST_STATE.parent,
        database=str(json.loads(HIST_STATE.read_text())["database"]),
        attempt=21,
        synth_root=world / "bind-root",
    )
    bind_path = world / "bind.json"
    write_bind(bind_path, payload)
    before = _h(bind_path)
    stage_before = _listing(STAGE)
    import subprocess

    code = subprocess.run(
        [str(PIN), "-B", str(STAGE / "cli.py"), "--plan", "--bind-json", str(bind_path)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert code.returncode == 0, code.stderr + code.stdout
    report = json.loads(code.stdout)
    assert report.get("writes") == 0 and report.get("read_only") is True
    assert _h(bind_path) == before
    assert _listing(STAGE) == stage_before
    cases["plan_zero_write"] = {"ok": True}


def main(evidence_dir: Path) -> int:
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    old_r2 = evidence_dir.parent / "isolation-r2" / "isolation-result.json"
    old_r2_hash = _h(old_r2) if old_r2.is_file() else None
    old_resume = evidence_dir.parent / "isolation-resume" / "isolation-result.json"
    old_resume_hash = _h(old_resume) if old_resume.is_file() else None
    old_first = evidence_dir.parent / "isolation-result.json"
    old_first_hash = _h(old_first) if old_first.is_file() else None
    report: dict[str, Any] = {"ok": False, "revision": "r3-real-db-leaf-contracts", "cases": {}}
    try:
        synth = evidence_dir / "synthetic" / time.strftime("%Y%m%d-%H%M%S")
        synth.mkdir(parents=True, exist_ok=True)
        report["synthetic_root"] = str(synth)
        test_reproduce_counterexample_then_pass(synth, report["cases"])
        test_negative_same_entry_model_real_validator(synth, report["cases"])
        test_leaf_contracts(synth, report["cases"])
        test_product_75c_migration(synth, report["cases"])
        test_prewrite_gate_no_bind(synth, report["cases"])
        test_plan_zero_write(synth, report["cases"])
        report["ok"] = True
    except Exception as exc:  # noqa: BLE001
        report["ok"] = False
        report["error"] = repr(exc)
        raise
    finally:
        if old_r2_hash is not None:
            assert _h(old_r2) == old_r2_hash
            report["old_r2_unchanged"] = True
        if old_resume_hash is not None:
            assert _h(old_resume) == old_resume_hash
            report["old_resume_unchanged"] = True
        if old_first_hash is not None:
            assert _h(old_first) == old_first_hash
            report["old_first_unchanged"] = True
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
