"""Case 14: V1/V2/V8 full event-chain detection in rehearsal scene."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

from bind import Bind, load_bind
from workday_isolation import assert_config
from history_execution import HistoryExecution


def row_hash(row: dict) -> str:
    # Event-chain wire format: UTF-8, pipe-separated fields and six fractional
    # timestamp digits. This independent fixture encoder must track the public
    # chain protocol; it never inspects or compares the product's source text.
    material = (
        f"{row['prev_hash']}|{row['event_id']}|{row['task_id'] or ''}|"
        f"{row['event_type']}|{row['payload_json']}|{row['created_at']:.6f}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def fill(store, n: int = 40) -> None:
    for i in range(n):
        store.append_event(f"task-{i % 7}", "bench.tick", {"i": i})


def require_first_failure(store, sequence: int, *, rounds: int = 2) -> list[dict[str, Any]]:
    """Require a fresh measured history query and the earliest corrupted row."""
    executions = []
    for _ in range(rounds):
        meter = HistoryExecution(store.path, check_write_lock=False)
        with meter.window(store, method="read_only"):
            verdict = store.verify_event_chain()
        if (meter.connections != 1 or meter.history_queries < 1
                or verdict.get("ok") is not False or verdict.get("sequence") != sequence
                or verdict.get("reason") != "event_hash mismatch"):
            raise RuntimeError((
                "STOP: chain first-failure/history-query guard rejected",
                {"expected_sequence": sequence, "execution": meter.result(), "verdict": verdict},
            ))
        executions.append({"execution": meter.result(), "verdict": verdict})
    return executions


def chain_tamper_checks(root: Path, EventStore) -> dict[str, Any]:
    """Exercise head/middle/tail corruption in one fixture, restoring each first error."""
    location = root / "case14-three-position"
    location.mkdir(exist_ok=False)
    store = EventStore(location / "db.sqlite3")
    store.initialize()
    fill(store, 40)
    if store.verify_event_chain().get("ok") is not True:
        raise RuntimeError("STOP: chain corruption fixture did not start valid")
    with store.read_only() as db:
        rows = db.execute("SELECT sequence,payload_json FROM events ORDER BY sequence").fetchall()
    positions = [rows[0]["sequence"], rows[len(rows) // 2]["sequence"], rows[-1]["sequence"]]
    if positions != sorted(set(positions)):
        raise RuntimeError(("STOP: head/middle/tail positions not distinct and ordered", positions))
    originals = {row["sequence"]: row["payload_json"] for row in rows if row["sequence"] in positions}
    with closing(sqlite3.connect(str(store.path))) as db:
        for position in positions:
            db.execute("UPDATE events SET payload_json=? WHERE sequence=?",
                       (json.dumps({"tampered_sequence": position}), position))
        db.commit()
    cases = []
    for expected in positions:
        executions = require_first_failure(store, expected)
        cases.append({"remaining_tampered_sequences": positions[len(cases):],
                      "expected_first_failure": expected, "executions": executions})
        # Restore the original payload so the next call must find a different
        # earliest error in this same database rather than reuse the first verdict.
        with closing(sqlite3.connect(str(store.path))) as db:
            db.execute("UPDATE events SET payload_json=? WHERE sequence=?", (originals[expected], expected))
            db.commit()
    clean = store.verify_event_chain()
    if clean.get("ok") is not True:
        raise RuntimeError(("STOP: restored chain fixture remains invalid", clean))
    return {"ok": True, "positions": positions, "cases": cases, "restored_valid": True,
            "scope": "three seeded positions and changing first failure in one fixture; query counts alone do not prove full-history verification"}


def run_case14(bind: Bind) -> dict[str, Any]:
    scene = json.loads((bind.O / "SCENE.json").read_text(encoding="utf-8"))
    root = Path(scene["root"]).resolve()
    if not root.is_relative_to(bind.root / "rehearsal"):
        raise RuntimeError("STOP: case14 scene escapes rehearsal")
    assert_config(Path(scene["config"]), root)
    package = Path(scene["runtime"]) / bind.scene_version_dir
    sys.path.insert(0, str(package / "agentos/src"))
    from xiaoba_agentos.store import EventStore

    results: dict[str, Any] = {}

    iso = root / "case14-v1"
    iso.mkdir(exist_ok=False)
    store = EventStore(iso / "db.sqlite3")
    store.initialize()
    fill(store, 40)
    assert store.verify_event_chain()["ok"]
    raw = sqlite3.connect(str(iso / "db.sqlite3"))
    raw.execute("UPDATE events SET payload_json=? WHERE sequence=?", ('{"tampered":true}', 5))
    raw.commit()
    raw.close()
    v1 = store.verify_event_chain()
    assert v1["ok"] is False and v1.get("sequence") == 5 and v1.get("reason") == "event_hash mismatch"
    results["V1"] = {"ok": False, "sequence": 5, "reason": "event_hash mismatch"}

    iso = root / "case14-v2"
    iso.mkdir(exist_ok=False)
    store = EventStore(iso / "db.sqlite3")
    store.initialize()
    fill(store, 40)
    assert store.verify_event_chain()["ok"]
    raw = sqlite3.connect(str(iso / "db.sqlite3"))
    raw.row_factory = sqlite3.Row
    row = dict(raw.execute("SELECT * FROM events WHERE sequence=5").fetchone())
    row["payload_json"] = '{"tampered":true}'
    raw.execute(
        "UPDATE events SET payload_json=?, event_hash=? WHERE sequence=?",
        (row["payload_json"], row_hash(row), 5),
    )
    raw.commit()
    raw.close()
    v2 = store.verify_event_chain()
    assert v2["ok"] is False and v2.get("sequence") == 6 and v2.get("reason") == "prev_hash mismatch"
    results["V2"] = {"ok": False, "sequence": 6, "reason": "prev_hash mismatch"}

    iso = root / "case14-v8"
    iso.mkdir(exist_ok=False)
    store = EventStore(iso / "db.sqlite3")
    store.initialize()
    fill(store, 20)
    assert store.verify_event_chain()["ok"]
    raw = sqlite3.connect(str(iso / "db.sqlite3"))
    raw.execute("UPDATE events SET payload_json=? WHERE sequence=?", ('{"foreign":true}', 7))
    raw.commit()
    raw.close()
    with store.connect(refresh_views=False) as conn:
        # V8 intentionally exercises the transaction-level append after a
        # foreign write; ordinary fixture seeding above uses append_event.
        store._append_event_tx(conn, "t", "bench.tick", {"after": True})
    v8 = store.verify_event_chain()
    assert v8["ok"] is False and v8.get("sequence") == 7 and v8.get("reason") == "event_hash mismatch"
    results["V8"] = {"ok": False, "sequence": 7, "reason": "event_hash mismatch"}

    # Every call must actually read history, including a repeated failed check.
    # Names/comments in source cannot prove this safety property.
    repeated_queries = require_first_failure(store, 7)
    multiple_positions = chain_tamper_checks(root, EventStore)

    out = {
        "ok": True,
        "scene_only": True,
        "cases": results,
        "repeated_history_query_executions": repeated_queries,
        "history_query_scope": "each repeated call executed a measured event-history query; first-failure positions are checked separately",
        "three_position_tampering": multiple_positions,
        "doctor_and_worker_gates": "same full verify_event_chain API",
        "cleanup": "case14 dirs live under scene root; scene cleanup removes them",
    }
    if set(results) != {"V1", "V2", "V8"}:
        raise RuntimeError("STOP: case14 missing V1/V2/V8")
    (bind.O / "51-chain-case14.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"ok": True, "cases": sorted(results)}, ensure_ascii=False), flush=True)
    return out


def main() -> int:
    run_case14(load_bind())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
