"""Fixture-only SQLite execution meter; never inspects product source text."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit


_REAL_CONNECT = sqlite3.connect
_WINDOW_ACTIVE = False


def _database_path(value, uri=False):
    name = os.fsdecode(value)
    if name in {'', ':memory:'}:
        return None
    if uri and name.startswith('file:'):
        parsed = urlsplit(name)
        if parsed.netloc not in {'', 'localhost'}:
            return None
        name = unquote(parsed.path)
        if name == ':memory:' or 'mode=memory' in parsed.query.split('&'):
            return None
    return Path(name).resolve()


class HistoryExecution:
    def __init__(self, database, reference_types=None, check_write_lock=True):
        self.database = database
        self.reference_types = reference_types or {}
        self.check_write_lock = check_write_lock
        self.connections = 0
        self.transaction_begins = 0
        self.history_queries = 0
        self.observed_types = set()
        self.write_lock_blocked = None
        self._wrappers = {}
        self._window_used = False

    @contextmanager
    def window(self, store=None, method='connect', *, _self_test=False):
        """Instrument this process's sqlite3.connect plus one store factory."""
        global _WINDOW_ACTIVE
        if _WINDOW_ACTIVE:
            raise RuntimeError('STOP: nested SQLite measurement windows are unsupported')
        if not _self_test:
            sensor_self_check()
        self._window_used = True
        target = _database_path(self.database)

        def connect(*args, **kwargs):
            database = args[0] if args else kwargs['database']
            uri = kwargs.get('uri', args[7] if len(args) > 7 else False)
            raw = _REAL_CONNECT(*args, **kwargs)
            return self.connection(raw) if _database_path(database, uri) == target else raw

        _WINDOW_ACTIVE = True
        try:
            with patch.object(sqlite3, 'connect', side_effect=connect):
                if store is None:
                    yield self
                else:
                    original = getattr(store, method)

                    @contextmanager
                    def measured(*args, **kwargs):
                        with original(*args, **kwargs) as db:
                            yield self.connection(db)

                    with patch.object(store, method, side_effect=measured):
                        yield self
        finally:
            _WINDOW_ACTIVE = False

    def connection(self, raw):
        if isinstance(raw, _Connection):
            if raw.meter is not self:
                raise AssertionError('connection already belongs to a different meter')
            return raw
        existing = self._wrappers.get(id(raw))
        if existing is not None:
            return existing
        self.connections += 1
        wrapped = _Connection(raw, self)
        self._wrappers[id(raw)] = wrapped
        return wrapped

    def execute(self, raw, target, sql, parameters=()):
        reads = set()
        begins = []

        def authorize(action, table, column, database, trigger):
            if action == sqlite3.SQLITE_READ and table == 'events':
                reads.add(column)
            if action == sqlite3.SQLITE_TRANSACTION and table == 'BEGIN':
                begins.append(True)
            return sqlite3.SQLITE_OK

        # Reinstalling the authorizer invalidates SQLite's statement cache, so
        # repeated executions of identical SQL are counted, not just prepares.
        raw.set_authorizer(authorize)
        try:
            cursor = target.execute(sql, parameters)
        finally:
            raw.set_authorizer(None)
        self.transaction_begins += len(begins)
        historical = bool(reads & {'payload_json', 'event_type', ''})
        # Metadata projections can also scan the entire event history. Count
        # them when a second row is consumed, without prefetching or parsing
        # SQL; the append path's one-row chain-head lookup remains excluded.
        lock_blocked = (self._check_write_lock() if reads and self.check_write_lock
                        and self.write_lock_blocked is None else None)
        if historical:
            self._record_history(lock_blocked)
        return _Cursor(cursor, raw, self, historical, bool(reads), lock_blocked)

    def _check_write_lock(self):
        # Excluded observer: never uses the patched public factory. Capture at
        # execution time even when metadata classification awaits row two.
        contender = _REAL_CONNECT(self.database, timeout=0)
        try:
            contender.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as exc:
            if getattr(exc, 'sqlite_errorcode', None) != sqlite3.SQLITE_BUSY:
                raise
            return True
        else:
            contender.rollback()
            return False
        finally:
            contender.close()

    def _record_history(self, lock_blocked):
        self.history_queries += 1
        if self.check_write_lock and self.write_lock_blocked is None:
            self.write_lock_blocked = lock_blocked

    def observe(self, cursor, row):
        if row is None:
            return
        # Use seeded semantic identities, not SELECT order or output aliases.
        for value in row:
            if not isinstance(value, str):
                continue
            if value in {'workday.output_recorded', 'workday.reference_acked'}:
                self.observed_types.add(value)
            if value.startswith('{'):
                payload = json.loads(value)
                if isinstance(payload, dict):
                    kind = self.reference_types.get(payload.get('reference_id'))
                    if kind:
                        self.observed_types.add(kind)

    def require_budget(self, maximum=1, require_types=False):
        if self.connections != 1 or self.transaction_begins != 1:
            raise AssertionError(('batch connection/transaction budget', self.result()))
        if not 1 <= self.history_queries <= maximum:
            raise AssertionError(('event history query budget', self.result()))
        if self.write_lock_blocked is not True:
            raise AssertionError(('write lock not held before history read', self.result()))
        if require_types and not {
            'workday.output_recorded', 'workday.reference_acked'
        } <= self.observed_types:
            raise AssertionError(('both reference event types must be consumed', self.result()))

    def result(self):
        return {
            'connections': self.connections,
            'transaction_begins': self.transaction_begins,
            'history_queries': self.history_queries,
            'write_lock_blocked': self.write_lock_blocked,
            'observed_types': sorted(self.observed_types),
            'meter_scope': ('store factory and sqlite3.connect calls to the same canonical database during window'
                            if self._window_used else 'explicitly wrapped connections only; no factory window'),
            'excluded_connections': ['write-lock observer using saved real factory'],
            'coverage_limits': ['pre-bound connect aliases', '_sqlite3.connect and direct Connection constructors',
                                'other processes', 'previously opened raw connections outside the store factory',
                                'explicit raw unwrapping of Python proxy objects',
                                'events reads with fewer than two consumed rows are excluded only without payload_json, event_type, or empty-column read callbacks; '
                                'in verified SQLite/schema fixtures COUNT(*) and INTEGER PRIMARY KEY MAX(sequence) are counted via empty-column callbacks; metadata-only single-row chain-head reads without those callbacks stay excluded',
                                'write-lock observer detects a writer but does not identify the owning connection',
                                'connection and cursor proxies do not preserve sqlite3 Connection/Cursor isinstance identity'],
        }


