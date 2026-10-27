"""A database sidecar file that vanishes mid-check must not fail the caller.

Two agents of one task run in parallel threads.  When one thread's last
connection closes, SQLite deletes agentos.sqlite3-shm / -wal; the other thread
may be just about to check that file's permissions.  Before v1.6.5 the second
stat() raised FileNotFoundError and the agent run failed as an unknown internal
fault (seen as an intermittent `internal_unknown_candidate` in v1.6.4 tests).

The first test below reproduces that ordering deterministically instead of
making every sidecar stat() fail.  Failing every stat() hides the regression:
the pre-v1.6.5 code guarded with `candidate.exists()`, and Path.exists() swallows
FileNotFoundError and returns False, so the old code skipped the file and the
test passed on both implementations.  Here the file is really present for the
first stat() and really gone for every later one, which is what the race does.
The old code still fails on both interpreters but at different lines, because
Path.exists() routes through Path.stat on 3.9 and not on 3.14: the missing file
surfaces at the second stat() on 3.9, and at os.chmod() on 3.14.
"""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaoba_agentos.store import EventStore


class SidecarRaceTest(unittest.TestCase):
    def test_a_sidecar_that_vanishes_during_the_permission_check_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "agentos.sqlite3")
            store.initialize()
            sidecar = Path(str(store.path) + "-shm")
            sidecar.write_bytes(b"")
            os.chmod(sidecar, 0o644)
            real_stat = Path.stat
            removed_after_first_stat = []

            def vanishing(self, *args, **kwargs):
                result = real_stat(self, *args, **kwargs)
                if self == sidecar and not removed_after_first_stat:
                    removed_after_first_stat.append(self)
                    os.unlink(self)
                return result

            with patch.object(Path, "stat", vanishing):
                store._secure_permissions()  # must not raise

            self.assertEqual([sidecar], removed_after_first_stat)
            self.assertFalse(sidecar.exists())

    def test_a_present_sidecar_is_still_tightened_to_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "agentos.sqlite3")
            store.initialize()
            sidecar = Path(str(store.path) + "-wal")
            sidecar.write_bytes(b"")
            os.chmod(sidecar, 0o644)
            store._secure_permissions()
            self.assertEqual(0o600, sidecar.stat().st_mode & 0o777)

    def test_parallel_connections_never_fail_on_a_vanishing_sidecar(self):
        # Smoke test only.  Whether SQLite happens to delete a sidecar while
        # another thread is inside _secure_permissions depends on scheduling,
        # so a pass here is not evidence about either implementation; the
        # deterministic test above is what distinguishes them.
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "agentos.sqlite3")
            store.initialize()
            errors = []

            def worker():
                try:
                    for _ in range(60):
                        with store.connect() as conn:
                            conn.execute("SELECT COUNT(*) FROM events").fetchone()
                except Exception as exc:  # noqa: BLE001 - the test collects every failure
                    errors.append(repr(exc))

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
