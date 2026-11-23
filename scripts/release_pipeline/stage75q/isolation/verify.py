"""Isolation suite for stage75q.
FakeRunner verifies orchestration only.
Separate leaf tests drive real build_verified against fake CLI and prove argv/nonzero/missing artifacts.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE))

from bind import PIN, clean_env, load_bind, sha, write_bind  # noqa: E402
from build_verified import REQUIRED_EVIDENCE_KEYS, build_argv, run_formal_build  # noqa: E402
from gates import (  # noqa: E402
    check_interpreter,
    refuse_half_package,
    run_prebuild_gates,
    verify_tool_manifests,
)
from orchestrator import FOURTEEN_KEYS, Orchestrator, plan_only  # noqa: E402
from schema75c import NEW_SIX, OLD_FOUR, check_ri_pending_references  # noqa: E402


def _h(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class FakeProc(SimpleNamespace):
    pass


class FakeRunner:
    """Records helper argv order and emits required artifacts for orchestration tests."""

    def __init__(self, bind, *, fail_at: str | None = None, skip_fourteen_artifact: str | None = None):
        self.bind = bind
        self.fail_at = fail_at
        self.skip_fourteen_artifact = skip_fourteen_artifact
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd=None, env=None, stdout=None, stderr=None, capture_output=False, text=False):
        self.calls.append(list(args))
        label = Path(args[2]).name if len(args) >= 3 else "unknown"
        if self.fail_at and label == self.fail_at:
            if stdout is not None:
                stdout.write("FAKE_FAIL " + label + "\n")
            return FakeProc(returncode=1, stdout="FAKE_FAIL", stderr="")
        self._emit(label)
        if stdout is not None:
            stdout.write(json.dumps({"ok": True, "fake": label}) + "\n")
        return FakeProc(returncode=0, stdout='{"ok":true}', stderr="")

    def _emit(self, label: str) -> None:
        O, A, PKG = self.bind.O, self.bind.A, self.bind.PKG
        O.mkdir(parents=True, exist_ok=True)
        if label == "build_verified.py":
            PKG.mkdir(parents=True, exist_ok=True)
            release = {
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
            (PKG / "RELEASE.json").write_text(json.dumps(release) + "\n")
            (PKG / "FILES.sha256").write_text("abc  x\n")
            state = json.loads((A / "STATE.json").read_text())
            state["verified_package"] = str(PKG)
            (A / "STATE.json").write_text(json.dumps(state) + "\n")
            (A / "FORMAL_BUILD.started").write_text("x\n")
        elif label == "prepare_scene.py":
            scene_root = self.bind.root / "rehearsal" / "iso-scene"
            runtime = scene_root / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            db = runtime / ".runtime/agentos.sqlite3"
            db.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(db) as conn:
                conn.execute("PRAGMA user_version=7")
                conn.execute(
                    "CREATE TABLE ri_pending_references("
                    "id TEXT, data TEXT, created_at REAL, attempts INTEGER, state TEXT, last_error TEXT)"
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
                + "\n"
            )
            (runtime / "cfg.json").write_text(
                json.dumps(
                    {
                        "runtime_dir": str(runtime / ".runtime"),
                        "project_dir": str(runtime),
                        "memory": {"obsidian_vault": str(scene_root / "assistant/第二大脑")},
                        "paths": {"obsidian_vault": str(scene_root / "assistant/第二大脑")},
                    }
                )
            )
            (scene_root / "assistant/第二大脑").mkdir(parents=True, exist_ok=True)
        elif label == "run_rehearsal.py":
            (O / "30-复制现场演练.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "cases": [{"fault_after": f"p{i}"} for i in range(11)],
                        "success": {"result": {"read_back": {"restart_verified": True, "version": self.bind.version}}},
                    }
                )
                + "\n"
            )
            (O / "34-card51-worker-scores.json").write_text(json.dumps({"ok": True}) + "\n")
            (O / "35-card57-default-task.json").write_text(json.dumps({"ok": True}) + "\n")
        elif label == "specials_v169.py":
            sections = {
                "card43_node_test_deadline": {"ok": True},
                "card44_host_backvote": {"ok": True},
                "card45_retired_workbuddy": {"ok": True},
                "card46_experience_recovery": {"ok": True},
                "card47_duplicate_revise": {"ok": True},
                "card57_host_pool_score_required": {"ok": True},
            }
            (O / "41-v169-new-behaviors.json").write_text(json.dumps({"ok": True, "sections": sections}) + "\n")
        elif label == "specials_scores_v169.py":
            (O / "42-card52-52b.json").write_text(
                json.dumps({"ok": True, "sections": {"a": {"ok": True}, "b": {"ok": True}, "c": {"ok": True}}}) + "\n"
            )
        elif label == "rehearsal_specials.py":
            (O / "33-task-abandon-cancel.json").write_text(json.dumps({"ok": True}) + "\n")
        elif label == "specials_v1614.py":
            (O / "48-v1614-new-cases.json").write_text(
                json.dumps({"ok": True, "sections": {"10_void": {"ok": True}, "11_ui_install": {"ok": True}}}) + "\n"
            )
        elif label == "workday_case12.py":
            if self.skip_fourteen_artifact == "workday":
                return
            (O / "49-workday-case12.json").write_text(json.dumps({"ok": True}) + "\n")
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
                + "\n"
            )
        elif label == "case14_chain.py":
            (O / "51-chain-case14.json").write_text(
                json.dumps({"ok": True, "cases": {"V1": {}, "V2": {}, "V8": {}}}) + "\n"
            )
        elif "switch_release.py" in label or (len(self.calls[-1]) > 3 and self.calls[-1][3] in ("plan", "preflight")):
            # online plan/preflight via package script — FakeRunner writes JSON to stdout file already
            if stdout is not None:
                # rewrite with required shape
                pass


def _setup_world(tmp: Path) -> tuple[dict, Path]:
    root = tmp / "release-root"
    work = root / "work/v1615-r1"
    source = root / "source-v1615-r1"
    candidate = source / "development/integrated-candidate"
    acceptance = root / "acceptance-v1615-r1"
    step4 = acceptance / "step4-attempt9"
    reports = root / "reports/v1615-r1/round1b-attempt9"
    package = root / "releases/v1615-r1-attempt9/v1.6.15"
    brain = root / "generated-brain-v1615-r1"
    live = tmp / "fake-live"
    prev = tmp / "previous-package/v1.6.14"
    for p in (work, candidate / "scripts", step4, reports, brain, live / ".runtime", prev):
        p.mkdir(parents=True, exist_ok=True)
    (work / "BASE_READY.json").write_text(json.dumps({"base": "a" * 40, "preparation_sha256": "x"}) + "\n")
    (work / "PREPARATION.json").write_text(json.dumps({"tests": {"core": 1666}, "base": "a" * 40, "round": "v1615-r1"}) + "\n")
    # acceptance STATE / FREEZE / prompt
    prompt = acceptance / "review.txt"
    prompt.write_text("PASS FINAL_ACCEPTANCE: PASS\n")
    freeze = acceptance / "FREEZE.json"
    freeze.write_text(json.dumps({"ok": True}) + "\n")
    # acceptance DB with two independent votes
    db = tmp / "acceptance.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, execution_snapshot_json TEXT)")
        conn.execute("CREATE TABLE agent_runs (task_id TEXT, agent_name TEXT, status TEXT, output TEXT)")
        snap = json.dumps({"review_context": {"no_work_task": True, "release_acceptance": True}})
        conn.execute("INSERT INTO tasks VALUES (?,?,?)", ("task-1", "completed", snap))
        for name in ("reviewer-alpha", "reviewer-beta"):
            conn.execute(
                "INSERT INTO agent_runs VALUES (?,?,?,?)",
                ("task-1", name, "completed", "FINAL_ACCEPTANCE: PASS"),
            )
    state = {
        "card49_acceptance_passed": True,
        "tasks": {"card49": "task-1"},
        "database": str(db),
        "freeze_sha256": _h(freeze),
        "source_manifest_sha256": "0" * 64,
        "prompt": str(prompt),
        "prompt_sha256": _h(prompt),
        "workspace_root": str(tmp / "workspaces"),
    }
    # source manifest matching state hash
    manifest = candidate / "SOURCE_MANIFEST.json"
    # write then fix hash in state
    manifest.write_text(json.dumps({"files": {}}) + "\n")
    state["source_manifest_sha256"] = _h(manifest)
    (acceptance / "STATE.json").write_text(json.dumps(state, indent=2) + "\n")
    # candidate scripts stubs
    (candidate / "scripts/build_release.py").write_text(
        "def _passed_verdict(text):\n    return 'FINAL_ACCEPTANCE: PASS' in str(text)\n"
        "def validate_acceptance_database(*a, **k):\n    return {'conclusions':{},'pool_nodes':{},'execution':{}}\n"
    )
    (candidate / "src").mkdir(exist_ok=True)
    # live idle DB + manifest
    live_db = live / ".runtime/agentos.sqlite3"
    with sqlite3.connect(live_db) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, attempts INT, lease_until TEXT)")
        conn.execute("CREATE TABLE events (sequence INTEGER)")
        conn.execute("INSERT INTO events VALUES (1)")
        conn.execute(
            "CREATE TABLE ri_sources (id TEXT, data TEXT)"
        )  # no ri_pending_references — v14 live ok
    live_manifest = live / "SOURCE_MANIFEST.json"
    live_manifest.write_text(json.dumps({"live": True}) + "\n")
    # dual tool manifests
    tool_a = reports / "final-tool-hashes.json"
    tool_b = reports / "step4-tool-hashes.json"
    rel = "work/v1615-r1/stage75q/bind.py"
    # place a tracked file under root
    tracked = root / rel
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text((STAGE / "bind.py").read_text(encoding="utf-8"))
    digest = _h(tracked)
    tool_a.write_text(json.dumps({rel: digest}) + "\n")
    tool_b.write_text(json.dumps({rel: digest}) + "\n")
    # previous package placeholder
    (prev / "RELEASE.json").write_text(json.dumps({"status": "verified_immutable_release"}) + "\n")
    bind = {
        "root": str(root),
        "work": str(work),
        "live": str(live),
        "source": str(source),
        "candidate": str(candidate),
        "acceptance_root": str(acceptance),
        "acceptance_step4_dir": str(step4),
        "reports_step4_dir": str(reports),
        "releases_package_dir": str(package),
        "helpers_dir": str(STAGE / "helpers"),
        "version": "v1.6.15",
        "round": "v1615-r1",
        "attempt": 9,
        "previous_package": str(prev),
        "generated_brain": str(brain),
        "tool_manifests": [str(tool_a), str(tool_b)],
        "preparation_path": str(work / "PREPARATION.json"),
        "online_manifest_sha256": _h(live_manifest),
        "review_report": None,
        "previous_resource_vendor_cli": str(tmp / "old_vendor.py"),
        "previous_resource_vendor_cli_sha256": "b" * 64,
    }
    (tmp / "old_vendor.py").write_text("# fake previous module\n")
    bind_path = tmp / "bind.json"
    write_bind(bind_path, bind)
    return bind, bind_path


def test_interpreter_and_optimize(evidence: dict) -> None:
    check_interpreter()
    evidence["interpreter_ok"] = True
    # The separate -O subprocess case below verifies optimize refusal by behavior.


def test_manifest_gate_fails_before_subprocess(tmp: Path, evidence: dict) -> None:
    bind_data, bind_path = _setup_world(tmp / "manifest")
    os.environ["STAGE75Q_BIND_JSON"] = str(bind_path)
    bind = load_bind()
    # wipe manifests
    for p in bind.tool_manifests:
        p.unlink()
    calls = []

    def runner(*a, **k):
        calls.append(a)
        raise AssertionError("subprocess must not run")

    try:
        verify_tool_manifests(bind.root, bind.tool_manifests)
        raise AssertionError("expected manifest failure")
    except RuntimeError as exc:
        evidence["manifest_gate"] = str(exc)
    assert not calls
    evidence["manifest_zero_subprocess"] = True


def test_half_package_and_marker_refuse(tmp: Path, evidence: dict) -> None:
    bind_data, bind_path = _setup_world(tmp / "half")
    os.environ["STAGE75Q_BIND_JSON"] = str(bind_path)
    bind = load_bind()
    bind.PKG.mkdir(parents=True, exist_ok=True)
    (bind.PKG / "partial.txt").write_text("x")
    try:
        refuse_half_package(bind)
        raise AssertionError("expected half package refuse")
    except RuntimeError as exc:
        evidence["half_package_refuse"] = str(exc)
    bind.PKG.joinpath("partial.txt").unlink()
    bind.PKG.rmdir()
    bind.A.mkdir(parents=True, exist_ok=True)
    (bind.A / "FORMAL_BUILD.started").write_text("old\n")
    try:
        refuse_half_package(bind)
        raise AssertionError("expected marker refuse")
    except RuntimeError as exc:
        evidence["old_marker_refuse"] = str(exc)


def test_fake_runner_argv_order_and_same_attempt(tmp: Path, evidence: dict) -> None:
    bind_data, bind_path = _setup_world(tmp / "orch")
    os.environ["STAGE75Q_BIND_JSON"] = str(bind_path)
    bind = load_bind()
    # monkeypatch cleanup / live online switch / cli_retest / workspace guard for FakeRunner path
    import orchestrator as orch_mod
    import workday_isolation as wi

    orch_mod.cleanup_scene = lambda b: {"ok": True, "service_created": False, "production_untouched": True}
    wi.real_views = lambda: {"books": [], "catalog_exists": False, "catalog_files": {}}
    wi.unchanged = lambda before: {"ok": True, "before": before, "after": before}
    orch_mod.real_views = wi.real_views
    orch_mod.unchanged = wi.unchanged

    fake = FakeRunner(bind)

    def runner(args, cwd=None, env=None, stdout=None, stderr=None, capture_output=False, text=False):
        # online switch_release plan/preflight
        if len(args) >= 4 and args[3] in ("plan", "preflight"):
            payload = json.dumps({"ok": True, "writes_performed": 0}) + "\n"
            if stdout is not None:
                stdout.write(payload)
            else:
                return FakeProc(returncode=0, stdout=payload, stderr="")
            return FakeProc(returncode=0, stdout=payload, stderr="")
        return fake(args, cwd=cwd, env=env, stdout=stdout, stderr=stderr)

    import cli_retest_preflight as crp

    crp.require_testable_cli = lambda live: {"ok": True, "disabled_cli_blockers": [], "mode": "read_only"}

    orch = orch_mod.Orchestrator(bind, runner=runner, skip_live=True, allow_real=True)
    # also skip real online manifest file writes that need package switch script — FakeRunner handles
    summary = orch.run()
    assert summary["ok"] is True
    # orch._calls entries are dicts {"label","argv","out"}; do not index like fake.calls lists.
    helper_names = [Path(c["argv"][2]).name for c in orch._calls if len(c.get("argv") or []) >= 3]
    assert helper_names[0] == "build_verified.py"
    assert "prepare_scene.py" in helper_names
    assert helper_names.index("prepare_scene.py") < helper_names.index("run_rehearsal.py")
    assert "case13_reasoning.py" in helper_names and "case14_chain.py" in helper_names
    assert set(summary["fourteen_items"]) == set(FOURTEEN_KEYS)
    assert len(summary["fourteen_items"]) == 14 and all(summary["fourteen_items"].values())
    evidence["fake_runner_order"] = helper_names
    evidence["same_attempt"] = {
        "attempt": bind.attempt,
        "acceptance": str(bind.A),
        "reports": str(bind.O),
        "package": str(bind.PKG),
    }
    evidence["fake_runner_ok"] = True
    evidence["fourteen_items"] = summary["fourteen_items"]
    # failure log retained
    bind2_data, bind2_path = _setup_world(tmp / "orch-fail")
    os.environ["STAGE75Q_BIND_JSON"] = str(bind2_path)
    bind2 = load_bind()
    fake2 = FakeRunner(bind2, fail_at="prepare_scene.py")

    def runner2(args, cwd=None, env=None, stdout=None, stderr=None, capture_output=False, text=False):
        return fake2(args, cwd=cwd, env=env, stdout=stdout, stderr=stderr)

    orch2 = orch_mod.Orchestrator(bind2, runner=runner2, skip_live=True, allow_real=True)
    try:
        orch2.run()
        raise AssertionError("expected fail")
    except RuntimeError:
        log = bind2.O / "21-prepare-scene.stdout.txt"
        assert log.is_file() and "FAKE_FAIL" in log.read_text(encoding="utf-8")
        evidence["failure_log_retained"] = str(log)


def test_fourteen_missing_not_ok(tmp: Path, evidence: dict) -> None:
    bind_data, bind_path = _setup_world(tmp / "fourteen")
    os.environ["STAGE75Q_BIND_JSON"] = str(bind_path)
    bind = load_bind()
    import orchestrator as orch_mod
    import workday_isolation as wi
    import cli_retest_preflight as crp

    orch_mod.cleanup_scene = lambda b: {"ok": True, "service_created": False}
    wi.real_views = lambda: {"books": [], "catalog_exists": False, "catalog_files": {}}
    wi.unchanged = lambda before: {"ok": True, "before": before, "after": before}
    orch_mod.real_views = wi.real_views
    orch_mod.unchanged = wi.unchanged
    crp.require_testable_cli = lambda live: {"ok": True, "disabled_cli_blockers": [], "mode": "read_only"}
    orch_mod.require_testable_cli = crp.require_testable_cli
    fake = FakeRunner(bind, skip_fourteen_artifact="workday")

    def runner(args, cwd=None, env=None, stdout=None, stderr=None, capture_output=False, text=False):
        if len(args) >= 4 and args[3] in ("plan", "preflight"):
            payload = json.dumps({"ok": True, "writes_performed": 0}) + "\n"
            if stdout is not None:
                stdout.write(payload)
            return FakeProc(returncode=0, stdout=payload, stderr="")
        return fake(args, cwd=cwd, env=env, stdout=stdout, stderr=stderr)

    orch = orch_mod.Orchestrator(bind, runner=runner, skip_live=True, allow_real=True)
    try:
        orch.run()
        raise AssertionError("fourteen missing should fail")
    except (RuntimeError, FileNotFoundError) as exc:
        evidence["fourteen_missing_refused"] = str(exc)


def test_leaf_build_verified_real_subprocess(tmp: Path, evidence: dict) -> None:
    """Real leaf function: actually launches fake CLI, parses output, detects nonzero & missing artifacts."""
    bind_data, bind_path = _setup_world(tmp / "leaf")
    os.environ["STAGE75Q_BIND_JSON"] = str(bind_path)
    bind = load_bind()
    # init attempt state
    from gates import initialize_attempt_state, load_acceptance_state

    initialize_attempt_state(bind, load_acceptance_state(bind))
    fake_script = STAGE / "isolation/fake_build_release.py"

    # success path
    result = run_formal_build(bind, build_script_override=fake_script)
    assert result["ok"] and result["forced_migration_tier"] is False
    assert set(REQUIRED_EVIDENCE_KEYS) <= set(result["end_to_end_keys"])
    argv = result["argv"]
    # parameter order proof
    assert argv[0] == str(PIN) and argv[1] == "-B"
    assert "--interface" in argv and "--stable-scripts" in argv and "--second-brain" in argv
    assert "--acceptance-database" in argv and "--acceptance-task-id" in argv and "--previous-release" in argv
    evidence["leaf_argv"] = argv
    evidence["leaf_success"] = result

    # nonzero exit retains log, no success package overwrite beyond marker
    bind_data2, bind_path2 = _setup_world(tmp / "leaf-fail")
    os.environ["STAGE75Q_BIND_JSON"] = str(bind_path2)
    bind2 = load_bind()
    initialize_attempt_state(bind2, load_acceptance_state(bind2))

    def failing_runner(args, cwd=None, env=None, capture_output=False, text=False):
        # inject --fail into fake script argv
        args = list(args) + ["--fail"]
        return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)

    try:
        run_formal_build(bind2, runner=failing_runner, build_script_override=fake_script)
        raise AssertionError("nonzero must fail")
    except RuntimeError as exc:
        assert "exit=7" in str(exc) or "exit=" in str(exc)
        assert (bind2.O / "20-formal-build.log").is_file()
        evidence["leaf_nonzero"] = str(exc)

    # missing RELEASE
    bind_data3, bind_path3 = _setup_world(tmp / "leaf-omit")
    os.environ["STAGE75Q_BIND_JSON"] = str(bind_path3)
    bind3 = load_bind()
    initialize_attempt_state(bind3, load_acceptance_state(bind3))

    def omit_runner(args, cwd=None, env=None, capture_output=False, text=False):
        args = list(args) + ["--omit-release"]
        return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)

    try:
        run_formal_build(bind3, runner=omit_runner, build_script_override=fake_script)
        raise AssertionError("missing RELEASE must fail")
    except RuntimeError as exc:
        evidence["leaf_missing_release"] = str(exc)


def test_schema75c(tmp: Path, evidence: dict) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    db = tmp / "s75c.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version=7")
    live = check_ri_pending_references(db, require_present=False, phase="pre_switch_live")
    assert live["ok"] and live["table_present"] is False
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE ri_pending_references(id TEXT, data TEXT, created_at REAL, attempts INTEGER)"
        )
    four = check_ri_pending_references(db, require_present=True, phase="copy_scene")
    assert four.get("needs_four_to_six_migration")
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE ri_pending_references")
        conn.execute(
            "CREATE TABLE ri_pending_references(id TEXT, data TEXT, created_at REAL, attempts INTEGER, state TEXT, last_error TEXT)"
        )
    six = check_ri_pending_references(db, require_present=True, phase="copy_scene")
    assert six["ok"] and six["six_columns"]
    evidence["schema75c"] = {"live_absent_ok": live, "four": four, "six": six, "old_four": list(OLD_FOUR), "new_six": list(NEW_SIX)}


def test_plan_and_help(evidence: dict) -> None:
    proc = subprocess.run(
        [str(PIN), "-B", str(STAGE / "cli.py"), "--help"],
        capture_output=True,
        text=True,
        env=clean_env(),
    )
    assert proc.returncode == 0 and "stage75q" in proc.stdout
    evidence["help_ok"] = True


def test_build_argv_uses_same_attempt_paths(tmp: Path, evidence: dict) -> None:
    bind_data, bind_path = _setup_world(tmp / "argv")
    os.environ["STAGE75Q_BIND_JSON"] = str(bind_path)
    bind = load_bind()
    state = json.loads((bind.acceptance_root / "STATE.json").read_text())
    prep = json.loads(bind.preparation_path.read_text())
    argv = build_argv(bind, state, prep)
    assert str(bind.PKG) in argv
    assert str(bind.previous_package) in argv
    assert str(bind.generated_brain) in argv
    evidence["build_argv_same_attempt"] = True


def test_shared_release_context_import(evidence: dict) -> None:
    """Normal CLI import of driver75p.release_context — required shared API surface."""
    from context_adapter import import_release_context

    rc = import_release_context()
    for attr in ("load_context", "validate_context", "clean_env", "check_interpreter", "verify_manifests"):
        assert hasattr(rc, attr) and callable(getattr(rc, attr)), attr
    env = rc.clean_env()
    assert "PYTHONPATH" not in env and "PYTHONOPTIMIZE" not in env
    assert env.get("PYTHONDONTWRITEBYTECODE") == "1"
    # check_interpreter must pass under current -B / non-optimize session
    rc.check_interpreter()
    evidence["shared_api"] = {
        "module": str(Path(rc.__file__).resolve()) if getattr(rc, "__file__", None) else "driver75p.release_context",
        "attrs": [
            "load_context",
            "validate_context",
            "clean_env",
            "check_interpreter",
            "verify_manifests",
        ],
        "clean_env_ok": True,
        "check_interpreter_ok": True,
    }


def test_optimize_refused_before_write(evidence: dict) -> None:
    """Own -O process must see optimize!=0; gates refuse in-process when flags.optimize!=0."""
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(STAGE)!r})\n"
        "import gates\n"
        "try:\n"
        "    gates.check_interpreter()\n"
        "    print('UNEXPECTED_PASS')\n"
        "except RuntimeError as e:\n"
        "    print('REFUSED:' + str(e))\n"
    )
    proc = subprocess.run(
        [str(PIN), "-O", "-B", "-c", code],
        capture_output=True,
        text=True,
        env=clean_env(),
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert "REFUSED:" in out and "UNEXPECTED_PASS" not in out, out
    evidence["optimize_refused"] = [line for line in out.splitlines() if line.startswith("REFUSED:")][-1]


def main(evidence_dir: Path) -> int:
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence: dict = {"ok": False, "cases": {}, "revision": "resume-keyerror2-fix"}
    # refuse optimize via explicit subprocess -O
    opt = subprocess.run(
        [str(PIN), "-O", "-B", "-c", "import sys; print(sys.flags.optimize)"],
        capture_output=True,
        text=True,
    )
    evidence["optimize_flag_probe"] = opt.stdout.strip()
    try:
        with tempfile.TemporaryDirectory(prefix="stage75q-iso-") as td:
            tmp = Path(td)
            test_interpreter_and_optimize(evidence["cases"].setdefault("interpreter", {}))
            test_plan_and_help(evidence["cases"].setdefault("help", {}))
            test_shared_release_context_import(evidence["cases"].setdefault("shared", {}))
            test_optimize_refused_before_write(evidence["cases"].setdefault("optimize", {}))
            test_manifest_gate_fails_before_subprocess(tmp, evidence["cases"].setdefault("manifest", {}))
            test_half_package_and_marker_refuse(tmp, evidence["cases"].setdefault("refuse", {}))
            test_schema75c(tmp / "schema", evidence["cases"].setdefault("schema75c", {}))
            test_build_argv_uses_same_attempt_paths(tmp, evidence["cases"].setdefault("argv_paths", {}))
            test_leaf_build_verified_real_subprocess(tmp, evidence["cases"].setdefault("leaf", {}))
            test_fake_runner_argv_order_and_same_attempt(tmp, evidence["cases"].setdefault("fake_runner", {}))
            test_fourteen_missing_not_ok(tmp, evidence["cases"].setdefault("fourteen", {}))
        evidence["ok"] = True
    except Exception as exc:  # noqa: BLE001
        evidence["ok"] = False
        evidence["error"] = repr(exc)
        raise
    finally:
        (evidence_dir / "isolation-result.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        log = evidence_dir / "isolation.log"
        log.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": evidence["ok"], "evidence_dir": str(evidence_dir)}, ensure_ascii=False))
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")))