class _Connection:
    def __init__(self, raw, meter):
        self.raw, self.meter = raw, meter

    def execute(self, sql, parameters=()):
        return self.meter.execute(self.raw, self.raw, sql, parameters)

    def cursor(self, *args, **kwargs):
        return _Cursor(self.raw.cursor(*args, **kwargs), self.raw, self.meter, False)

    def executemany(self, sql, parameters):
        cursor = self.cursor()
        return cursor.executemany(sql, parameters)

    def executescript(self, script):
        raise AssertionError('unmetered executescript is forbidden in this execution probe')

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def __setattr__(self, name, value):
        if name in {'raw', 'meter'}:
            object.__setattr__(self, name, value)
        else:
            setattr(self.raw, name, value)

    def __enter__(self):
        self.raw.__enter__()
        return self

    def __exit__(self, *args):
        return self.raw.__exit__(*args)


def sensor_self_check():
    """A removed direct-connection sensor must make every window fail closed."""
    with tempfile.TemporaryDirectory(prefix='history-sensor-') as location:
        database = Path(location) / 'db.sqlite'
        setup = _REAL_CONNECT(database)
        setup.execute('CREATE TABLE events(event_type TEXT,payload_json TEXT,event_hash TEXT)')
        setup.executemany('INSERT INTO events VALUES(?,?,?)', [('TEST', '{}', str(i)) for i in range(60)])
        setup.commit()
        setup.close()
        meter = HistoryExecution(database, check_write_lock=False)
        with meter.window(_self_test=True):
            for name, uri in ((str(database), False), (database.as_uri(), True)):
                db = sqlite3.connect(name, uri=uri)
                try:
                    db.execute('SELECT event_type,payload_json FROM events').fetchall()
                finally:
                    db.close()
            db = sqlite3.connect(database)
            try:
                for _ in range(50):
                    db.execute('SELECT event_hash FROM events').fetchall()
            finally:
                db.close()
        if meter.connections != 3 or meter.history_queries != 52:
            raise RuntimeError(('STOP: direct SQLite connection sensor self-check failed', meter.result()))
    return {'ok': True, 'direct_connections_observed': 3, 'history_queries_observed': 52,
            'metadata_projection_queries_observed': 50}


