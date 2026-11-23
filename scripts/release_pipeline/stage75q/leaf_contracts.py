"""Shared leaf-parameter contracts: no hardcoded historical round paths / node configs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from bind import Bind


def acceptance_marked_passed(state: dict[str, Any]) -> bool:
    """P shared STATE may use acceptance_passed; mature v14 used card49_acceptance_passed."""
    if state.get("acceptance_passed") is True:
        return True
    if state.get("card49_acceptance_passed") is True:
        return True
    return False


def require_acceptance_passed(state: dict[str, Any]) -> None:
    if not acceptance_marked_passed(state):
        raise RuntimeError(
            "STOP: acceptance not marked passed "
            "(need acceptance_passed or card49_acceptance_passed)"
        )


def resolve_scene_drain_path(bind: Bind) -> Path:
    """Drain list comes from current record/work — never hardcoded work/v1614-r7/…."""
    raw = bind.data.get("scene_drain_path")
    if raw:
        return Path(raw)
    return bind.work / "scene_drain.json"


def load_scene_drain_task_ids(bind: Bind) -> list[str]:
    path = resolve_scene_drain_path(bind)
    if not path.is_file() or path.is_symlink():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = data.get("task_ids") if isinstance(data, dict) else None
    if ids is None:
        raise RuntimeError("STOP: scene_drain.json missing task_ids: " + str(path))
    if not isinstance(ids, list) or any(not isinstance(x, str) or not x for x in ids):
        raise RuntimeError("STOP: scene_drain.task_ids must be non-empty strings list or empty list")
    return list(ids)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resolve_scene_config_template(bind: Bind) -> Path:
    """Bind MANAGEMENT-STATE live_config_path + live_config_sha256_before.

    Do not refuse by dirname (model-overrides is the real DEFAULT_CONFIG).
    Do not fall back to .runtime/active.json or guess models from filename.
    """
    raw = bind.data.get("live_config_path") or bind.data.get("scene_config_template")
    if not raw:
        raise RuntimeError(
            "STOP: live_config_path missing from STATE/bind; refuse active.json fallback"
        )
    path = Path(raw)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("STOP: live config template missing or symlink: " + str(path))
    expect = bind.data.get("live_config_sha256_before")
    if not expect:
        raise RuntimeError("STOP: live_config_sha256_before missing; refuse unbound config template")
    got = _file_sha256(path)
    if got != expect:
        raise RuntimeError(
            "STOP: live_config hash drift before scene copy: expected "
            + str(expect)
            + " got "
            + got
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("STOP: live config must be a JSON object")
    # B09 shape: real activity config carries cli_routes / release / worker bindings.
    for key in ("cli_routes", "release", "worker"):
        if key not in data:
            raise RuntimeError("STOP: live config missing B09 key: " + key)
    if not isinstance(data.get("cli_routes"), dict) or not data["cli_routes"]:
        raise RuntimeError("STOP: live config cli_routes empty or malformed")
    return path.resolve()


def resolve_fake_dispatch(bind: Bind) -> Path:
    """fake_dispatch lives under record-derived helpers_dir, not work/v1614-r7/…."""
    raw = bind.data.get("fake_dispatch_path")
    if raw:
        path = Path(raw)
    else:
        path = bind.W / "fake_dispatch.py"
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("STOP: fake_dispatch.py missing under helpers_dir: " + str(path))
    return path


def _manifest_review_store_sha(candidate: Path | None) -> str | None:
    if candidate is None:
        return None
    manif = Path(candidate) / "SOURCE_MANIFEST.json"
    if not manif.is_file() or manif.is_symlink():
        return None
    data = json.loads(manif.read_text(encoding="utf-8"))
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return None
    sha = files.get("src/xiaoba_review/store.py")
    return str(sha) if sha else None


def _package_review_store_sha(pkg: Path | None) -> str | None:
    if pkg is None:
        return None
    files_path = Path(pkg) / "FILES.sha256"
    if not files_path.is_file() or files_path.is_symlink():
        return None
    for line in files_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].endswith("agentos/src/xiaoba_review/store.py"):
            return parts[0]
    return None


def _verify_review_root(root: Path, expected_sha: str | None) -> Path | None:
    root = Path(root)
    if root.is_symlink():
        return None
    store = root / "xiaoba_review" / "store.py"
    if store.is_symlink() or not store.is_file():
        return None
    got = _file_sha256(store)
    if expected_sha is not None and got != expected_sha:
        raise RuntimeError(
            "STOP: review-ingress store.py hash mismatch under "
            + str(root)
            + ": expected "
            + expected_sha
            + " got "
            + got
        )
    return root.resolve()


def resolve_review_ingress_root(bind: Bind | None = None, candidate: Path | None = None) -> Path:
    """Locate frozen/export ReviewStore only — never prefer live working-tree development.

    Order: explicit bind → verified package agentos/src → candidate/src →
    candidate.parent/review-ingress (cc8 projection sibling). Hash must match
    bind.review_store_sha256 or SOURCE_MANIFEST/FILES entry. No ensure-string gate.
    """
    extras: list[Path] = []
    expected: str | None = None
    if bind is not None:
        raw = bind.data.get("review_ingress_root")
        if raw:
            extras.append(Path(raw))
        expected = bind.data.get("review_store_sha256")
        candidate = candidate or bind.C
        extras.append(bind.PKG / "agentos" / "src")
        if expected is None:
            expected = _package_review_store_sha(bind.PKG)
    cand: Path | None = Path(candidate).resolve() if candidate is not None else None
    if cand is not None:
        extras.append(cand / "src")
        extras.append(cand.parent / "review-ingress")
        if expected is None:
            expected = _manifest_review_store_sha(cand)
    if expected is None:
        raise RuntimeError(
            "STOP: review_store_sha256 unbound (no bind hash / FILES / SOURCE_MANIFEST); "
            "refuse live working-tree fallback"
        )

    seen: set[str] = set()
    last_hash_error: Exception | None = None
    for root in extras:
        key = str(Path(root))
        if key in seen:
            continue
        seen.add(key)
        try:
            accepted = _verify_review_root(root, expected)
        except RuntimeError as exc:
            last_hash_error = exc
            continue
        if accepted is not None:
            return accepted

    if last_hash_error is not None:
        raise last_hash_error
    raise RuntimeError(
        "STOP: frozen review-ingress (ReviewStore) not found under package/export; "
        "set bind.review_ingress_root + review_store_sha256 matching SOURCE_MANIFEST/FILES; "
        "refuse live working-tree fallback"
    )


def previous_package_version(bind: Bind) -> str:
    """Read formal previous version from previous_package RELEASE.json — never hardcode."""
    release = Path(bind.previous_package) / "RELEASE.json"
    if release.is_symlink() or not release.is_file():
        raise RuntimeError("STOP: previous_package RELEASE.json missing: " + str(release))
    data = json.loads(release.read_text(encoding="utf-8"))
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise RuntimeError("STOP: previous_package RELEASE.version missing")
    return version


def load_migration_checks_module(bind: Bind):
    """Leaf-safe load of pointer-bound migration_checks-r5 by path+hash (no sys.path accident)."""
    import importlib.util
    import sys

    path_raw = bind.data.get("migration_checks")
    expect = bind.data.get("migration_checks_sha256")
    if not path_raw or not expect:
        raise RuntimeError("STOP: migration_checks path/hash not bound from ATTEMPT-POINTER")
    path = Path(path_raw)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("STOP: migration_checks missing or symlink: " + str(path))
    got = _file_sha256(path)
    if got != expect:
        raise RuntimeError("STOP: migration_checks hash drift: " + got)
    name = "stage75q_pointer_migration_checks_r5"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("STOP: cannot load migration_checks from " + str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    # _norm belongs to the same frozen tool family, not the product API; the
    # pointer's path and SHA-256 were verified above before relying on it.
    for attr in (
        "assert_indexes_present",
        "assert_fingerprint",
        "previous_package_can_use_indexed_db",
        "list_indexes",
        "INDEXES",
        "EXPECTED_INDEX_SQL",
        "_norm",
    ):
        if not hasattr(mod, attr):
            raise RuntimeError("STOP: migration_checks missing " + attr)
    return mod


def load_migration_declaration(bind: Bind) -> dict[str, Any]:
    """Manager migration-spec: schema_version + new_indexes{name:sql}. Never hardcode 7/{}."""
    raw = bind.data.get("migration_spec")
    path = Path(raw) if raw else (Path(bind.work) / "migration-spec.json")
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("STOP: migration-spec.json missing: " + str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("STOP: migration-spec must be an object")
    schema_version = data.get("schema_version")
    new_indexes = data.get("new_indexes")
    if not isinstance(schema_version, int):
        raise RuntimeError("STOP: migration-spec.schema_version must be int")
    if not isinstance(new_indexes, dict):
        raise RuntimeError("STOP: migration-spec.new_indexes must be an object {name: sql}")
    for name, sql in new_indexes.items():
        if not isinstance(name, str) or not name or not isinstance(sql, str) or not sql.strip():
            raise RuntimeError("STOP: migration-spec.new_indexes entries must be name→sql strings")
    return {
        "schema_version": schema_version,
        "new_indexes": dict(new_indexes),
        "fresh_database_sha256": data.get("fresh_database_sha256") or "",
        "path": str(path.resolve()),
        "sha256": _file_sha256(path),
    }


def parameterize_migration_checks(mc: Any, declaration: dict[str, Any]) -> dict[str, Any]:
    """Apply the same runtime parameterization as prepare_round-r4.run_migration.

    Putting new_indexes only inside expected{} has no effect; r5 reads module globals
    INDEXES / EXPECTED_INDEX_SQL.
    """
    new_indexes = declaration.get("new_indexes")
    schema_version = declaration.get("schema_version")
    if not isinstance(new_indexes, dict):
        raise RuntimeError("STOP: new_indexes must be an object {name: sql}")
    if not isinstance(schema_version, int):
        raise RuntimeError("STOP: schema_version must be int")
    mc.INDEXES = tuple(new_indexes)
    mc.EXPECTED_INDEX_SQL = {name: mc._norm(sql) for name, sql in new_indexes.items()}
    expected = {
        "schema_version": schema_version,
        "sha256": declaration.get("fresh_database_sha256") or "",
        "objects": 0,
    }
    return {
        "ok": True,
        "schema_version": schema_version,
        "new_indexes": dict(new_indexes),
        "declared_indexes": len(new_indexes),
        "INDEXES": list(mc.INDEXES),
        "EXPECTED_INDEX_SQL": dict(mc.EXPECTED_INDEX_SQL),
        "expected_fingerprint": expected,
    }


def run_declared_migration_checks(
    bind: Bind,
    db_path: Path,
    *,
    previous_package: Path | None = None,
    scene_root: Path | None = None,
) -> dict[str, Any]:
    """Load r5, apply manager declaration, then assert_indexes/fingerprint (+ optional rollback)."""
    declaration = load_migration_declaration(bind)
    mc = load_migration_checks_module(bind)
    applied = parameterize_migration_checks(mc, declaration)
    indexes = mc.assert_indexes_present(db_path)
    fingerprint = mc.assert_fingerprint(db_path, applied["expected_fingerprint"])
    out: dict[str, Any] = {
        "ok": True,
        "declaration": {
            "path": declaration["path"],
            "sha256": declaration["sha256"],
            "schema_version": declaration["schema_version"],
            "new_indexes": declaration["new_indexes"],
        },
        "parameterized": applied,
        "indexes_present": indexes,
        "fingerprint": fingerprint,
        "migration_checks": {
            "path": str(bind.migration_checks),
            "sha256": bind.migration_checks_sha256,
            "r5_bound": True,
        },
    }
    if previous_package is not None and scene_root is not None:
        out["migration_rollback_previous_package"] = mc.previous_package_can_use_indexed_db(
            Path(previous_package) / "agentos", Path(db_path), Path(scene_root)
        )
        if out["migration_rollback_previous_package"].get("ok") is not True:
            raise RuntimeError(
                "STOP: previous_package rollback compat failed under declared indexes: "
                + str(out["migration_rollback_previous_package"])
            )
    return out


def resolve_post_switch_package(bind: Bind, state: dict[str, Any], scene: dict[str, Any]) -> dict[str, Any]:
    """Post-switch runtime must load core/review/ledger from the switched new package — not previous."""
    candidates: list[Path] = []
    if state.get("verified_package"):
        candidates.append(Path(state["verified_package"]))
    if scene.get("runtime"):
        candidates.append(Path(scene["runtime"]) / ".runtime" / "releases" / bind.version)
    candidates.append(Path(bind.PKG))
    prev = Path(bind.previous_package).resolve()
    chosen = None
    for cand in candidates:
        if cand.is_symlink() or not cand.is_dir():
            continue
        release = cand / "RELEASE.json"
        src = cand / "agentos" / "src"
        if release.is_symlink() or not release.is_file() or not src.is_dir():
            continue
        data = json.loads(release.read_text(encoding="utf-8"))
        if data.get("version") != bind.version:
            continue
        if cand.resolve() == prev:
            continue
        chosen = cand.resolve()
        break
    if chosen is None:
        raise RuntimeError(
            "STOP: post-switch new package not found for version "
            + bind.version
            + "; refuse previous_package core fallback"
        )
    src = chosen / "agentos" / "src"
    if not (src / "xiaoba_agentos").is_dir():
        raise RuntimeError("STOP: post-switch package missing agentos/src/xiaoba_agentos: " + str(src))
    # Prefer in-package review; else frozen export bound to this package via resolve_review_ingress_root.
    review_root = src if (src / "xiaoba_review" / "store.py").is_file() else resolve_review_ingress_root(bind)
    if Path(review_root).resolve() == prev / "agentos" / "src":
        raise RuntimeError("STOP: review root resolved to previous_package; refuse mixed 14/15 import")
    return {
        "package": chosen,
        "agentos_src": src.resolve(),
        "review_ingress_root": Path(review_root).resolve(),
        "version": bind.version,
        "release_sha256": _file_sha256(chosen / "RELEASE.json"),
        "previous_package_refused": str(prev),
    }


def verify_same_package_modules(
    agentos_src: Path,
    review_ingress_root: Path,
    previous_package: Path,
    *,
    event_store_mod: Any,
    review_store_mod: Any,
    workday_ledger_mod: Any,
) -> dict[str, Any]:
    """Record and enforce EventStore/ReviewStore/workday_ledger come from the switched new package tree."""
    agentos_src = Path(agentos_src).resolve()
    review_root = Path(review_ingress_root).resolve()
    prev = Path(previous_package).resolve()

    def _file(mod: Any) -> Path:
        path = Path(getattr(mod, "__file__", "")).resolve()
        if not path.is_file():
            raise RuntimeError("STOP: module __file__ missing: " + repr(mod))
        return path

    def _refuse_previous(path: Path, label: str) -> None:
        try:
            if path.is_relative_to(prev):
                raise RuntimeError(
                    f"STOP: {label} loaded from previous_package after switch: {path}"
                )
        except AttributeError:
            if str(prev) in str(path):
                raise RuntimeError(
                    f"STOP: {label} loaded from previous_package after switch: {path}"
                )

    es = _file(event_store_mod)
    wl = _file(workday_ledger_mod)
    rs = _file(review_store_mod)
    for path, label in ((es, "EventStore"), (wl, "workday_ledger"), (rs, "ReviewStore")):
        _refuse_previous(path, label)
    if not es.is_relative_to(agentos_src):
        raise RuntimeError(f"STOP: EventStore not under post-switch agentos/src: {es}")
    if not wl.is_relative_to(agentos_src):
        raise RuntimeError(f"STOP: workday_ledger not under post-switch agentos/src: {wl}")
    if not (rs.is_relative_to(agentos_src) or rs.is_relative_to(review_root)):
        raise RuntimeError(
            f"STOP: ReviewStore not under post-switch package/review root: {rs}"
        )
    return {
        "ok": True,
        "agentos_src": str(agentos_src),
        "review_ingress_root": str(review_root),
        "event_store": str(es),
        "review_store": str(rs),
        "workday_ledger": str(wl),
        "event_store_sha256": _file_sha256(es),
        "review_store_sha256": _file_sha256(rs),
        "workday_ledger_sha256": _file_sha256(wl),
    }
