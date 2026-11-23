"""Consume shared release_context from driver75p. helpers_dir/stage_dir come from record.step4_helpers_dir."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

STAGE = Path(__file__).resolve().parent
WORK = STAGE.parent


def import_release_context():
    path = WORK / "driver75p" / "release_context.py"
    if not path.is_file():
        raise RuntimeError(
            "STOP: shared driver75p/release_context.py not landed yet; "
            "75Q leaf tools ready, await 75P shared module for live stages"
        )
    name = "driver75p_release_context"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    for attr in ("load_context", "validate_context", "clean_env", "check_interpreter", "verify_manifests"):
        if not hasattr(mod, attr):
            raise RuntimeError(f"STOP: release_context missing {attr}")
    return mod


def _pointer_migration(work: Path) -> tuple[str | None, str | None]:
    pointer = work / "ATTEMPT-POINTER.json"
    if not pointer.is_file():
        return None, None
    data = json.loads(pointer.read_text(encoding="utf-8"))
    return data.get("migration_checks"), data.get("migration_checks_sha256")


def derive_helper_paths(record: dict[str, Any]) -> tuple[Path, Path]:
    """stage_dir = record.step4_helpers_dir; helpers_dir = stage_dir/helpers.
    Manager will set step4_helpers_dir to stage75q; never hardcode __file__ to bypass record.
    """
    raw = record.get("step4_helpers_dir")
    if not raw:
        raise RuntimeError("STOP: record.step4_helpers_dir missing; refuse hardcoded helpers")
    stage_dir = Path(raw)
    helpers_dir = stage_dir / "helpers"
    return stage_dir, helpers_dir


def ctx_to_bind(ctx: dict[str, Any], *, extras: dict[str, Any] | None = None) -> dict[str, Any]:
    record = ctx["record"]
    state = ctx["state"]
    root = Path(ctx["root"])
    round_name = str(ctx["round"])
    version = str(ctx["version"])
    attempt = int(record["attempt"])
    stage_dir, helpers_dir = derive_helper_paths(record)
    mig_path, mig_hash = _pointer_migration(Path(ctx["work"]))
    bind = {
        "root": str(root),
        "work": str(ctx["work"]),
        "live": str(ctx["live"]),
        "repo": str(ctx["repo"]),
        "source": str(ctx["source"]),
        "candidate": str(ctx["candidate"]),
        "acceptance_root": str(ctx["acceptance"]),
        "acceptance_step4_dir": str(record["acceptance_step4_dir"]),
        "reports_step4_dir": str(record["reports_step4_dir"]),
        "releases_package_dir": str(record["releases_package_dir"]),
        "stage_dir": str(stage_dir),
        "helpers_dir": str(helpers_dir),
        "version": version,
        "round": round_name,
        "attempt": attempt,
        "previous_package": str(ctx["previous_package"]),
        "generated_brain": str(
            extras.get("generated_brain")
            if extras and extras.get("generated_brain")
            else root / f"generated-brain-{round_name}"
        ),
        "tool_manifests": list(state.get("tool_manifests") or []),
        "preparation_path": str(Path(ctx["work"]) / "PREPARATION.json"),
        "online_manifest_sha256": state.get("previous_source_sha256"),
        "migration_checks": mig_path,
        "migration_checks_sha256": mig_hash,
        "live_config_path": state.get("live_config_path"),
        "live_config_sha256_before": state.get("live_config_sha256_before"),
        "review_store_sha256": state.get("review_store_sha256"),
        "migration_spec": str(
            Path(extras["migration_spec"]).resolve()
            if extras and extras.get("migration_spec")
            else Path(ctx["work"]) / "migration-spec.json"
        ),
        "review_report": extras.get("review_report") if extras else None,
        "integrity_report": extras.get("integrity_report") if extras else None,
        "previous_resource_vendor_cli": extras.get("previous_resource_vendor_cli") if extras else None,
        "previous_resource_vendor_cli_sha256": extras.get("previous_resource_vendor_cli_sha256")
        if extras
        else None,
    }
    if extras:
        for k, v in extras.items():
            if k not in bind or bind[k] is None:
                bind[k] = v
    return bind


def load_live_context(stage: str = "step4") -> tuple[Any, dict[str, Any], dict[str, Any]]:
    rc = import_release_context()
    rc.check_interpreter()
    ctx = rc.load_context()
    rc.validate_context(ctx, stage)
    rc.verify_manifests(ctx)
    bind = ctx_to_bind(ctx)
    return rc, ctx, bind


def load_live_context_plan() -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Read-only plan path: never writes. Returns (bind_payload_or_None, report)."""
    try:
        rc = import_release_context()
        rc.check_interpreter()
        ctx = rc.load_context()
    except Exception as exc:  # noqa: BLE001
        return None, {
            "ok": False,
            "read_only": True,
            "writes": 0,
            "ready": False,
            "error": str(exc),
            "note": "context not loadable; formal evidence incomplete",
        }
    try:
        rc.validate_context(ctx, "step4")
        rc.verify_manifests(ctx)
        payload = ctx_to_bind(ctx)
        return payload, {"ok": True, "read_only": True, "writes": 0, "ready": True}
    except Exception as exc:  # noqa: BLE001
        # Still expose expected consumer paths from record when possible
        try:
            partial = ctx_to_bind(ctx)
        except Exception:
            partial = None
        return partial, {
            "ok": True,
            "read_only": True,
            "writes": 0,
            "ready": False,
            "error": str(exc),
            "note": "gates not ready; expected argv from record when available",
        }


def load_synthetic_context(bind_path: Path) -> dict[str, Any]:
    data = json.loads(Path(bind_path).read_text(encoding="utf-8"))
    required = {
        "root",
        "work",
        "reports_step4_dir",
        "acceptance_step4_dir",
        "releases_package_dir",
        "helpers_dir",
        "stage_dir",
        "source",
        "candidate",
        "version",
        "round",
        "attempt",
        "live",
        "previous_package",
        "generated_brain",
    }
    missing = sorted(required - set(data))
    if missing:
        raise RuntimeError("STOP: synthetic bind missing: " + ",".join(missing))
    return data


def verify_r5_migration_binding(bind_payload: dict[str, Any]) -> dict[str, Any]:
    """Pointer migration_checks path+hash must match file bytes (r5)."""
    path = bind_payload.get("migration_checks")
    expect = bind_payload.get("migration_checks_sha256")
    if not path or not expect:
        raise RuntimeError("STOP: migration_checks path/hash not bound from pointer")
    p = Path(path)
    if p.is_symlink() or not p.is_file():
        raise RuntimeError("STOP: migration_checks missing or symlink: " + str(p))
    got = hashlib.sha256(p.read_bytes()).hexdigest()
    if got != expect:
        raise RuntimeError("STOP: migration_checks hash drift: " + got)
    if "migration_checks-r5" not in p.name and expect != got:
        pass
    return {"ok": True, "path": str(p), "sha256": got, "r5_bound": True}
