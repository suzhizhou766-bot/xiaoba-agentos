"""Formal experience promotion, projected solely from transactional SQLite events.

No schema, candidate-state or human-review writes. Revision requests are events
for card52b; they are deliberately not existing new-candidate review objects.
"""
import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path


SCORED = 'memory.reuse_scored'
SKIPPED = 'memory.reuse_scoring_skipped'
REVISION = 'memory.reuse_revision_requested'
MAX_FORMAL_SOURCE_BYTES = 1_000_000  # Same byte limit as MemoryBridge._verified_source.


def is_formal(candidate):
    """Includes historical quarantined/retired publications; never resurrect them."""
    return bool(candidate and (candidate.get('published_path') or candidate.get('published_at')
                               or candidate.get('status') == 'published'))


def source_version(conn, application):
    row = conn.execute('SELECT * FROM memory_candidates WHERE task_id=?', (application['candidate_id'],)).fetchone()
    source = dict(row) if row else {}
    if source.get('status') != 'published' or not source.get('published_path'):
        raise ValueError('formal source is not currently published')
    path = Path(source['published_path'])
    if path.is_symlink() or not path.is_file():
        raise ValueError('formal source must be a regular file')
    if path.stat().st_size > MAX_FORMAL_SOURCE_BYTES:
        raise ValueError(f'formal source exceeds {MAX_FORMAL_SOURCE_BYTES}-byte limit (same as retrieval)')
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if application['candidate_sha256'] != sha:
        raise ValueError('formal source version changed')
    return source, sha


def projection_tx(conn):
    """Permanent totals and last *scored* use; no wall-clock week or Markdown scan."""
    result, seen = {}, set()
    for row in conn.execute('SELECT payload_json FROM events WHERE event_type=? ORDER BY sequence', (SCORED,)):
        value = json.loads(row[0])
        source, task = value['source_task_id'], value['real_task_id']
        key = (task, source)
        score, used = value['score'], value['used_at']
        if (not isinstance(source, str) or not isinstance(task, str) or not source or not task
                or type(score) is not int or score not in (0, 1)
                or type(used) not in (int, float) or not math.isfinite(used)):
            raise ValueError('invalid formal reuse score event')
        if key in seen:
            raise ValueError('duplicate formal reuse score event')
        seen.add(key)
        item = result.setdefault(source, {'score': 0, 'last_used_at': None})
        item['score'] += score
        item['last_used_at'] = max(item['last_used_at'] or used, used)
    return result


def ledger_check(store):
    """Report the global projection breaker without writes or schema changes."""
    try:
        with store.read_only() as conn:
            scores = projection_tx(conn)
        return {'ok': True, 'source_count': len(scores), 'problems': []}
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        return {'ok': False, 'problems': [type(exc).__name__ + ': ' + str(exc)[:240]]}


def _settle_source(store, conn, delivery, delivery_id, applications):
    from .experience_loop import _application_files
    from .store import EventStore
    if not EventStore.verify_event_chain_tx(conn)['ok']:
        raise ValueError('event chain is invalid')
    projection_tx(conn)  # Malformed historical scoring must not create another score.
    source_id, task_id = applications[0]['candidate_id'], delivery['real_task_id']
    for row in conn.execute('SELECT payload_json FROM events WHERE event_type=?', (SCORED,)):
        old = json.loads(row[0])
        if (old['real_task_id'], old['source_task_id']) == (task_id, source_id):
            return
    task = conn.execute('SELECT result FROM tasks WHERE id=?', (task_id,)).fetchone()
    conclusion = str(task['result'] or '') if task else ''
    assessments = []
    for application in applications:
        if not application.get('formal_reuse'):
            raise ValueError('application was not registered as formal reuse')
        source, sha = source_version(conn, application)
        if not any(identifier and identifier in conclusion for identifier in
                   (source_id, Path(source['published_path']).stem)):
            raise ValueError('task conclusion does not cite this formal source')
        _application_files(application)
        assessment = application.get('reuse_assessment')
        if not isinstance(assessment, dict) or assessment.get('outcome') not in {'unchanged', 'modified', 'inapplicable'}:
            raise ValueError('explicit reuse_assessment outcome is missing')
        if any(not isinstance(assessment.get(k), str) for k in ('changed', 'added', 'discarded')):
            raise ValueError('reuse_assessment requires changed/added/discarded text')
        if assessment['outcome'] == 'unchanged' and any(assessment[k].strip() for k in ('changed', 'added', 'discarded')):
            raise ValueError('unchanged assessment conflicts with reported changes')
        if assessment['outcome'] != 'unchanged' and not any(assessment[k].strip() for k in ('changed', 'added', 'discarded')):
            raise ValueError('revision assessment requires an explanation')
        assessments.append(assessment)
    score = int(all(a['outcome'] == 'unchanged' for a in assessments))
    value = {'source_task_id': source_id, 'source_sha256': sha, 'real_task_id': task_id,
             'application_ids': [a['application_id'] for a in applications],
             'delivery_event_id': delivery_id, 'score': score, 'used_at': time.time()}
    store._append_event_tx(conn, source_id, SCORED, value)
    if not score:
        # Preserve every assessment when a task applied one experience multiple times.
        revised = [a for a in applications if a['reuse_assessment']['outcome'] != 'unchanged']
        revision_id = store._append_event_tx(conn, source_id, REVISION, dict(value,
            reuse_assessment=revised[-1]['reuse_assessment'],
            applications=[{'application_id': a['application_id'], 'application_location': a['application_location'],
                           'steps_applied': a['steps_applied'], 'deviations': a['deviations'],
                           'reuse_assessment': a['reuse_assessment']} for a in revised]))
        draft = revised[-1]['reuse_assessment'].get('draft')
        if draft is not None:
            from .formal_review import _draft, PREFIX
            _draft(draft)
            store._append_event_tx(conn, source_id, PREFIX+'draft_submitted', {'request_id':revision_id,'draft':draft})


def settle_delivery_tx(store, conn, delivery, delivery_id, applications):
    """Per-source savepoints: scoring failure cannot undo a valid delivery."""
    if delivery['decision'] != 'accept':
        return
    grouped = {}
    for app in applications:
        grouped.setdefault(app['candidate_id'], []).append(app)
    for source_id, apps in grouped.items():
        conn.execute('SAVEPOINT formal_reuse_score')
        try:
            _settle_source(store, conn, delivery, delivery_id, apps)
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
            conn.execute('ROLLBACK TO formal_reuse_score')
            conn.execute('RELEASE formal_reuse_score')
            store._append_event_tx(conn, source_id, SKIPPED, {
                'source_task_id': source_id, 'real_task_id': delivery['real_task_id'],
                'application_ids': [a['application_id'] for a in apps], 'delivery_event_id': delivery_id,
                'reason': type(exc).__name__ + ': ' + str(exc)[:240]})
        else:
            conn.execute('RELEASE formal_reuse_score')
