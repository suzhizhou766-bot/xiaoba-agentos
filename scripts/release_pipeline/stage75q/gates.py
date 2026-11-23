"""Pre-formal-build gates: strong path safety, full event-chain, real acceptance validator, independence."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from acceptance_verdict import passed_verdict
from bind import PIN, Bind, sha
from context_adapter import verify_r5_migration_binding
from leaf_contracts import require_acceptance_passed


def check_interpreter() -> None:
    if Path(sys.executable).resolve() != PIN.resolve():
        raise RuntimeError("STOP: unified 3.14.7 interpreter required")
    if not sys.dont_write_bytecode:
        raise RuntimeError("STOP: must run with -B / PYTHONDONTWRITEBYTECODE")
    if sys.flags.optimize != 0:
        raise RuntimeError("STOP: optimize mode refused before any write/subprocess")


def _no_symlink_ancestors(path: Path) -> None:
    cur = path
    # Check path and all ancestors for symlinks BEFORE trusting resolve().
    for _ in range(64):
        if cur.is_symlink():
            raise RuntimeError("STOP: symlink in path ancestry: " + str(cur))
        if cur == cur.parent:
            break
        cur = cur.parent


def _safe_rel_under_root(root: Path, rel: str) -> Path:
    if not isinstance(rel, str) or not rel or rel.startswith("/") or rel.startswith("\\"):
        raise RuntimeError("STOP: tool path must be relative, non-absolute: " + repr(rel))
    if ".." in Path(rel).parts:
        raise RuntimeError("STOP: tool path escapes via ..: " + rel)
    root = root.resolve()
    candidate = root / rel
    _no_symlink_ancestors(candidate if candidate.exists() else candidate.parent)
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise RuntimeError("STOP: tool path not relative_to root: " + rel)
    if resolved.is_symlink() or not resolved.is_file():
        raise RuntimeError("STOP: tool path not a regular file: " + rel)
    return resolved


def verify_tool_manifests(root: Path, manifests: list[Path]) -> dict[str, Any]:
    if len(manifests) != 2:
        raise RuntimeError("STOP: exactly two distinct tool manifests required")
    resolved_manifests = []
    for manifest in manifests:
        m = Path(manifest)
        if m.is_symlink() or not m.is_file() or m.stat().st_size == 0:
            raise RuntimeError("STOP: tool manifest missing/empty/symlink: " + str(m))
        _no_symlink_ancestors(m)
        resolved_manifests.append(m.resolve())
    if resolved_manifests[0] == resolved_manifests[1]:
        raise RuntimeError("STOP: dual manifests must be distinct paths")
    checked = {}
    root = Path(root)
    _no_symlink_ancestors(root)
    root_res = root.resolve()
    for manifest in resolved_manifests:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data:
            raise RuntimeError("STOP: tool manifest not a non-empty object: " + str(manifest))
        for rel, digest in data.items():
            path = _safe_rel_under_root(root_res, rel)
            got = sha(path)
            if got != digest:
                raise RuntimeError("STOP: tool hash drift: " + rel)
            checked[rel] = got
    return {"ok": True, "files": len(checked), "manifests": [str(m) for m in resolved_manifests]}


def refuse_half_package(bind: Bind) -> None:
    a, pkg, o = bind.A, bind.PKG, bind.O
    if (a / "FORMAL_BUILD.started").exists():
        raise RuntimeError("STOP: FORMAL_BUILD.started exists; refuse marker reuse")
    if (a / "STATE.json").exists() and (a / "STATE.json").stat().st_size > 0:
        state = json.loads((a / "STATE.json").read_text(encoding="utf-8"))
        if state.get("verified_package") or state.get("package_files_manifest_sha256"):
            raise RuntimeError("STOP: attempt STATE already has package evidence")
    if pkg.exists() and any(pkg.iterdir()):
        raise RuntimeError("STOP: package dir not empty; refuse half/old package reuse")
    if (o / "SCENE.json").exists():
        raise RuntimeError("STOP: SCENE.json already exists; refuse scene reuse")


def load_acceptance_state(bind: Bind) -> dict[str, Any]:
    path = bind.acceptance_root / "STATE.json"
    if not path.is_file():
        raise RuntimeError("STOP: acceptance STATE.json missing: " + str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def verify_freeze_prompt_source(bind: Bind, state: dict[str, Any]) -> dict[str, Any]:
    freeze = bind.acceptance_root / "FREEZE.json"
    if sha(freeze) != state.get("freeze_sha256"):
        raise RuntimeError("STOP: FREEZE hash mismatch")
    if sha(bind.C / "SOURCE_MANIFEST.json") != state.get("source_manifest_sha256"):
        raise RuntimeError("STOP: source manifest hash mismatch")
    prompt = Path(state["prompt"])
    if sha(prompt) != state.get("prompt_sha256"):
        raise RuntimeError("STOP: prompt hash mismatch")
    if state.get("diff"):
        if sha(Path(state["diff"])) != state.get("diff_sha256"):
            raise RuntimeError("STOP: diff hash mismatch")
    return {
        "ok": True,
        "freeze_sha256": state["freeze_sha256"],
        "source_manifest_sha256": state["source_manifest_sha256"],
        "prompt_sha256": state["prompt_sha256"],
    }


def _load_validate_acceptance(candidate: Path):
    path = Path(candidate) / "scripts" / "build_release.py"
    if not path.is_file():
        raise RuntimeError("STOP: build_release.py missing in candidate")
    name = "_stage75q_build_release_" + hashlib.sha256(str(path).encode()).hexdigest()[:12]
    if name not in sys.modules:
        sys.path[:0] = [str(Path(candidate) / "src"), str(Path(candidate) / "scripts")]
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        assert spec.loader is not None
        spec.loader.exec_module(mod)
    return sys.modules[name]


def verify_two_independent_votes(
    bind: Bind,
    state: dict[str, Any],
    *,
    review_report: Path | None = None,
) -> dict[str, Any]:
    """Independence comes from product validator + pool freeze/source — not fictional agent_runs columns."""
    require_acceptance_passed(state)
    tasks = state.get("tasks") or {}
    if not tasks:
        raise RuntimeError("STOP: STATE.tasks empty")
    task_id = next(iter(tasks.values()))
    database = Path(state["database"])
    if database.is_symlink() or not database.is_file():
        raise RuntimeError("STOP: acceptance database missing")

    with sqlite3.connect(f"file:{quote(str(database.resolve()))}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        task = dict(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone() or {})
        if not task:
            raise RuntimeError("STOP: acceptance task missing in DB")
        if task.get("status") != "completed":
            raise RuntimeError("STOP: acceptance task not completed")
        run_cols = {r[1] for r in db.execute("PRAGMA table_info(agent_runs)")}
        # Product schema has no entry/model on agent_runs and no run_identity table.
        if "entry" in run_cols or "model" in run_cols:
            raise RuntimeError(
                "STOP: refusing synthetic agent_runs.entry/model columns; "
                "use product schema + validate_acceptance_database"
            )
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "run_identity" in tables:
            raise RuntimeError(
                "STOP: refusing synthetic run_identity table; "
                "independence is pool source / freeze snapshot via product validator"
            )
        runs = [dict(r) for r in db.execute("SELECT * FROM agent_runs WHERE task_id=?", (task_id,))]

    if len(runs) < 2:
        raise RuntimeError("STOP: need >=2 reviewer runs in acceptance DB")
    names = {r.get("agent_name") for r in runs}
    if len(names) < 2:
        raise RuntimeError("STOP: need >=2 distinct agent_name")
    for run in runs:
        if run.get("status") != "completed":
            raise RuntimeError("STOP: reviewer run not completed: " + str(run.get("agent_name")))
        if not passed_verdict(run.get("output") or "", bind.C):
            raise RuntimeError("STOP: reviewer verdict failed: " + str(run.get("agent_name")))

    # Official product validator owns independence (pool node entry+model / freeze).
    mod = _load_validate_acceptance(bind.C)
    evidence = mod.validate_acceptance_database(database, task_id, bind.C)
    if not isinstance(evidence, dict) or not {"conclusions", "pool_nodes", "execution"} <= set(evidence):
        raise RuntimeError("STOP: validate_acceptance_database evidence incomplete")
    if "_source" in evidence:
        raise RuntimeError("STOP: evidence must not contain _source")

    return {
        "ok": True,
        "task_id": task_id,
        "agent_names": sorted(names),
        "run_columns": sorted(run_cols),
        "fictional_identity_columns": False,
        "validator": "build_release.validate_acceptance_database",
        "end_to_end_keys": sorted(k for k in evidence if k != "database_record_sha256"),
        "hardcoded_reviewer_names": False,
        "acceptance_field": (
            "acceptance_passed" if state.get("acceptance_passed") is True else "card49_acceptance_passed"
        ),
    }


def verify_live_idle(live: Path, *, drain_fabricator: Callable | None = None) -> dict[str, Any]:
    if drain_fabricator is not None:
        raise RuntimeError("STOP: drain fabricator injected; refuse forged drain")
    db_path = Path(live) / ".runtime/agentos.sqlite3"
    with sqlite3.connect(f"file:{quote(str(db_path.resolve()))}?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT id,status,attempts,lease_until FROM tasks "
            "WHERE status NOT IN ('completed','failed') OR lease_until IS NOT NULL"
        ).fetchall()
    if rows:
        raise RuntimeError("STOP: live queue not idle; do not drain: " + str(rows))
    return {"ok": True, "idle": True, "forged_drain": False}


def _row_event_hash(row: dict[str, Any]) -> str:
    material = (
        f"{row['prev_hash']}|{row['event_id']}|{row['task_id'] or ''}|"
        f"{row['event_type']}|{row['payload_json']}|{float(row['created_at']):.6f}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def verify_event_chain_sqlite_ro(db_path: Path) -> dict[str, Any]:
    """Full chain check over mode=ro connection; never initialize / never write views."""
    db_path = Path(db_path)
    if db_path.is_symlink() or not db_path.is_file():
        raise RuntimeError("STOP: event DB missing")
    with sqlite3.connect(f"file:{quote(str(db_path.resolve()))}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY sequence")]
    if not rows:
        raise RuntimeError("STOP: events empty")
    prev = "0" * 64
    for row in rows:
        if row.get("prev_hash") != prev:
            return {
                "ok": False,
                "sequence": row.get("sequence"),
                "reason": "prev_hash mismatch",
            }
        expect = _row_event_hash(row)
        if row.get("event_hash") != expect:
            return {
                "ok": False,
                "sequence": row.get("sequence"),
                "reason": "event_hash mismatch",
            }
        prev = row["event_hash"]
    return {"ok": True, "events": len(rows), "mode": "sqlite_ro_full_scan"}


def verify_event_chain_live(live: Path) -> dict[str, Any]:
    db_path = Path(live) / ".runtime/agentos.sqlite3"
    result = verify_event_chain_sqlite_ro(db_path)
    if result.get("ok") is not True:
        raise RuntimeError("STOP: live event chain failed: " + str(result))
    return result


def initialize_attempt_state(bind: Bind, original: dict[str, Any]) -> Path:
    refuse_half_package(bind)
    bind.A.mkdir(parents=True, exist_ok=True)
    bind.O.mkdir(parents=True, exist_ok=True)
    state = dict(original)
    state.pop("verified_package", None)
    state.pop("package_files_manifest_sha256", None)
    state["step4_attempt"] = bind.attempt
    state["version"] = bind.version
    state["round"] = bind.round
    target = bind.A / "STATE.json"
    if target.exists():
        raise RuntimeError("STOP: attempt STATE.json already exists")
    with target.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    (bind.A / "STEP4.started").mkdir(exist_ok=False)
    return target


def run_prebuild_gates(bind: Bind, *, skip_live: bool = False) -> dict[str, Any]:
    check_interpreter()
    bind.require_ready()
    r5 = verify_r5_migration_binding(bind.data)
    manifests = verify_tool_manifests(bind.root, bind.tool_manifests)
    refuse_half_package(bind)
    state = load_acceptance_state(bind)
    hashes = verify_freeze_prompt_source(bind, state)
    votes = verify_two_independent_votes(bind, state)
    live = {"ok": True, "skipped": True}
    chain = {"ok": True, "skipped": True}
    if not skip_live:
        live = verify_live_idle(bind.live)
        chain = verify_event_chain_live(bind.live)
    if bind.online_manifest_sha256:
        got = sha(bind.live / "SOURCE_MANIFEST.json")
        if got != bind.online_manifest_sha256:
            raise RuntimeError("STOP: live SOURCE_MANIFEST drift")
    return {
        "ok": True,
        "migration_r5": r5,
        "manifests": manifests,
        "hashes": hashes,
        "votes": votes,
        "live_idle": live,
        "event_chain": chain,
        "state_task_keys": sorted((state.get("tasks") or {}).keys()),
        "helpers_dir": str(bind.W),
        "stage_dir": str(bind.stage_dir),
    }
