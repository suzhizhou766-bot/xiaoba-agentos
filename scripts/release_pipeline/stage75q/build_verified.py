"""Formal-build leaf: actually invokes build_release with full argv; parses RELEASE/FILES.
Tier is read back from RELEASE.json (computed by the release tool), never forced to migration.
PREVIOUS_PACKAGE is bound before argv assembly (preserves the NameError fix from v14).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable

from bind import PIN, Bind, clean_env, load_bind, sha, write_bind


REQUIRED_EVIDENCE_KEYS = ("conclusions", "pool_nodes", "execution")


def build_argv(bind: Bind, state: dict[str, Any], preparation: dict[str, Any]) -> list[str]:
    """Assemble full build_release parameters. previous_package is resolved first."""
    previous_package = Path(bind.previous_package)
    if not previous_package.exists():
        raise RuntimeError("STOP: previous_package missing: " + str(previous_package))
    C = bind.C
    tests = preparation.get("tests") or {}
    test_count = tests.get("core")
    if test_count is None:
        raise RuntimeError("STOP: PREPARATION.tests.core missing")
    task_id = next(iter((state.get("tasks") or {}).values()))
    verified_date = str(state.get("verified_date") or date.today().isoformat())
    args = [
        str(PIN),
        "-B",
        str(C / "scripts/build_release.py"),
        "--agentos-root",
        str(C),
        "--interface",
        str(C.parent / "interface/SKILL.md"),
        "--stable-scripts",
        str(C / ".agents/skills/xiaoba-agentos/scripts"),
        "--second-brain",
        str(bind.generated_brain),
        "--published-agentos-root",
        str(bind.live),
        "--target",
        str(bind.PKG),
        "--version",
        bind.version,
        "--verified-date",
        verified_date,
        "--test-count",
        str(test_count),
        "--test-result",
        "passed",
        "--acceptance-complete",
        "--acceptance-database",
        str(state["database"]),
        "--acceptance-task-id",
        str(task_id),
        "--previous-release",
        str(previous_package),
    ]
    return args


def run_formal_build(
    bind: Bind,
    *,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    build_script_override: Path | None = None,
) -> dict[str, Any]:
    bind.require_ready()
    state_path = bind.A / "STATE.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    from leaf_contracts import require_acceptance_passed

    require_acceptance_passed(state)
    if sha(bind.C / "SOURCE_MANIFEST.json") != state["source_manifest_sha256"]:
        raise RuntimeError("STOP: source hash drifted before build")
    if sha(bind.acceptance_root / "FREEZE.json") != state["freeze_sha256"]:
        raise RuntimeError("STOP: freeze hash drifted before build")
    if sha(Path(state["prompt"])) != state["prompt_sha256"]:
        raise RuntimeError("STOP: prompt hash drifted before build")

    preparation = json.loads(bind.preparation_path.read_text(encoding="utf-8"))
    args = build_argv(bind, state, preparation)
    if build_script_override is not None:
        # Isolation leaf: keep full flag order, only replace the script path slot.
        args = list(args)
        args[2] = str(build_script_override)

    bind.O.mkdir(parents=True, exist_ok=True)
    (bind.O / "20-formal-build-command.json").write_text(
        json.dumps({"argv": args, "cwd": str(bind.C)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if bind.PKG.exists() and any(bind.PKG.iterdir()):
        raise RuntimeError("STOP: package target not empty")
    marker = bind.A / "FORMAL_BUILD.started"
    if marker.exists():
        raise RuntimeError("STOP: FORMAL_BUILD.started already present")
    marker.write_text("One formal build only. Do not remove.\n", encoding="utf-8")

    run = runner or subprocess.run
    env = clean_env()
    proc = run(args, cwd=str(bind.C), capture_output=True, text=True, env=env)
    log = (proc.stdout or "") + (proc.stderr or "")
    (bind.O / "20-formal-build.log").write_text(log, encoding="utf-8")
    print(log, flush=True)
    if proc.returncode:
        raise RuntimeError(
            f"STOP: build_release exit={proc.returncode}; log kept at {bind.O / '20-formal-build.log'}"
        )

    release_path = bind.PKG / "RELEASE.json"
    files_path = bind.PKG / "FILES.sha256"
    if not release_path.is_file():
        raise RuntimeError("STOP: RELEASE.json missing after build")
    if not files_path.is_file():
        raise RuntimeError("STOP: FILES.sha256 missing after build")
    if sha(bind.C / "SOURCE_MANIFEST.json") != state["source_manifest_sha256"]:
        raise RuntimeError("STOP: source changed during build")

    release = json.loads(release_path.read_text(encoding="utf-8"))
    if release.get("status") != "verified_immutable_release":
        raise RuntimeError("STOP: unexpected release status: " + str(release.get("status")))
    # Tier is computed by the tool; record it, do not assert a fixed 'migration'.
    tier = (release.get("release_tier") or {}).get("tier")
    evidence = (release.get("acceptance") or {}).get("end_to_end_evidence") or {}
    if not isinstance(evidence, dict) or not set(REQUIRED_EVIDENCE_KEYS) <= set(evidence):
        raise RuntimeError("STOP: end_to_end_evidence missing required keys")
    if "_source" in evidence:
        raise RuntimeError("STOP: end_to_end_evidence must not contain _source")

    state["verified_package"] = str(bind.PKG)
    state["package_files_manifest_sha256"] = sha(files_path)
    state["release_tier_readback"] = tier
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = {
        "ok": True,
        "package": str(bind.PKG),
        "files_sha256": state["package_files_manifest_sha256"],
        "status": release["status"],
        "release_tier": tier,
        "end_to_end_keys": sorted(evidence),
        "argv": args,
        "forced_migration_tier": False,
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    bind = load_bind()
    run_formal_build(bind)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
