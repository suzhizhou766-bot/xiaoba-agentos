"""Explicit user delivery over existing events and the review submission journal.

The CLI flag is an authorization declaration, not proof of a human identity.
HTTP capabilities are minted only after the local Origin/CSRF boundary. Neither
JSON origin=user nor a GET request can mint one. No candidate approval is made.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .experience_workflow import digest


class UserDecision:
    def __init__(self, value, channel, seal):
        self.value_sha256 = digest(value)
        self.channel = channel
        self.seal = seal


_SEAL = object()


def user_decision(value, *, channel, explicit):
    if explicit is not True or channel not in {'cli_user_declaration', 'local_csrf_post'}:
        raise ValueError('explicit user decision is required; origin=user is not authorization')
    return UserDecision(value, channel, _SEAL)


def require_decision(value, authority):
    if not isinstance(authority, UserDecision) or authority.seal is not _SEAL or authority.value_sha256 != digest(value):
        raise ValueError('explicit user decision is required; origin=user is not authorization')


def _answer(text):
    from .memory import MemoryBridge
    from .output_hygiene import contains_non_answer_markers, detect_reasoning_leak
    if (not isinstance(text, str) or not text.strip() or len(text.encode()) > 1_000_000
            or contains_non_answer_markers(text) or detect_reasoning_leak(text)
            or MemoryBridge._sensitive_reason(text)):
        raise ValueError('task result failed B19 or sensitive-output gate')
    return text


def _ref_shape(ref, workspace):
    import re
    from .experience_loop import _text
    ws = Path(workspace)
    if not ws.is_absolute() or ws.is_symlink() or ws.resolve() != ws or not ws.is_dir():
        raise ValueError('workspace must be an absolute canonical directory')
    if not isinstance(ref, dict) or not re.fullmatch(r'[0-9a-f]{64}', str(ref.get('sha256', ''))):
        raise ValueError('file reference requires SHA-256')
    path = Path(_text(ref.get('path'), 'path'))
    if (not path.is_absolute() or '..' in path.parts or path.is_symlink()
            or not path.resolve(strict=False).is_relative_to(ws) or path.resolve(strict=False) == ws):
        raise ValueError('evidence must be a regular file inside the authorized task workspace')


def _read(ref, workspace):
    from .experience_loop import _file
    _file(ref, Path(workspace))
    path = Path(ref['path'])
    if path.stat().st_size > 1_000_000:
        raise ValueError('task result exceeds 1000000 bytes')
    text = path.read_text(encoding='utf-8')
    if hashlib.sha256(text.encode()).hexdigest() != ref['sha256']:
        raise ValueError('task result changed while reading')
    return _answer(text)


def _direct_rows(conn, *, task_id=None):
    from .delivery_events import rows
    def validate(kind, value):
        if (value.get('version') != 1 or not isinstance(value.get('real_task'), dict)
                or not isinstance(value['real_task'].get('id'), str)
                or not isinstance(value.get('artifact'), dict)
                or not isinstance(value['artifact'].get('version'), str)
                or not value['artifact']['version'] or not isinstance(value.get('result'), dict)):
            raise ValueError('incomplete direct completion')
    def relevant(row, value):
        if row['task_id'] == task_id:
            return True
        real = value.get('real_task') if isinstance(value, dict) else None
        return isinstance(real, dict) and real.get('id') == task_id

    valid, problems = rows(conn, ('task.direct_completed', 'task.delivery_materials_registered'),
                           validate, relevant=relevant if task_id is not None else None)
    if task_id is not None and problems:
        raise ValueError('related direct completion history is invalid: ' + problems[0]['event_id'])
    if task_id is not None and any(
        row['value']['real_task']['id'] != task_id or row['task_id'] not in (None, task_id)
        for row in valid
    ):
        raise ValueError('related direct completion history has conflicting task IDs')
    return valid


def register_direct(store, value):
    from .experience_loop import _chain, _safe, _text, _file
    if not isinstance(value, dict) or set(value) != {'version', 'real_task', 'artifact', 'result'} or value['version'] != 1:
        raise ValueError('direct completion requires version, real_task, artifact and result')
    _safe(value)
    real = value['real_task']
    for key in ('id', 'workspace', 'authorization_ref', 'description'):
        _text(real.get(key), 'real_task.' + key)
    workspace = Path(real['workspace'])
    if not workspace.is_absolute() or workspace.resolve() != workspace or not workspace.is_dir():
        raise ValueError('direct completion requires an absolute authorized workspace')
    _text(value['artifact'].get('version'), 'artifact.version')
    _file(value['artifact'], workspace)
    _read(value['result'], workspace)
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE'); _chain(conn)
        native = conn.execute('SELECT 1 FROM tasks WHERE id=?', (real['id'],)).fetchone()
        if native:
            completed = completed_task_tx(conn, real['id'])
            if completed['result_sha256'] != value['result']['sha256']:
                raise ValueError('registered result differs from native completion')
        prior = _direct_rows(conn, task_id=real['id'])
        identical = next((e for e in prior if e['value'] == value), None)
        if identical:
            return dict(value, event_id=identical['event_id'], idempotent=True)
        if any(e['value']['artifact']['version'] == value['artifact']['version'] for e in prior):
            raise ValueError('a direct product version cannot change its evidence')
        event_id = store._append_event_tx(conn, None, 'task.delivery_materials_registered' if native else 'task.direct_completed', value)
    return dict(value, event_id=event_id, completion_basis='host_attested_verified_files')


def completed_task_tx(conn, task_id, real_task=None, artifact=None, verify_artifact_bytes=True):
    """Return verified completion, never infer it from a client status field."""
    row = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if row:
        from .workflow import verified_snapshot
        task = dict(row); snapshot = verified_snapshot(task)
        if task['status'] != 'completed' or snapshot.get('task_purpose') != 'user_task' or task['execution_mode'] == 'host_authored':
            raise ValueError('delivery requires a completed ordinary user task')
        result = _answer(task.get('result'))
        sha = hashlib.sha256(result.encode()).hexdigest()
        from .delivery_events import rows
        completions, _ = rows(conn, ('task.completed',))
        matching = [e for e in completions if e['task_id'] == task_id and e['value'].get('result_sha256') == sha]
        if not matching or json.loads(task.get('provenance_json') or '{}').get('result_sha256') != sha:
            raise ValueError('native completion result does not match its event and provenance')
        return {'real_task_id': task_id, 'result': result, 'result_sha256': sha,
                'completion_event_id': matching[-1]['event_id'], 'completion_kind': 'native'}
    direct = _direct_rows(conn, task_id=task_id)
    if not direct:
        raise ValueError('direct task has no registered completion evidence')
    if verify_artifact_bytes:
        event = direct[-1]
        value = event['value']
        if artifact is not None and artifact != value['artifact']:
            raise ValueError('direct completion product version changed')
    else:
        if artifact is not None:
            matching = [e for e in direct if e['value']['artifact'] == artifact]
            if not matching:
                raise ValueError('direct completion product version changed')
            event = matching[-1]
        else:
            event = direct[-1]
        value = event['value']
    if real_task and any(real_task.get(k) != value['real_task'].get(k) for k in ('workspace', 'authorization_ref')):
        raise ValueError('direct completion authorization or workspace changed')
    from .experience_loop import _file
    ws = Path(value['real_task']['workspace'])
    if verify_artifact_bytes:
        _file(value['artifact'], ws)
        result = _read(value['result'], ws)
    else:
        _ref_shape(value['artifact'], ws)
        _ref_shape(value['result'], ws)
        try:
            result = _read(value['result'], ws)
        except (ValueError, OSError):
            result = (
                f"已登记结果文件在交付后发生本地变更（原冻结 SHA-256：{value['result']['sha256']}），"
                "本次仅执行已接受版本的查询或撤回验收。"
            )
    return {'real_task_id': task_id, 'real_task': value['real_task'], 'artifact': value['artifact'],
            'result': result, 'result_sha256': value['result']['sha256'],
            'completion_event_id': event['event_id'], 'completion_kind': 'direct'}


def detail_tx(conn, task_id, artifact=None):
    from .experience_loop import _events, _application_files, _chain, _file
    _chain(conn)
    applications = [v for v in _events(conn, 'memory.application_recorded') if v['real_task']['id'] == task_id]
    direct = [e for e in _direct_rows(conn) if e['value']['real_task']['id'] == task_id]
    history = [d for d in _events(conn, 'task.delivery_recorded') if d['real_task_id'] == task_id]
    latest = history[-1] if history else None
    latest_candidate_artifact = (
        direct[-1]['value']['artifact'] if direct
        else (applications[-1]['artifact'] if applications else None)
    )
    default_query = artifact is None
    if isinstance(artifact, str):
        ver = artifact.strip()
        candidates = (
            [e['value']['artifact'] for e in direct if e['value']['artifact'].get('version') == ver]
            + [a['artifact'] for a in applications if a['artifact'].get('version') == ver]
            + ([latest['artifact']] if latest and isinstance(latest.get('artifact'), dict) and latest['artifact'].get('version') == ver else [])
        )
        if not candidates:
            raise ValueError('unknown task product version: ' + ver)
        artifact = candidates[-1]
    elif artifact is None:
        artifact = latest_candidate_artifact
    if not isinstance(artifact, dict):
        raise ValueError('select an explicit task product version')
    apps = [a for a in applications if a['artifact'] == artifact]
    real = apps[-1]['real_task'] if apps else None
    currently_accepted = bool(latest and latest['decision'] == 'accept' and latest['artifact'] == artifact)
    completion = completed_task_tx(conn, task_id, real, artifact, verify_artifact_bytes=not currently_accepted)
    if not real:
        matching_direct = [e for e in direct if e['value']['artifact'] == artifact]
        real = completion.get('real_task') or (
            matching_direct[-1]['value']['real_task'] if matching_direct
            else (direct[-1]['value']['real_task'] if direct else None)
        )
    if not real:
        raise ValueError('native task without applications requires explicit registered workspace evidence')
    files_intact = True
    try:
        for app in apps:
            _application_files(app)
        _file(artifact, Path(real['workspace']))
    except (ValueError, OSError):
        if not currently_accepted:
            raise
        _ref_shape(artifact, Path(real['workspace']))
        files_intact = False
    superseded_by_newer = bool(latest_candidate_artifact is not None and latest_candidate_artifact != artifact)
    expected = {'version': 1, 'completion_event_id': completion['completion_event_id'],
                'result_sha256': completion['result_sha256'], 'artifact': artifact,
                'application_ids': sorted(a['application_id'] for a in apps),
                'latest_delivery_event_id': latest['event_id'] if latest else None}
    allowed = (['accept', 'withdraw'] if (currently_accepted and files_intact and not superseded_by_newer)
               else ['withdraw'] if currently_accepted else ['accept'])
    out = {'task_id': task_id, 'real_task': real, 'artifact': artifact, 'applications': apps,
           'expected': expected, 'expected_sha256': digest(expected),
           'result': completion['result'], 'latest_delivery': latest,
           'allowed_decisions': allowed}
    if default_query and latest and latest['decision'] == 'accept' and latest.get('artifact') != artifact:
        try:
            out['accepted_version'] = detail_tx(conn, task_id, latest['artifact'])
        except (ValueError, KeyError, OSError):
            pass
    return out


def pending_tasks(store):
    from .experience_loop import _events, _chain
    with store.read_only() as conn:
        _chain(conn)
        apps = _events(conn, 'memory.application_recorded')
        directs = _direct_rows(conn)
        deliveries = _events(conn, 'task.delivery_recorded')
        ids = {a['real_task']['id'] for a in apps}
        ids.update(e['value']['real_task']['id'] for e in directs)
        items, problems = [], []
        for task_id in sorted(ids):
            task_history = [d for d in deliveries if d['real_task_id'] == task_id]
            latest = task_history[-1] if task_history else None
            task_directs = [e for e in directs if e['value']['real_task']['id'] == task_id]
            task_apps = [a for a in apps if a['real_task']['id'] == task_id]
            latest_candidate = (
                task_directs[-1]['value']['artifact'] if task_directs
                else (task_apps[-1]['artifact'] if task_apps else None)
            )
            targets = []
            if latest and latest.get('decision') == 'accept' and isinstance(latest.get('artifact'), dict):
                targets.append(latest['artifact'])
            if isinstance(latest_candidate, dict) and latest_candidate not in targets:
                targets.append(latest_candidate)
            if not targets:
                targets = [None]
            for target_art in targets:
                try:
                    detail = detail_tx(conn, task_id, target_art)
                    items.append({k: detail[k] for k in ('task_id', 'artifact', 'expected', 'expected_sha256', 'allowed_decisions')})
                except (ValueError, KeyError, OSError) as exc:
                    problems.append({'task_id': task_id, 'reason': str(exc)})
        return {'version': 1, 'items': items, 'problems': problems}


@contextlib.contextmanager
def _journal(config):
    path = config.runtime_dir / 'review-center' / 'submissions.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('delivery journal must not be a symlink')
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute('CREATE TABLE IF NOT EXISTS submissions (id TEXT PRIMARY KEY, digest TEXT NOT NULL, request TEXT NOT NULL, result TEXT, created REAL NOT NULL)')
        os.chmod(path, 0o600)
        with conn:
            yield conn
    finally:
        conn.close()


def submit(config, store, request, *, authority):
    """A durable, replayable single user action; only failed work is retried."""
    from .experience_loop import _safe, _file, record_delivery
    require_decision(request, authority)
    if (not isinstance(request, dict) or request.get('version') != 1 or
            request.get('decision') not in {'accept', 'withdraw'} or
            request.get('user_selected') is not True or
            not isinstance(request.get('expected'), dict)):
        raise ValueError('explicit single delivery selection and frozen expected version are required')
    _safe(request)
    for key in ('message_id', 'source_ref', 'task_id'):
        if not isinstance(request.get(key), str) or not request[key].strip():
            raise ValueError('missing ' + key)
    # Store only valid local references before touching the damaged/healthy chain.
    if not isinstance(request.get('real_task'), dict) or not isinstance(request['real_task'].get('workspace'), str):
        raise ValueError('real_task workspace evidence required')
    workspace = Path(request['real_task']['workspace'])
    if not workspace.is_absolute() or workspace.is_symlink() or workspace.resolve() != workspace or not workspace.is_dir():
        raise ValueError('real_task workspace must be an absolute canonical directory')
    if request['decision'] == 'accept':
        _file(request['artifact'], workspace)
    else:
        _ref_shape(request['artifact'], workspace)
    delivery_ref = request.get('delivery_record')
    if delivery_ref is None:
        msg_id = request['message_id']
        if ('/' in msg_id or '\\' in msg_id or '\x00' in msg_id
                or msg_id in {'.', '..'} or msg_id.startswith('.')
                or Path(msg_id).is_absolute()):
            raise ValueError('invalid delivery message_id')
        folder = workspace / '.xiaoba-delivery'
        if folder.exists() and (folder.is_symlink() or not folder.is_dir()):
            raise ValueError('delivery evidence folder is not a regular directory')
        folder.mkdir(mode=0o700, exist_ok=True)
        if folder.is_symlink() or folder.resolve() != folder:
            raise ValueError('delivery evidence folder may not be redirected')
        path = folder / (msg_id + '.json')
        if path.parent != folder or path.is_symlink() or path.resolve(strict=False).parent != folder:
            raise ValueError('delivery evidence path must stay inside workspace')
        evidence = {'version': 1, 'task_id': request['task_id'], 'decision': request['decision'],
                    'expected_sha256': digest(request['expected']), 'artifact_sha256': request['artifact']['sha256'],
                    'application_ids': request['expected'].get('application_ids', []),
                    'message_id': request['message_id'], 'source_ref': request['source_ref']}
        raw = (json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != raw:
                raise ValueError('delivery evidence id already has different content')
        else:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        delivery_ref = {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}
    _file(delivery_ref, workspace)
    journal_id = 'delivery:' + request['message_id']; request_hash = digest(request)
    with _journal(config) as journal:
        old = journal.execute('SELECT digest,result FROM submissions WHERE id=?', (journal_id,)).fetchone()
        if old and old[0] != request_hash:
            raise ValueError('delivery message identity already has different input')
        if old and old[1]:
            prior = json.loads(old[1])
            if prior.get('ok'):
                return dict(prior, replayed=True)
        if not old:
            journal.execute('INSERT INTO submissions VALUES(?,?,?,?,?)',
                            (journal_id, request_hash, json.dumps(request, ensure_ascii=False), None, time.time()))
    delivery_attempted = False
    delivery_recorded = False
    try:
        from .experience_loop import delivery_history, _chain
        with store.read_only() as conn:
            _chain(conn)
            history = delivery_history(conn, request['task_id'], request['message_id'])
            previous = next((d for d in history if d['message_id'] == request['message_id']), None)
            current = None if previous else detail_tx(conn, request['task_id'], request['artifact'])
        if current and request['expected'] != current['expected']:
            raise ValueError('task/application/product version changed; refresh before deciding')
        if current and (request['real_task'] != current['real_task'] or request['decision'] not in current['allowed_decisions']):
            raise ValueError('task identity changed or decision is no longer available')
        value = {'origin': 'user', 'user_message': '交付' if request['decision'] == 'accept' else '撤回验收',
                 'message_id': request['message_id'], 'source_ref': request['source_ref'],
                 'real_task_id': request['task_id'], 'artifact': request['artifact'],
                 'real_task': request['real_task'], 'delivery_record': delivery_ref,
                 'decision': request['decision'], 'expected': request['expected']}
        delivery_attempted = True
        receipt = record_delivery(store, value, authority=user_decision(value, channel=authority.channel, explicit=True))
        delivery_recorded = True
        # Replaying an accepted message never re-applies its score after withdrawal.
        archive = archive_delivery(config, store, receipt)
        response = {'ok': True, 'receipt': receipt, 'archive': archive,
                    'experience_approval': 'separate_user_review_required'}
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
        pending = delivery_recorded or (
            delivery_attempted and not isinstance(exc, (ValueError, KeyError, TypeError))
        )
        response = {'ok': False, 'pending': pending, 'message_id': request['message_id'], 'error': str(exc)}
    with _journal(config) as journal:
        journal.execute('UPDATE submissions SET result=? WHERE id=?', (json.dumps(response, ensure_ascii=False), journal_id))
    return response


def legacy_request(store, value):
    """Bind the chat message to the server's currently completed product."""
    from .experience_loop import _text
    msg_clean = str(value.get('user_message', '')).strip().rstrip('。！!')
    decision = value.get('decision') or ('withdraw' if msg_clean == '撤回验收' else 'accept')
    expected_message = '交付' if decision == 'accept' else '撤回验收' if decision == 'withdraw' else ''
    if msg_clean != expected_message or not expected_message:
        raise ValueError('explicit delivery/withdrawal chat message required')
    with store.read_only() as conn:
        detail = detail_tx(conn, value['real_task_id'], value['artifact'])
    for key in ('message_id', 'source_ref'):
        _text(value.get(key), key)
    return {'version': 1, 'task_id': value['real_task_id'], 'real_task': detail['real_task'],
            'artifact': value['artifact'], 'expected': value.get('expected', detail['expected']),
            'decision': decision, 'user_selected': True, 'message_id': value['message_id'],
            'source_ref': value['source_ref'], 'delivery_record': value.get('delivery_record')}


