"""Card 68: events indexes, formal_review rewrite, full-chain anti-regression (V1/V2/V8).

Standalone: sets sys.path so this file can run without riding test_card59.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CANDIDATE = Path(__file__).resolve().parents[1]
_INGRESS = REPO / "development" / "review-ingress"
_MONITOR = REPO / "architecture-package-20261027" / "shared" / "工具" / "小八监看"
for _path in (CANDIDATE / "src", _INGRESS, _MONITOR):
    text = str(_path)
    if text not in sys.path:
        sys.path.insert(0, text)

from xiaoba_agentos.formal_review import PREFIX, REVISION, _events  # noqa: E402
from xiaoba_agentos.store import EventStore  # noqa: E402


def _fill(store: EventStore, n: int, *, event_type: str = "bench.tick") -> None:
    with store.connect(refresh_views=False) as conn:
        for i in range(n):
            store._append_event_tx(conn, f"task-{i % 17}", event_type, {"i": i})


def _row_hash(row) -> str:
    material = (
        f"{row['prev_hash']}|{row['event_id']}|{row['task_id'] or ''}|"
        f"{row['event_type']}|{row['payload_json']}|{row['created_at']:.6f}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class EventsIndexTests(unittest.TestCase):
    def test_indexes_exist_after_initialize(self):
        with tempfile.TemporaryDirectory() as root:
            store = EventStore(Path(root) / "db.sqlite3")
            store.initialize()
            with store.connect(refresh_views=False) as conn:
                names = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'"
                    )
                }
            self.assertIn("idx_events_event_type_seq", names)
            self.assertIn("idx_events_task_id_type_seq", names)

    def test_explain_uses_event_type_index(self):
        with tempfile.TemporaryDirectory() as root:
            store = EventStore(Path(root) / "db.sqlite3")
            store.initialize()
            _fill(store, 200)
            with store.connect(refresh_views=False) as conn:
                plan = " ".join(
                    str(row[-1])
                    for row in conn.execute(
                        "EXPLAIN QUERY PLAN "
                        "SELECT * FROM events WHERE event_type=? ORDER BY sequence",
                        ("bench.tick",),
                    )
                ).lower()
            self.assertIn("idx_events_event_type_seq", plan)
            self.assertNotIn("scan events", plan)

    def test_formal_events_query_uses_index_range(self):
        with tempfile.TemporaryDirectory() as root:
            store = EventStore(Path(root) / "db.sqlite3")
            store.initialize()
            with store.connect(refresh_views=False) as conn:
                store._append_event_tx(conn, None, REVISION, {"k": 1})
                store._append_event_tx(conn, None, PREFIX + "enabled", {"enabled_at": 1})
                store._append_event_tx(conn, None, "other.noise", {"n": 1})
                plan = " ".join(
                    str(row[-1])
                    for row in conn.execute(
                        "EXPLAIN QUERY PLAN "
                        "SELECT * FROM ("
                        "SELECT * FROM events WHERE event_type=?"
                        " UNION ALL "
                        "SELECT * FROM events WHERE event_type >= ? AND event_type < ?"
                        ") ORDER BY sequence",
                        (REVISION, PREFIX, "memory.formal`"),
                    )
                ).lower()
                rows = _events(conn)
            self.assertIn("idx_events_event_type_seq", plan)
            kinds = {r["event_type"] for r in rows}
            self.assertEqual({REVISION, PREFIX + "enabled"}, kinds)


class FullChainAntiRegressionTests(unittest.TestCase):
    """V1/V2/V8: middle-history rewrites must fail full verify (card 68 review).

    These guard against re-introducing an incremental cache that only checks
    genesis/checkpoint and therefore misses interior row rewrites.
    """

    def _open_filled(self, root: str, n: int = 40):
        path = Path(root) / "db.sqlite3"
        store = EventStore(path)
        store.initialize()
        _fill(store, n)
        ok = store.verify_event_chain()
        self.assertTrue(ok["ok"])
        self.assertEqual(n, ok["events"])
        return store, path

    def test_V1_middle_payload_rewrite_without_hash_update_is_detected(self):
        with tempfile.TemporaryDirectory() as root:
            store, path = self._open_filled(root)
            raw = sqlite3.connect(str(path))
            raw.row_factory = sqlite3.Row
            try:
                raw.execute(
                    "UPDATE events SET payload_json=? WHERE sequence=?",
                    ('{"tampered":true}', 5),
                )
                raw.commit()
            finally:
                raw.close()
            result = store.verify_event_chain()
            self.assertFalse(result["ok"])
            self.assertEqual(5, result.get("sequence"))
            self.assertEqual("event_hash mismatch", result.get("reason"))

    def test_V2_middle_payload_and_self_hash_rewrite_breaks_next_prev(self):
        with tempfile.TemporaryDirectory() as root:
            store, path = self._open_filled(root)
            raw = sqlite3.connect(str(path))
            raw.row_factory = sqlite3.Row
            try:
                row = dict(raw.execute("SELECT * FROM events WHERE sequence=5").fetchone())
                row["payload_json"] = '{"tampered":true}'
                new_hash = _row_hash(row)
                raw.execute(
                    "UPDATE events SET payload_json=?, event_hash=? WHERE sequence=?",
                    (row["payload_json"], new_hash, 5),
                )
                raw.commit()
            finally:
                raw.close()
            result = store.verify_event_chain()
            self.assertFalse(result["ok"])
            self.assertEqual(6, result.get("sequence"))
            self.assertEqual("prev_hash mismatch", result.get("reason"))

    def test_V8_foreign_middle_rewrite_still_detected_after_local_append(self):
        with tempfile.TemporaryDirectory() as root:
            store, path = self._open_filled(root, n=20)
            raw = sqlite3.connect(str(path))
            raw.row_factory = sqlite3.Row
            try:
                raw.execute(
                    "UPDATE events SET payload_json=? WHERE sequence=?",
                    ('{"foreign":true}', 7),
                )
                raw.commit()
            finally:
                raw.close()
            with store.connect(refresh_views=False) as conn:
                store._append_event_tx(conn, "t", "bench.tick", {"after": True})
            result = store.verify_event_chain()
            self.assertFalse(result["ok"])
            self.assertEqual(7, result.get("sequence"))
            self.assertEqual("event_hash mismatch", result.get("reason"))


class FormalReviewSemanticsNoteTests(unittest.TestCase):
    def test_strict_prefix_excludes_like_wildcard_lookalikes(self):
        """New range query intentionally drops LIKE '_' / case-insensitive hits."""
        with tempfile.TemporaryDirectory() as root:
            store = EventStore(Path(root) / "db.sqlite3")
            store.initialize()
            with store.connect(refresh_views=False) as conn:
                store._append_event_tx(conn, None, PREFIX + "enabled", {"enabled_at": 1})
                for kind in (
                    "memory.formalXenabled",
                    "memory.formal-enabled",
                    "Memory.Formal_Enabled",
                ):
                    store._append_event_tx(conn, None, kind, {"noise": True})
                rows = _events(conn)
            kinds = {r["event_type"] for r in rows}
            self.assertEqual({PREFIX + "enabled"}, kinds)


if __name__ == "__main__":
    unittest.main()
