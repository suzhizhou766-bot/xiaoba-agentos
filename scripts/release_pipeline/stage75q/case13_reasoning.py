"""Case 13: full previous-release module import compare + dynamic model/effort baseline.
Preserves the NameError fix pattern: previous_package / OLD path resolved before use.
Fake CLI only — never starts a real Agent.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bind import Bind, load_bind
from workday_isolation import assert_config


def roster_baseline(live_db: Path) -> dict[str, Any]:
    """Read model/effort from the roster; an unset effort remains unset."""
    with closing(sqlite3.connect(Path(live_db).as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT id, data FROM ri_sources").fetchall()
    models = []
    for _nid, raw in rows:
        data = json.loads(raw)
        node = data.get("node") or data
        if node.get("removed"):
            continue
        if not node.get("enabled"):
            continue
        models.append(
            {
                "reviewer_id": data.get("reviewer_id") or _nid,
                "model": node.get("model"),
                "reasoning_effort": node.get("reasoning_effort"),
                "agent_type": node.get("agent_type"),
            }
        )
    if not models:
        raise RuntimeError("STOP: no enabled roster nodes for dynamic baseline")
    # Prefer a qoder-like CLI node if present; else first enabled.
    preferred = next((m for m in models if m.get("agent_type") == "qoder"), models[0])
    return {
        "model": preferred["model"],
        "reasoning_effort": preferred.get("reasoning_effort"),
        "effort_source": "roster_node" if preferred.get("reasoning_effort") is not None else "roster_unset",
        "agent_type": preferred.get("agent_type"),
        "reviewer_id": preferred["reviewer_id"],
        "hardcoded": False,
        "enabled_count": len(models),
    }


def legacy_command(old_module: Path, old_sha: str, entry: str, model: str) -> list:
    body = old_module.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    if digest != old_sha:
        raise RuntimeError("STOP: previous release argv implementation changed")
    program = "\n".join(
        [
            "import json, sys",
            "from types import SimpleNamespace",
            "sys.path.insert(0, sys.argv[1])",
            "from xiaoba_review.resource_vendor_cli import QoderCliResource",
            'resource = SimpleNamespace(model=sys.argv[3], agent_type="qoder", reasoning_effort=None)',
            "print(json.dumps(QoderCliResource._command(resource, sys.argv[2], 90)))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", program, str(old_module.parent.parent), entry, model],
        env={"PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def run_case13(bind: Bind, *, live_db: Path | None = None) -> dict[str, Any]:
    scene = json.loads((bind.O / "SCENE.json").read_text(encoding="utf-8"))
    root = Path(scene["root"]).resolve()
    if not root.is_relative_to(bind.root / "rehearsal"):
        raise RuntimeError("STOP: case13 scene escapes rehearsal")
    assert_config(Path(scene["config"]), root)

    old_module = bind.previous_resource_vendor_cli
    old_sha = bind.previous_resource_vendor_cli_sha256
    if old_module is None or old_sha is None:
        raise RuntimeError("STOP: previous_resource_vendor_cli bind missing")

    package = Path(scene["runtime"]) / bind.scene_version_dir
    sys.path.insert(0, str(package / "agentos/src"))
    from xiaoba_agentos.store import EventStore
    from xiaoba_review.agent_pool import AgentPool, REASONING_EFFORT, digest, node_state
    from xiaoba_review.node_runtime import readiness, spec
    from xiaoba_review.resource_vendor_cli import QoderCliResource
    from xiaoba_review.store import ReviewStore, canonical

    baseline = roster_baseline(live_db or (bind.live / ".runtime/agentos.sqlite3"))
    model = baseline["model"]
    # Preserve the selected roster record. Its effort applies to this Qoder
    # adapter check only when the selected node itself uses Qoder.
    effort = None
    effort_source = "no_qoder_roster_node"
    argv_skip_reason = "no Qoder roster node; Qoder effort flag check is not applicable"
    if baseline["agent_type"] == "qoder":
        effort = baseline["reasoning_effort"]
        effort_source = baseline["effort_source"]
        argv_skip_reason = "roster has no configured effort; no fallback invented"
    allowed_efforts = REASONING_EFFORT.get("qoder", frozenset())
    if effort is not None and (not isinstance(effort, str) or effort not in allowed_efforts):
        raise RuntimeError(("STOP: roster effort is outside the product Qoder contract", effort,
                            sorted(allowed_efforts)))

    isolated = root / "case13-isolated"
    isolated.mkdir(exist_ok=False)
    native = EventStore(isolated / "runtime/db.sqlite")
    native.initialize()
    review = ReviewStore(native, isolated / "outbox")
    identity = {"protocol": "TEST-case13-fake", "implementation_sha256": "a" * 64}
    adapter = SimpleNamespace(supports_resource_plan=True, resource_identity=lambda: identity)
    backends = SimpleNamespace(adapter_for=lambda source: adapter, retire_resource=lambda node_id: None)
    pool = AgentPool(review, backends)
    entry = isolated / "fake-qoder"
    log = isolated / "argv.jsonl"
    entry.write_text(
        "#!"
        + sys.executable
        + "\nimport json,os,sys\n"
        'with open(os.environ["CASE13_LOG"],"a",encoding="utf-8") as out:\n'
        ' out.write(json.dumps(sys.argv[1:])+"\\n")\n',
        encoding="utf-8",
    )
    entry.chmod(0o700)

    def add(node_id, effort_value=None):
        payload = {
            "node_id": node_id,
            "name": node_id,
            "agent": "TEST Qoder",
            "kind": "review",
            "interface": "cli",
            "entry": str(entry),
            "model": model,
            "agent_type": "qoder",
        }
        if effort_value is not None:
            payload["reasoning_effort"] = effort_value
        return pool.add(payload)

    unset = add("TEST-case13-unset")
    unset_argv = QoderCliResource(unset, isolated / "workspaces")._command(str(entry), 90)
    expected_legacy = legacy_command(old_module, old_sha, str(entry), model)
    if unset_argv != expected_legacy:
        raise RuntimeError("STOP: unset argv changed from previous release")
    commands = [unset_argv]
    if effort is not None:
        with_effort = add("TEST-case13-effort", effort)
        effort_argv = QoderCliResource(with_effort, isolated / "workspaces")._command(str(entry), 90)
        if effort_argv != expected_legacy + ["--reasoning-effort", effort]:
            raise RuntimeError("STOP: effort argv mismatch")
        commands.append(effort_argv)
    for argv in commands:
        subprocess.run(
            argv,
            cwd=isolated,
            env={"CASE13_LOG": str(log)},
            check=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    seen = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    if seen != [argv[1:] for argv in commands]:
        raise RuntimeError("STOP: fake CLI argv capture mismatch")

    # For a configured effort, exercise unset -> configured. When the roster
    # leaves effort unset, use a product-declared synthetic value -> unset to
    # keep the qualification invalidation check meaningful without a fallback.
    initial_effort = None if effort is not None else next(iter(sorted(allowed_efforts)), None)
    if initial_effort == effort:
        raise RuntimeError("STOP: product declares no Qoder effort for qualification change fixture")
    original = add("TEST-case13-digest", initial_effort)
    node = node_state(original)
    node["enabled"] = True
    original["node"] = node
    evidence = {"kind": "TEST synthetic isolated evidence"}
    qualification = {
        "status": "passed",
        "spec_digest": digest(spec(original)),
        "runner_digest": digest(identity),
        "evidence_digest": digest(evidence),
        "execution_evidence": evidence,
        "test_id": "TEST-case13",
        "reason": "TEST",
    }
    with review.transaction() as db:
        event = native._append_event_tx(
            db,
            None,
            "agent_pool.test_finished",
            {"node_id": original["reviewer_id"], "test": qualification},
        )
        node["test"] = dict(qualification, event_id=event)
        original["node"] = node
        db.execute(
            "UPDATE ri_sources SET data=? WHERE id=?",
            (canonical(original), original["reviewer_id"]),
        )
    with native.read_only() as db:
        before_ready = readiness(db, original, backends.adapter_for)
    if before_ready["ready"] is not True:
        raise RuntimeError(before_ready)
    before = digest(spec(original))
    edited = pool.edit(
        {
            "node_id": original["reviewer_id"],
            "expected_revision": node["revision"],
            "name": node["name"],
            "entry": node["entry"],
            "model": node["model"],
            "reasoning_effort": effort,
        }
    )
    after = digest(spec(edited))
    if before == after:
        raise RuntimeError("STOP: spec digest did not change")
    if node_state(edited)["test"]["status"] != "invalidated":
        raise RuntimeError("STOP: test status not invalidated")
    with native.read_only() as db:
        after_ready = readiness(db, edited, backends.adapter_for)
    if not (after_ready["ready"] is False and after_ready["reason"] == "test_required"):
        raise RuntimeError(after_ready)

    output = {
        "ok": True,
        "fake_cli_only": True,
        "actual_argv_effort": seen[1] if effort is not None else None,
        "effort_checks": {
            "argv_status": "passed" if effort is not None else "skipped",
            "argv_skip_reason": None if effort is not None else argv_skip_reason,
            "effort_source": effort_source,
            "qualification_status": "passed",
            "qualification_transition": {"from": initial_effort, "to": effort},
            "qualification_initial_source": "unset" if initial_effort is None else "product REASONING_EFFORT synthetic fixture",
        },
        "unset_matches_previous_release_argv": True,
        "old_source_sha256": old_sha,
        "dynamic_baseline": baseline,
        "qualification_fixture": "synthetic scene database",
        "spec_before": before,
        "spec_after": after,
        "ready_before": before_ready,
        "ready_after": after_ready,
        "test_after": node_state(edited)["test"]["status"],
        "nameerror_fix_previous_resolved_before_use": True,
    }
    (bind.O / "50-reasoning-case13.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(output, ensure_ascii=False), flush=True)
    return output


def main() -> int:
    run_case13(load_bind())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
