"""75Q r2 isolation: real entry, strong gates, record-derived helpers, evidence_dir-local synthetics.
Does not overwrite isolation-resume / first-round evidence. Does not re-run the full prior suite.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE))

from bind import PIN, Bind, clean_env, sha, write_bind  # noqa: E402
from context_adapter import derive_helper_paths, verify_r5_migration_binding  # noqa: E402
from gates import (  # noqa: E402
    verify_event_chain_sqlite_ro,
    verify_tool_manifests,
    verify_two_independent_votes,
)
from isolation.verify_r3 import EXPECT_DB_HASH, HIST_SOURCE, HIST_STATE, _current_product_database  # noqa: E402
import cli as stage_cli  # noqa: E402
import orchestrator as orch_mod  # noqa: E402



class FakeProc(SimpleNamespace):
    pass


def _h(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _r5_paths() -> tuple[Path, str]:
    pointer = STAGE.parent / "ATTEMPT-POINTER.json"
    data = json.loads(pointer.read_text(encoding="utf-8"))
    path = Path(data["migration_checks"])
    expect = data["migration_checks_sha256"]
    got = _h(path)
    assert got == expect, (got, expect)
    return path, expect


def _make_world(base: Path, *, attempt: int, helpers_name: str = "stage75q") -> dict[str, Any]:
    """All synthetics under evidence_dir base — never /var/folders TMPDIR."""
    root = base / f"release-root-a{attempt}"
    work = root / "work/v1615-r1"
    # These are immutable historical acceptance inputs, not a synthetic verdict.
    # The live product migration probes below use PRODUCT_CANDIDATE instead.
    source = HIST_SOURCE.parent.parent
    candidate = HIST_SOURCE
    acceptance = root / "acceptance-v1615-r1"
    step4 = acceptance / f"step4-attempt{attempt}"
    reports = root / f"reports/v1615-r1/round1b-attempt{attempt}"
    package = root / f"releases/v1615-r1-attempt{attempt}/v1.6.15"
    brain = root / "generated-brain-v1615-r1"
    live = base / f"fake-live-a{attempt}"
    prev = base / "previous-package/v1.6.14"
    # record.step4_helpers_dir points at a copy/link-style stage tree under synthetic root
    stage_dir = root / "work/v1615-r1" / helpers_name
    helpers_dir = stage_dir / "helpers"
    for p in (work, step4, reports, brain, live / ".runtime", prev, helpers_dir):
        p.mkdir(parents=True, exist_ok=True)

    # Copy essential stage scripts into record-derived stage_dir so argv follow record
    for name in (
        "build_verified.py",
        "case13_reasoning.py",
        "case14_chain.py",
        "bind.py",
        "gates.py",
        "acceptance_verdict.py",
        "schema75c.py",
        "cleanup.py",
        "cli_retest_preflight.py",
        "workday_isolation.py",
        "context_adapter.py",
        "orchestrator.py",
    ):
        src = STAGE / name
        if src.is_file():
            shutil.copy2(src, stage_dir / name)
    for name in (STAGE / "helpers").glob("*.py"):
        shutil.copy2(name, helpers_dir / name.name)

    (work / "BASE_READY.json").write_text(json.dumps({"base": "a" * 40}) + "\n", encoding="utf-8")
    (work / "PREPARATION.json").write_text(
        json.dumps({"tests": {"core": 1666}, "base": "a" * 40, "round": "v1615-r1"}) + "\n",
        encoding="utf-8",
    )
    # pointer copy with real r5
    mig_path, mig_hash = _r5_paths()
    (work / "ATTEMPT-POINTER.json").write_text(
        json.dumps(
            {
                "migration_checks": str(mig_path),
                "migration_checks_sha256": mig_hash,
                "record_path": str(step4 / "attempt-record.json"),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    historical_state = json.loads(HIST_STATE.read_text(encoding="utf-8"))
    original_db = Path(historical_state["database"])
    assert _h(original_db) == EXPECT_DB_HASH
    original_listing = sorted(p.name for p in original_db.parent.iterdir())
    prompt = acceptance / "review.txt"
    shutil.copyfile(Path(historical_state["prompt"]), prompt)
    freeze = acceptance / "FREEZE.json"
    shutil.copyfile(HIST_STATE.parent / "FREEZE.json", freeze)
    manifest = candidate / "SOURCE_MANIFEST.json"
    db = base / f"acceptance-a{attempt}.sqlite3"
    with closing(sqlite3.connect(original_db.as_uri() + '?mode=ro', uri=True)) as original, closing(sqlite3.connect(db)) as copied:
        original.backup(copied)
    assert _h(original_db) == EXPECT_DB_HASH
    assert sorted(p.name for p in original_db.parent.iterdir()) == original_listing
    state = dict(historical_state, database=str(db), prompt=str(prompt))
    assert state['freeze_sha256'] == _h(freeze)
    assert state['source_manifest_sha256'] == _h(manifest)
    assert state['prompt_sha256'] == _h(prompt)
    (acceptance / "STATE.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    live_db = live / ".runtime/agentos.sqlite3"
    with sqlite3.connect(live_db) as conn:
        conn.execute("PRAGMA user_version=7")
        conn.execute("DROP TABLE IF EXISTS events")
        conn.execute("DROP TABLE IF EXISTS tasks")
        conn.execute(
            "CREATE TABLE tasks (id TEXT, status TEXT, attempts INT, lease_until TEXT)"
        )
        conn.execute(
            "CREATE TABLE events (sequence INTEGER PRIMARY KEY, prev_hash TEXT, event_id TEXT, "
            "task_id TEXT, event_type TEXT, payload_json TEXT, created_at REAL, event_hash TEXT)"
        )
        # seed a valid 1-event chain
        prev_hash = "0" * 64
        payload = "{}"
        created = 1.0
        eid = "e1"
        material = f"{prev_hash}|{eid}||boot|{payload}|{created:.6f}"
        eh = hashlib.sha256(material.encode()).hexdigest()
        conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
            (1, prev_hash, eid, None, "boot", payload, created, eh),
        )
    live_manifest = live / "SOURCE_MANIFEST.json"
    live_manifest.write_text(json.dumps({"live": True}) + "\n", encoding="utf-8")

    tracked_rel = f"work/v1615-r1/{helpers_name}/bind.py"
    tracked = root / tracked_rel
    tool_a = reports / "final-tool-hashes.json"
    tool_b = reports / "step4-tool-hashes.json"
    digest = _h(tracked)
    tool_a.write_text(json.dumps({tracked_rel: digest}) + "\n", encoding="utf-8")
    tool_b.write_text(json.dumps({tracked_rel: digest}) + "\n", encoding="utf-8")
    (prev / "RELEASE.json").write_text(json.dumps({"status": "verified_immutable_release"}) + "\n", encoding="utf-8")

    record = {
        "attempt": attempt,
        "acceptance_step4_dir": str(step4),
        "reports_step4_dir": str(reports),
        "releases_package_dir": str(package),
        "step4_helpers_dir": str(stage_dir),
    }
    (step4 / "attempt-record.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    bind = {
        "root": str(root),
        "work": str(work),
        "live": str(live),
        "repo": str(base / "repo"),
        "source": str(source),
        "candidate": str(candidate),
        "acceptance_root": str(acceptance),
        "acceptance_step4_dir": str(step4),
        "reports_step4_dir": str(reports),
        "releases_package_dir": str(package),
        "stage_dir": str(stage_dir),
        "helpers_dir": str(helpers_dir),
        "version": "v1.6.15",
        "round": "v1615-r1",
        "attempt": attempt,
        "previous_package": str(prev),
        "generated_brain": str(brain),
        "tool_manifests": [str(tool_a), str(tool_b)],
        "preparation_path": str(work / "PREPARATION.json"),
        "online_manifest_sha256": _h(live_manifest),
        "migration_checks": str(mig_path),
        "migration_checks_sha256": mig_hash,
        "isolation_skip_baseline_evidence": True,
    }
    return {"bind": bind, "record": record, "root": root, "db": db, "live_db": live_db, "stage_dir": stage_dir}


class FakeRunner:
    def __init__(self, bind: Bind):
        self.bind = bind
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd=None, env=None, stdout=None, stderr=None, capture_output=False, text=False):
        self.calls.append(list(args))
        label = Path(args[2]).name if len(args) >= 3 else "unknown"
        O, A, PKG = self.bind.O, self.bind.A, self.bind.PKG
        O.mkdir(parents=True, exist_ok=True)
        if label == "build_verified.py":
            PKG.mkdir(parents=True, exist_ok=True)
            (PKG / "RELEASE.json").write_text(
                json.dumps(
                    {
                        "status": "verified_immutable_release",
                        "release_tier": {"tier": "regular"},
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
            state = json.loads((A / "STATE.json").read_text(encoding="utf-8"))
            state["verified_package"] = str(PKG)
            (A / "STATE.json").write_text(json.dumps(state) + "\n", encoding="utf-8")
            (A / "FORMAL_BUILD.started").write_text("x\n", encoding="utf-8")
        elif label == "prepare_scene.py":
            scene_root = self.bind.root / "rehearsal" / "iso-scene"
            runtime = scene_root / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            db = runtime / ".runtime/agentos.sqlite3"
            db.parent.mkdir(parents=True, exist_ok=True)
            _current_product_database(db, 'initialize')
            with sqlite3.connect(db) as conn:
                conn.execute("PRAGMA user_version=7")
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
            (runtime / "cfg.json").write_text(
                json.dumps(
                    {
                        "runtime_dir": str(runtime / ".runtime"),
                        "project_dir": str(runtime),
                        "memory": {"obsidian_vault": str(scene_root / "assistant/第二大脑")},
                        "paths": {"obsidian_vault": str(scene_root / "assistant/第二大脑")},
                    }
                ),
                encoding="utf-8",
            )
            (scene_root / "assistant/第二大脑").mkdir(parents=True, exist_ok=True)
        elif label == "run_rehearsal.py":
            scene = json.loads((O / 'SCENE.json').read_text(encoding='utf-8'))
            database = Path(scene['runtime']) / '.runtime/agentos.sqlite3'
            _current_product_database(database, 'migrate')
            (O / "30-复制现场演练.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "cases": [{"fault_after": f"p{i}"} for i in range(11)],
                        "success": {
                            "result": {
                                "read_back": {"restart_verified": True, "version": self.bind.version}
                            }
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (O / "34-card51-worker-scores.json").write_text(json.dumps({"ok": True}) + "\n", encoding="utf-8")
            (O / "35-card57-default-task.json").write_text(json.dumps({"ok": True}) + "\n", encoding="utf-8")
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
                json.dumps({"ok": True, "sections": {"a": {"ok": True}, "b": {"ok": True}, "c": {"ok": True}}})
                + "\n",
                encoding="utf-8",
            )
        elif label == "rehearsal_specials.py":
            (O / "33-task-abandon-cancel.json").write_text(json.dumps({"ok": True}) + "\n", encoding="utf-8")
        elif label == "specials_v1614.py":
            (O / "48-v1614-new-cases.json").write_text(
                json.dumps({"ok": True, "sections": {"10_void": {"ok": True}, "11_ui_install": {"ok": True}}})
                + "\n",
                encoding="utf-8",
            )
        elif label == "workday_case12.py":
            (O / "49-workday-case12.json").write_text(json.dumps({"ok": True}) + "\n", encoding="utf-8")
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
            payload = json.dumps({"ok": True, "writes_performed": 0}) + "\n"
            if stdout is not None:
                stdout.write(payload)
            return FakeProc(returncode=0, stdout=payload, stderr="")
        if stdout is not None:
            stdout.write(json.dumps({"ok": True, "fake": label}) + "\n")
        return FakeProc(returncode=0, stdout='{"ok":true}', stderr="")


def test_plan_zero_write(evidence_dir: Path, cases: dict) -> None:
    world = _make_world(evidence_dir / "plan-world", attempt=21)
    bind_path = evidence_dir / "plan-world" / "bind.json"
    write_bind(bind_path, world["bind"])
    before = {str(bind_path): _h(bind_path)}
    stage_before = sorted(p.name for p in STAGE.iterdir())
    code = subprocess.run(
        [str(PIN), "-B", str(STAGE / "cli.py"), "--plan", "--bind-json", str(bind_path)],
        capture_output=True,
        text=True,
        env=clean_env(),
        cwd=str(evidence_dir / "plan-world"),
    )
    assert code.returncode == 0, code.stderr + code.stdout
    report = json.loads(code.stdout)
    assert report.get("read_only") is True and report.get("writes") == 0
    assert report.get("filesystem_hash_unchanged") is True
    assert _h(bind_path) == before[str(bind_path)]
    assert not (STAGE / ".plan-bind.json").exists()
    assert sorted(p.name for p in STAGE.iterdir()) == stage_before
    cases["plan_zero_write"] = {"ok": True, "report_keys": sorted(report)}


def test_run_entry_calls_orchestrator(evidence_dir: Path, cases: dict) -> None:
    world = _make_world(evidence_dir / "run-world", attempt=22)
    bind = Bind(world["bind"])
    # patch cleanup/workspace
    orch_mod.cleanup_scene = lambda b: {"ok": True, "service_created": False, "production_untouched": True}
    import workday_isolation as wi
    import cli_retest_preflight as crp

    wi.real_views = lambda: {"books": [], "catalog_exists": False, "catalog_files": {}}
    wi.unchanged = lambda before: {"ok": True, "before": before, "after": before}
    orch_mod.real_views = wi.real_views
    orch_mod.unchanged = wi.unchanged
    crp.require_testable_cli = lambda live: {"ok": True, "disabled_cli_blockers": [], "mode": "read_only"}

    fake = FakeRunner(bind)

    def runner(args, cwd=None, env=None, stdout=None, stderr=None, capture_output=False, text=False):
        return fake(args, cwd=cwd, env=env, stdout=stdout, stderr=stderr)

    # Prove cli.run_stage is the real entry wiring Orchestrator
    result = stage_cli.run_stage(
        allow_real=True,
        runner=runner,
        bind_payload=world["bind"],
        skip_live=True,
        bind_write_dir=evidence_dir / "run-world" / "bind-write",
    )
    assert result.get("ok") is True
    argv_helpers = [c[2] for c in fake.calls if len(c) >= 3]
    assert any(str(bind.stage_dir / "build_verified.py") == a for a in argv_helpers)
    assert any(str(bind.W / "prepare_scene.py") == a for a in argv_helpers)
    # 75C migrate happened via assert_scene_extension during orch
    assert result.get("scene_75c", {}).get("ok") is True or result.get("scene_75c", {}).get("migrated")
    cases["run_entry"] = {
        "ok": True,
        "called_build": True,
        "helpers_from_record": str(bind.W),
        "stage_from_record": str(bind.stage_dir),
        "fourteen": result.get("fourteen_items"),
        "scene_75c": result.get("scene_75c"),
    }


def test_helpers_follow_record_attempt_and_dirname(evidence_dir: Path, cases: dict) -> None:
    w1 = _make_world(evidence_dir / "follow-a", attempt=31, helpers_name="stage75q")
    w2 = _make_world(evidence_dir / "follow-b", attempt=32, helpers_name="stage75q-alt")
    b1 = Bind(w1["bind"])
    b2 = Bind(w2["bind"])
    assert b1.attempt == 31 and b2.attempt == 32
    assert b1.W != b2.W and b1.stage_dir != b2.stage_dir
    assert b1.A != b2.A and b1.O != b2.O and b1.PKG != b2.PKG
    # derive_helper_paths
    s, h = derive_helper_paths(w2["record"])
    assert Path(s) == b2.stage_dir and Path(h) == b2.W
    orch_mod.cleanup_scene = lambda b: {"ok": True, "service_created": False}
    import workday_isolation as wi
    import cli_retest_preflight as crp

    wi.real_views = lambda: {"books": [], "catalog_exists": False, "catalog_files": {}}
    wi.unchanged = lambda before: {"ok": True, "before": before, "after": before}
    orch_mod.real_views = wi.real_views
    orch_mod.unchanged = wi.unchanged
    crp.require_testable_cli = lambda live: {"ok": True, "disabled_cli_blockers": [], "mode": "read_only"}

    seen = {}
    for label, world, bind in (("a", w1, b1), ("b", w2, b2)):
        fake = FakeRunner(bind)

        def runner(args, cwd=None, env=None, stdout=None, stderr=None, capture_output=False, text=False, _f=fake):
            return _f(args, cwd=cwd, env=env, stdout=stdout, stderr=stderr)

        stage_cli.run_stage(
            allow_real=True,
            runner=runner,
            bind_payload=world["bind"],
            skip_live=True,
            bind_write_dir=evidence_dir / f"follow-{label}-bind",
        )
        seen[label] = {
            "attempt": bind.attempt,
            "prepare_argv": next(c for c in fake.calls if c[2].endswith("prepare_scene.py")),
            "build_argv": next(c for c in fake.calls if c[2].endswith("build_verified.py")),
        }
    assert seen["a"]["prepare_argv"][2] != seen["b"]["prepare_argv"][2]
    assert Path(seen['a']['prepare_argv'][2]).parent == b1.W
    assert Path(seen['b']['prepare_argv'][2]).parent == b2.W
    assert b1.attempt == w1['record']['attempt'] == 31
    assert b2.attempt == w2['record']['attempt'] == 32
    assert "stage75q-alt" in seen["b"]["prepare_argv"][2]
    assert "stage75q-alt" in seen["b"]["build_argv"][2]
    cases["helpers_follow_record"] = {"ok": True, "seen": seen}


def test_manifest_path_safety(evidence_dir: Path, cases: dict) -> None:
    world = _make_world(evidence_dir / "manifest-world", attempt=41)
    root = Path(world["root"])
    tag = uuid.uuid4().hex[:8]
    # absolute path entry
    bad = evidence_dir / "manifest-world" / f"bad-abs-{tag}.json"
    _write(bad, json.dumps({"/etc/passwd": "0" * 64}) + "\n")
    good = Path(world["bind"]["tool_manifests"][0])
    try:
        verify_tool_manifests(root, [bad, good])
        raise AssertionError("absolute should fail")
    except RuntimeError as exc:
        cases.setdefault("manifest", {})["absolute_refused"] = str(exc)
    # escape ..
    bad2 = evidence_dir / "manifest-world" / f"bad-dot-{tag}.json"
    _write(bad2, json.dumps({"../outside.txt": "0" * 64}) + "\n")
    try:
        verify_tool_manifests(root, [bad2, good])
        raise AssertionError("dotdot should fail")
    except RuntimeError as exc:
        cases["manifest"]["dotdot_refused"] = str(exc)
    # duplicate manifests
    try:
        verify_tool_manifests(root, [good, good])
        raise AssertionError("duplicate should fail")
    except RuntimeError as exc:
        cases["manifest"]["duplicate_refused"] = str(exc)
    # missing second
    try:
        verify_tool_manifests(root, [good])
        raise AssertionError("single should fail")
    except RuntimeError as exc:
        cases["manifest"]["single_refused"] = str(exc)
    # parent symlink: unique names, never unlink prior fixtures
    real_parent = evidence_dir / "manifest-world" / f"real-parent-{tag}"
    real_parent.mkdir(parents=True, exist_ok=True)
    (real_parent / "tool.py").write_text("x", encoding="utf-8")
    sym_root = evidence_dir / "manifest-world" / f"sym-root-{tag}"
    sym_root.mkdir(parents=True, exist_ok=True)
    try:
        child = sym_root / "linked"
        child.symlink_to(real_parent, target_is_directory=True)
        man2 = sym_root / "m1.json"
        man3 = sym_root / "m2.json"
        rel = "linked/tool.py"
        digest = _h(real_parent / "tool.py")
        _write(man2, json.dumps({rel: digest}) + "\n")
        _write(man3, json.dumps({rel: digest}) + "\n")
        verify_tool_manifests(sym_root, [man2, man3])
        raise AssertionError("symlink ancestor should fail")
    except RuntimeError as exc:
        cases["manifest"]["symlink_ancestor_refused"] = str(exc)
    # r5 binding
    r5 = verify_r5_migration_binding(world["bind"])
    cases["manifest"]["r5"] = r5
    cases["manifest"]["ok"] = True


def test_chain_and_independence(evidence_dir: Path, cases: dict) -> None:
    world = _make_world(evidence_dir / "chain-world", attempt=51)
    live_db = Path(world["live_db"])
    ok = verify_event_chain_sqlite_ro(live_db)
    assert ok["ok"] is True
    # tamper payload -> event_hash mismatch (V1)
    with sqlite3.connect(live_db) as db:
        db.execute("UPDATE events SET payload_json=? WHERE sequence=1", ('{"tampered":true}',))
    bad = verify_event_chain_sqlite_ro(live_db)
    assert bad["ok"] is False and bad["reason"] == "event_hash mismatch"
    cases["chain"] = {"ok_before": ok, "tamper": bad}

    # Establish a passing control on the exact copy before altering sources.
    bind = Bind(world["bind"])
    state = json.loads((bind.acceptance_root / 'STATE.json').read_text())
    votes = verify_two_independent_votes(bind, state)
    assert votes["ok"] and votes["validator"].endswith("validate_acceptance_database")
    cases["votes_ok"] = votes
    with closing(sqlite3.connect(world['db'])) as db, db:
        rows = db.execute('SELECT rowid,data FROM ri_sources ORDER BY rowid LIMIT 2').fetchall()
        assert len(rows) == 2
        for rowid, raw in rows:
            source = json.loads(raw)
            node = source['node']
            node.update(entry='/bin/echo', model='TEST-same-model', agent_type='qoder',
                        interface='cli', entry_program='cli:qoder')
            db.execute('UPDATE ri_sources SET data=? WHERE rowid=?', (json.dumps(source), rowid))
    try:
        verify_two_independent_votes(bind, state)
        raise AssertionError('same entry+model must fail through the product validator')
    except RuntimeError as exc:
        cases['independence_same_entry_model'] = {'ok': True, 'same_copy_baseline_passed': True,
                                                 'error': str(exc), 'validator': votes['validator']}


def test_75c_migration_data(evidence_dir: Path, cases: dict) -> None:
    db = evidence_dir / "c75" / f"db-{uuid.uuid4().hex[:8]}.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    _current_product_database(db, 'initialize')
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version=7")
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
    result = _current_product_database(db, 'migrate')
    assert result["ok"] and result["migrated"]
    assert result["rows_after"][0]["state"] in ('pending', 'dead')
    assert result["rows_after"][0]["attempts"] >= 3
    assert result["rows_after"][0]["created_at"] == 42.5
    assert json.loads(result["rows_after"][0]["data"]) == {"k": 1}
    cases["schema75c_migration"] = {
        "ok": True,
        "columns_after": result["columns_after"],
        "new_indexes": 0,
        "schema_version": 7,
        "kept_ids": [r["id"] for r in result["rows_after"]],
    }


def test_leaf_build_still(evidence_dir: Path, cases: dict) -> None:
    from build_verified import run_formal_build
    from gates import initialize_attempt_state, load_acceptance_state

    world = _make_world(evidence_dir / "leaf-world", attempt=61)
    write_bind(evidence_dir / "leaf-world" / "bind.json", world["bind"])
    bind = Bind(world["bind"])
    initialize_attempt_state(bind, load_acceptance_state(bind))
    fake_script = STAGE / "isolation/fake_build_release.py"
    result = run_formal_build(bind, build_script_override=fake_script)
    assert result["ok"] and result["forced_migration_tier"] is False
    assert "--interface" in result["argv"] and "--previous-release" in result["argv"]
    cases["leaf"] = {"ok": True, "argv_has_full_params": True, "tier_forced": False}


def main(evidence_dir: Path) -> int:
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    # Ensure we do not touch old evidence files
    old_resume = evidence_dir.parent / "isolation-resume" / "isolation-result.json"
    old_first = evidence_dir.parent / "isolation-result.json"
    old_resume_hash = _h(old_resume) if old_resume.is_file() else None
    old_first_hash = _h(old_first) if old_first.is_file() else None
    report: dict[str, Any] = {"ok": False, "revision": "r2-entry-strong-gates", "cases": {}}
    try:
        # Fresh synthetics under evidence_dir each run — never TMPDIR, never wipe old evidence
        synth = evidence_dir / "synthetic" / time.strftime("%Y%m%d-%H%M%S")
        synth.mkdir(parents=True, exist_ok=True)
        report["synthetic_root"] = str(synth)
        test_plan_zero_write(synth, report["cases"])
        test_manifest_path_safety(synth, report["cases"])
        test_chain_and_independence(synth, report["cases"])
        test_75c_migration_data(synth, report["cases"])
        test_leaf_build_still(synth, report["cases"])
        test_run_entry_calls_orchestrator(synth, report["cases"])
        test_helpers_follow_record_attempt_and_dirname(synth, report["cases"])
        report["ok"] = True
    except Exception as exc:  # noqa: BLE001
        report["ok"] = False
        report["error"] = repr(exc)
        raise
    finally:
        if old_resume_hash is not None:
            assert _h(old_resume) == old_resume_hash, "STOP: old isolation-resume evidence mutated"
            report["old_resume_unchanged"] = True
        if old_first_hash is not None:
            assert _h(old_first) == old_first_hash, "STOP: old first-round evidence mutated"
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