def reference_history_checks(root, EventStore, ledger):
    """Two historical ID classes must both suppress replay; new IDs still work."""
    from types import SimpleNamespace

    location = root / 'reference-history-semantics'
    location.mkdir(exist_ok=False)
    store = EventStore(location / 'runtime/db.sqlite')
    store.initialize()
    ledger.configure(store, SimpleNamespace(memory={'obsidian_vault': str(location / 'vault')}))
    product = location / 'product.txt'
    product.write_text('TEST semantic reference history')
    ids = {'TEST-history-output': 'workday.output_recorded',
           'TEST-history-ack': 'workday.reference_acked'}

    def queue(reference_id, status='pending'):
        value = ledger.record_reference(store, product, node='TEST semantic history',
            task_id='TEST-history-task', agent='TEST host', model='TEST no model',
            kind='产出', status=status, reference_id=reference_id)
        if not value.get('ok'):
            raise AssertionError(value)

    for reference_id in ids:
        queue(reference_id)
        ledger._flush_references(store)
    with store.read_only() as db:
        before = [tuple(row) for row in db.execute('SELECT * FROM events ORDER BY sequence')]
    meter = HistoryExecution(store.path, ids)
    # A changed body would be appended if either historical ID class were missed.
    for reference_id in ids:
        queue(reference_id, 'completed')
    with meter.window(store):
        ledger._flush_references(store)
    meter.require_budget(require_types=True)
    with store.read_only() as db:
        after = [tuple(row) for row in db.execute('SELECT * FROM events ORDER BY sequence')]
    if after != before or list((store.path.parent / '.workday-references').glob('*.json')):
        raise AssertionError('historical reference replay was not consumed exactly once')
    queue('TEST-history-new', 'completed')
    ledger._flush_references(store)
    with store.read_only() as db:
        recorded = db.execute('SELECT COUNT(*) FROM events WHERE event_type=?',
                              ('workday.output_recorded',)).fetchone()[0]
    if recorded != 2:
        raise AssertionError('new reference must still produce its output event')
    return {'ok': True, 'execution': meter.result(), 'both_replays_suppressed': True,
            'new_reference_recorded': True}


class _Cursor:
    def __init__(self, raw, connection, meter, historical, events_read=False, lock_blocked=None):
        self.raw, self._connection = raw, connection
        self.meter, self.historical = meter, historical
        self._events_read, self._rows_seen, self._lock_blocked = events_read, 0, lock_blocked

    @property
    def connection(self):
        # sqlite3.Cursor.connection is a public route back to the same physical
        # connection, not a new connection and never an unmetered escape.
        return self.meter.connection(self._connection)

    def execute(self, sql, parameters=()):
        result = self.meter.execute(self._connection, self.raw, sql, parameters)
        self.raw, self.historical = result.raw, result.historical
        self._events_read, self._rows_seen = result._events_read, result._rows_seen
        self._lock_blocked = result._lock_blocked
        return self

    def executemany(self, sql, parameters):
        cursor = self
        for values in parameters:
            cursor = cursor.execute(sql, values)
        return cursor

    def fetchone(self):
        absent = object()
        row = next(self.raw, absent)
        if row is absent:
            return None
        self._observe_row(row)
        return row

    def _observe_row(self, row):
        self._rows_seen += 1
        if self._events_read and not self.historical and self._rows_seen == 2:
            self.historical = True
            self.meter._record_history(self._lock_blocked)
        if self.historical:
            self.meter.observe(self.raw, row)

    def fetchall(self):
        return list(self)

    def fetchmany(self, size=None):
        rows = self.raw.fetchmany() if size is None else self.raw.fetchmany(size)
        for row in rows:
            self._observe_row(row)
        return rows

    def __iter__(self):
        return self

    def __next__(self):
        row = next(self.raw)
        self._observe_row(row)
        return row

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def __setattr__(self, name, value):
        if name in {'raw', '_connection', 'meter', 'historical', '_events_read', '_rows_seen', '_lock_blocked'}:
            object.__setattr__(self, name, value)
        else:
            setattr(self.raw, name, value)
