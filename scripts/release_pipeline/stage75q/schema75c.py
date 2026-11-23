"""75C extension check: product ReviewStore owns create/migrate; tools only verify readonly."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

from leaf_contracts import resolve_review_ingress_root

OLD_FOUR = ("id", "data", "created_at", "attempts")
NEW_SIX = ("id", "data", "created_at", "attempts", "state", "last_error")


def table_columns(db_path: Path, table: str) -> list[str]:
    with sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def check_ri_pending_references(
    db_path: Path,
    *,
    require_present: bool,
    phase: str,
) -> dict[str, Any]:
    db_path = Path(db_path)
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
        exists = bool(
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ri_pending_references'"
            ).fetchone()
        )
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if version != 7:
        raise RuntimeError(f"STOP: core schema must stay 7, got {version} at {phase}")
    if not exists:
        if require_present:
            raise RuntimeError(f"STOP: ri_pending_references missing after {phase}")
        return {
            "ok": True,
            "phase": phase,
            "table_present": False,
            "schema_version": 7,
            "new_indexes": 0,
            "note": "v14 live may lack extension table; not a pre-switch failure",
        }
    cols = table_columns(db_path, "ri_pending_references")
    if set(NEW_SIX) <= set(cols):
        return {
            "ok": True,
            "phase": phase,
            "table_present": True,
            "columns": cols,
            "schema_version": 7,
            "six_columns": True,
            "new_indexes": 0,
            "migrated_from_four": set(OLD_FOUR) <= set(cols),
        }
    if cols == list(OLD_FOUR):
        return {
            "ok": False,
            "phase": phase,
            "table_present": True,
            "columns": cols,
            "schema_version": 7,
            "needs_four_to_six_migration": True,
            "new_indexes": 0,
        }
    raise RuntimeError(f"STOP: unexpected ri_pending_references columns at {phase}: {cols}")


def _load_review_store(review_ingress_root: Path, agentos_src: Path):
    review_ingress_root = Path(review_ingress_root).resolve()
    agentos_src = Path(agentos_src).resolve()
    sys.path[:0] = [str(agentos_src), str(review_ingress_root)]
    # Import product module from disk path to avoid stale synthetic stubs.
    store_path = review_ingress_root / "xiaoba_review" / "store.py"
    name = "_stage75q_product_review_store"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, store_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None
    # Ensure package-style relative imports resolve
    pkg_name = "xiaoba_review"
    if pkg_name not in sys.modules:
        pkg = type(sys)("xiaoba_review")
        pkg.__path__ = [str(review_ingress_root / "xiaoba_review")]
        sys.modules[pkg_name] = pkg
    spec.loader.exec_module(mod)
    from xiaoba_agentos.store import EventStore

    return mod.ReviewStore, EventStore


def ensure_pending_via_product(
    db_path: Path,
    *,
    review_ingress_root: Path,
    agentos_src: Path,
    outbox_root: Path,
) -> dict[str, Any]:
    """Call product ReviewStore init / _ensure_pending_reference_columns — never hand-roll ALTER."""
    db_path = Path(db_path)
    outbox_root = Path(outbox_root)
    outbox_root.mkdir(parents=True, exist_ok=True)
    before = None
    if db_path.is_file():
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
            exists = bool(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ri_pending_references'"
                ).fetchone()
            )
            if exists:
                cols = [r[1] for r in db.execute("PRAGMA table_info(ri_pending_references)")]
                if cols == list(OLD_FOUR):
                    before = [
                        dict(zip(OLD_FOUR, r))
                        for r in db.execute(
                            "SELECT id,data,created_at,attempts FROM ri_pending_references ORDER BY id"
                        )
                    ]
    ReviewStore, EventStore = _load_review_store(review_ingress_root, agentos_src)
    native = EventStore(db_path)
    # Product constructor runs CREATE IF NOT EXISTS + _ensure_pending_reference_columns
    ReviewStore(native, outbox_root)
    cols_after = table_columns(db_path, "ri_pending_references")
    if not set(NEW_SIX) <= set(cols_after):
        raise RuntimeError("STOP: product ReviewStore did not yield six columns: " + str(cols_after))
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        after = [
            dict(r)
            for r in db.execute(
                "SELECT id,data,created_at,attempts,state,last_error "
                "FROM ri_pending_references ORDER BY id"
            )
        ]
    if version != 7:
        raise RuntimeError("STOP: core schema drifted during product ensure: " + str(version))
    if before is not None:
        if len(after) != len(before):
            raise RuntimeError("STOP: product migration dropped/added rows unexpectedly")
        for b, a in zip(before, after):
            if a["id"] != b["id"] or a["data"] != b["data"]:
                raise RuntimeError("STOP: product migration altered id/data")
            if float(a["created_at"]) != float(b["created_at"]):
                raise RuntimeError("STOP: product migration altered created_at")
            # ReviewStore.__init__ also runs recover_pending_references; attempts/state may
            # advance after the ALTER, but rows must remain (75C dead-hold, never silent delete).
            if int(a["attempts"]) < int(b["attempts"]):
                raise RuntimeError("STOP: product path decreased attempts")
            if a.get("state") not in ("pending", "dead"):
                raise RuntimeError("STOP: unexpected state after product ensure: " + str(a.get("state")))
    return {
        "ok": True,
        "product_api": "xiaoba_review.store.ReviewStore",
        "migrated": before is not None,
        "rows_before": before,
        "rows_after": after,
        "columns_after": cols_after,
        "schema_version": 7,
        "new_indexes": 0,
        "handwritten_alter": False,
    }


def migrate_four_to_six(
    db_path: Path,
    *,
    review_ingress_root: Path | None = None,
    agentos_src: Path | None = None,
    outbox_root: Path | None = None,
    bind=None,
) -> dict[str, Any]:
    """Compatibility wrapper: always delegates to product ReviewStore ensure."""
    if bind is not None:
        review_ingress_root = review_ingress_root or resolve_review_ingress_root(bind)
        agentos_src = agentos_src or (bind.C / "src")
        outbox_root = outbox_root or (Path(db_path).resolve().parent / "memory-outbox-75c")
    if review_ingress_root is None or agentos_src is None:
        raise RuntimeError("STOP: migrate_four_to_six requires product paths or bind")
    if outbox_root is None:
        outbox_root = Path(db_path).resolve().parent / "memory-outbox-75c"
    return ensure_pending_via_product(
        db_path,
        review_ingress_root=Path(review_ingress_root),
        agentos_src=Path(agentos_src),
        outbox_root=Path(outbox_root),
    )


def assert_scene_extension(db_path: Path, *, bind=None) -> dict[str, Any]:
    result = check_ri_pending_references(db_path, require_present=False, phase="copy_scene")
    if result.get("table_present") and result.get("needs_four_to_six_migration"):
        if bind is None:
            raise RuntimeError("STOP: 75C four→six needs product ReviewStore via bind")
        mig = migrate_four_to_six(db_path, bind=bind)
        result = {
            **check_ri_pending_references(db_path, require_present=True, phase="copy_scene"),
            **mig,
        }
    elif not result.get("table_present"):
        # Product init may create empty six-col table; do not hand-create.
        if bind is None:
            raise RuntimeError("STOP: scene missing ri_pending_references and no bind for product ensure")
        mig = migrate_four_to_six(db_path, bind=bind)
        result = {
            **check_ri_pending_references(db_path, require_present=True, phase="copy_scene"),
            **mig,
            "created_by_product": True,
        }
    if not result.get("ok"):
        raise RuntimeError("STOP: 75C extension not ready in copy scene: " + str(result))
    return result
