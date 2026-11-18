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
REVERSED = 'memory.reuse_reversed'
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


def score_events_tx(conn):
    from .delivery_events import rows
    from .experience_loop import _events
    applications = {a['application_id']: a for a in _events(conn, 'memory.application_recorded')}
    deliveries = {d['event_id']: d for d in _events(conn, 'task.delivery_recorded')}
    legacy_scores = set()
    def validate(kind, value):
        for key in ('source_task_id', 'real_task_id'):
            if not isinstance(value.get(key), str) or not value[key]:
                raise ValueError('invalid ' + key)
        if kind == SCORED:
            if (type(value.get('score')) is not int or value['score'] not in (0, 1)
                    or type(value.get('used_at')) not in (int, float) or not math.isfinite(value['used_at'])):
                raise ValueError('invalid formal reuse score event')
            # Preserve the previous v1 score payload as immutable history.
            # New-format contributions must carry the stronger evidence binding.
            if 'delivery_event_id' not in value and 'application_ids' not in value:
                legacy_scores.add(value.get('_event_id', ''))
                return
            app_ids = value.get('application_ids')
            delivery = deliveries.get(value.get('delivery_event_id'))
            if (not isinstance(app_ids, list) or not app_ids or len(set(app_ids)) != len(app_ids)
                    or not isinstance(value.get('source_sha256'), str) or len(value['source_sha256']) != 64
                    or not delivery or delivery['decision'] != 'accept'
                    or delivery['real_task_id'] != value['real_task_id']
                    or not set(app_ids) <= set(delivery['application_ids'])):
                raise ValueError('score lacks its accepted delivery/application/version binding')
            for app_id in app_ids:
                app = applications.get(app_id)
                if (not app or not app.get('formal_reuse') or app['candidate_id'] != value['source_task_id']
                        or app['candidate_sha256'] != value['source_sha256']
                        or app['real_task']['id'] != value['real_task_id'] or app['artifact'] != delivery['artifact']):
                    raise ValueError('score application binding does not match accepted evidence')
            if 'settlement_key' in value:
                from .experience_workflow import digest
                if value['settlement_key'] != digest([value['real_task_id'], value['source_task_id'], app_ids, value['delivery_event_id']]):
                    raise ValueError('score settlement key mismatch')
        elif (not isinstance(value.get('score_event_id'), str) or
              type(value.get('contribution')) is not int or value['contribution'] not in (0, 1)):
            raise ValueError('invalid formal reuse reversal event')
        else:
            withdrawal = deliveries.get(value.get('delivery_event_id'))
            if (not withdrawal or withdrawal['decision'] != 'withdraw'
                    or withdrawal['real_task_id'] != value['real_task_id']
                    or withdrawal.get('withdraws_delivery_event_id') != value.get('withdraws_delivery_event_id')):
                raise ValueError('reversal lacks its explicit withdrawal binding')
    def checked(kind, value):
        validate(kind, dict(value))
    events, problems = rows(conn, (SCORED, REVERSED), checked)
    scores, active, reversed_ids = {}, {}, set()
    for event in events:
        value = event['value']; pair = (value['real_task_id'], value['source_task_id'])
        try:
            if event['event_type'] == SCORED:
                if pair in active:
                    raise ValueError('duplicate active formal reuse score')
                active[pair] = event['event_id']
                scores[event['event_id']] = event
            else:
                original = scores.get(value['score_event_id'])
                if (not original or value['score_event_id'] in reversed_ids or
                        original['value']['real_task_id'] != pair[0] or
                        original['value']['source_task_id'] != pair[1] or
                        original['value']['score'] != value['contribution'] or
                        original['value']['delivery_event_id'] != value.get('withdraws_delivery_event_id') or
                        active.get(pair) != value['score_event_id']):
                    raise ValueError('reversal does not bind a live score contribution')
                reversed_ids.add(value['score_event_id']); active.pop(pair)
        except (ValueError, TypeError, KeyError) as exc:
            problems.append({'event_id': event['event_id'], 'sequence': event['sequence'],
                             'event_type': event['event_type'], 'reason': str(exc)})
    return scores, active, reversed_ids, [f"{p['event_id']}#{p['sequence']} {p['event_type']}: {p['reason']}" for p in problems]