def archive_delivery(config, store, receipt):
    """Deterministic per-event artifact; replay resumes safely after a file write."""
    from .experience_loop import _chain
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE'); _chain(conn)
        completed = completed_task_tx(
            conn, receipt['real_task_id'], receipt.get('real_task'), receipt['artifact'],
            verify_artifact_bytes=(receipt.get('decision') == 'accept'),
        )
        body = _answer(completed['result'])
        root = Path(config.memory['obsidian_vault']).resolve().parent / 'Playbook' / '交付'
        root.mkdir(parents=True, exist_ok=True)
        target = root / (receipt['event_id'] + '.md')
        content = ('# 任务交付记录\n\n' + json.dumps({k: receipt.get(k) for k in
                   ('real_task_id', 'artifact', 'application_ids', 'decision', 'source_ref', 'message_id')},
                   ensure_ascii=False, indent=2) + '\n\n' + body + '\n')
        if target.is_symlink() or (target.exists() and target.read_text() != content):
            raise ValueError('delivery archive path conflicts with recorded content')
        if not target.exists():
            with target.open('x', encoding='utf-8') as handle:
                handle.write(content)
            os.chmod(target, 0o600)
        sha = hashlib.sha256(content.encode()).hexdigest()
        found = conn.execute("SELECT 1 FROM events WHERE event_type='task.delivery_archived' AND task_id=?", (receipt['event_id'],)).fetchone()
        if not found:
            store._append_event_tx(conn, receipt['event_id'], 'task.delivery_archived',
                                   {'delivery_event_id': receipt['event_id'], 'path': str(target), 'sha256': sha})
        return {'path': str(target), 'sha256': sha}