def projection_tx(conn):
    """Net permanent contributions; malformed rows are isolated and reportable."""
    scores, active, reversed_ids, _ = score_events_tx(conn)
    result = {}
    for event_id, event in scores.items():
        v = event['value']; item = result.setdefault(v['source_task_id'], {'score': 0, 'last_used_at': None})
        if event_id not in reversed_ids:
            item['score'] += v['score']
        item['last_used_at'] = max(item['last_used_at'] or v['used_at'], v['used_at'])
    return result


def snapshot_tx(conn):
    """Read-only monitor contract; net accepted uses and per-event diagnostics."""
    from .store import EventStore
    if not EventStore.verify_event_chain_tx(conn)['ok']:
        return {'ok': False, 'available': False, 'version': 1, 'scores': {}, 'items': [], 'problems': [{'reason': 'event chain is invalid'}]}
    scores, _, reversed_ids, problems = score_events_tx(conn)
    totals = projection_tx(conn)
    for source_id, value in totals.items():
        value['use_count'] = sum(1 for event_id, event in scores.items()
                                 if event['value']['source_task_id'] == source_id and event_id not in reversed_ids)
    return {'ok': not problems, 'available': True, 'version': 1, 'scores': totals,
            'items': [dict(value, source_task_id=source_id, used=value['use_count'])
                      for source_id, value in totals.items()], 'problems': problems}


def ledger_check(store):
    with store.read_only() as conn:
        from .store import EventStore
        chain = EventStore.verify_event_chain_tx(conn)
        if not chain['ok']:
            return {'ok': False, 'source_count': 0, 'problems': ['event chain is invalid']}
        _, _, _, problems = score_events_tx(conn)
        return {'ok': not problems, 'source_count': len(projection_tx(conn)), 'problems': problems}


def _settle_source(store, conn, delivery, delivery_id, applications):
    from .experience_loop import _application_files
    from .store import EventStore
    if not EventStore.verify_event_chain_tx(conn)['ok']:
        raise ValueError('event chain is invalid')
    source_id, task_id = applications[0]['candidate_id'], delivery['real_task_id']
    _, active, _, _ = score_events_tx(conn)
    if (task_id, source_id) in active:
        return
    from .delivery import completed_task_tx
    task = completed_task_tx(conn, task_id, applications[0]['real_task'])
    conclusion = task['result']
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
    from .experience_workflow import digest
    value['settlement_key'] = digest([task_id, source_id, value['application_ids'], delivery_id])
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
    if delivery['decision'] == 'withdraw':
        scores, active, _, _ = score_events_tx(conn)
        for (task_id, source_id), event_id in list(active.items()):
            original = scores[event_id]['value']
            if task_id != delivery['real_task_id']:
                continue
            # Withdrawal only reverses the acceptance selected by the user.
            if original.get('delivery_event_id') != delivery.get('withdraws_delivery_event_id'):
                continue
            store._append_event_tx(conn, source_id, REVERSED, {
                'source_task_id': source_id, 'real_task_id': task_id,
                'score_event_id': event_id, 'contribution': original['score'],
                'delivery_event_id': delivery_id,
                'withdraws_delivery_event_id': delivery['withdraws_delivery_event_id']})
            for row in conn.execute('SELECT event_id,payload_json FROM events WHERE event_type=?', (REVISION,)):
                try: revision = json.loads(row['payload_json'])
                except (ValueError, TypeError): continue
                if isinstance(revision, dict) and revision.get('delivery_event_id') == original.get('delivery_event_id'):
                    store._append_event_tx(conn, source_id, 'memory.formal_withdrawn',
                        {'request_id': row['event_id'], 'delivery_event_id': delivery_id})
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
