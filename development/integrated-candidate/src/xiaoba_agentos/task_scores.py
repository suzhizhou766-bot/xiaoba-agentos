"""Objective task reliability, shared by host/Worker settlement and pool selection.

Only native, frozen pool participation is scored here. The ledger and its audit
event use the caller's transaction; no delivery message or model call is needed.
"""
from datetime import date, datetime, timedelta
import hashlib
import json
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')
PURPOSES = {'review': 'review', 'work': 'collaborate', 'collaboration': 'collaborate',
            'collaborate': 'collaborate'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def week_of(day):
    value = date.fromisoformat(day)
    monday = value - timedelta(days=value.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def exists(db, table):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def ensure_table(db):
    db.execute('''CREATE TABLE IF NOT EXISTS ri_task_assessments (
        id TEXT PRIMARY KEY, data TEXT NOT NULL, revision INTEGER NOT NULL,
        assessment_id TEXT NOT NULL, updated_at REAL NOT NULL)''')


def capture_pool(db, node_id, purpose):
    """Freeze the configured model and performed role, never infer from a later edit."""
    role = PURPOSES.get(purpose)
    if not role or not exists(db, 'ri_sources'):
        return None
    row = db.execute('SELECT data FROM ri_sources WHERE id=?', (node_id,)).fetchone()
    if not row:
        return None
    source = json.loads(row[0]); node = source.get('node')
    if not isinstance(node, dict):
        return None  # Historical external identities are not pool participants.
    return {'node_id': node_id, 'source_ref': source['source_ref'], 'model': node.get('model'), 'role': role}


def requires_review_vote(db, task, snapshot):
    """Use the host settlement context and adapters' release receipt contract.

    A review-purpose run or needs_review alone also occurs in ordinary multi
    adjudication, where no structured verdict is required.
    """
    from .pool_dispatch import needs_review, task_host
    task = dict(task)
    provenance = json.loads(task['provenance_json'] or '{}')
    if provenance.get('kind') == 'host_review':
        return True
    if needs_review(task) and (snapshot.get('review_context', {}).get('host') or task_host(task)
            or db.execute("SELECT 1 FROM events WHERE task_id=? AND event_type='task.host_bound'",
                          (task['id'],)).fetchone()):
        return True
    # The CLI adapters require this exact pair in the frozen task prompt before
    # enforcing FINAL_ACCEPTANCE; build_release subsequently validates the votes.
    prompt = task['prompt']
    return 'FINAL_ACCEPTANCE: PASS' in prompt and 'FINAL_ACCEPTANCE: FAIL' in prompt


def accepted(run, role, *, require_review_vote=False):
    from .output_hygiene import contains_non_answer_markers, detect_reasoning_leak
    body = run['output']
    if (run['status'] != 'completed' or not isinstance(body, str) or not body.strip()
            or contains_non_answer_markers(body) or detect_reasoning_leak(body)):
        return False
    if role == 'review' and require_review_vote:
        from .pool_dispatch import review_vote_problem
        return not review_vote_problem(body)
    return True


def settlement_tx(db, task_id, completed, now):
    from .store import EventStore
    task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if not task or task['execution_mode'] != 'production':
        return
    if task['status'] != ('completed' if completed else 'failed'):
        return  # An event alone is never a settlement.
    snapshot = json.loads(task['execution_snapshot_json'] or '{}')
    if snapshot.get('health_check') or snapshot.get('test_run'):
        return
    runs = {r['id']: dict(r) for r in db.execute('SELECT * FROM agent_runs WHERE task_id=?', (task_id,))}
    participants = {}
    for event in db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='agent.started' ORDER BY sequence", (task_id,)):
        payload = json.loads(event[0]); pool = payload.get('execution', {}).get('pool_score')
        if not pool or pool.get('role') not in {'review', 'collaborate'} or payload.get('run_id') not in runs:
            continue
        # A retried node has one task sample, using its final accepted attempt.
        participants[(pool['node_id'], pool['role'])] = (pool, runs[payload['run_id']])
    if not participants:
        return

    # 1. 记分前提：事件链校验（整个结算事务最多全量校验一次）
    chain_ok = EventStore.verify_event_chain_tx(db)['ok']
    if not chain_ok:
        for (node_id, role), (pool, run) in participants.items():
            EventStore._append_event_tx(db, task_id, 'task_scoring.skipped', {
                'task_id': task_id, 'node_id': node_id, 'role': role, 'reason': 'chain_invalid'
            })
        return

    ensure_table(db)
    # 2. 记分前提：账本一致性核验（共用同一个 chain_ok，不重复校验事件链）。
    # 键集合不一致只把不一致的那几个 key 当脏行隔离，不再全局停记分：其余任务
    # 照常记分；doctor 仍会报警（audit_ledger_tx 的 problems 保留告警）。
    audit = audit_ledger_tx(db, chain_ok=chain_ok)

    finishes = {}
    for event in db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='agent.finished' ORDER BY sequence", (task_id,)):
        value = json.loads(event[0]); finishes[value['run_id']] = value

    require_review_vote = requires_review_vote(db, task, snapshot)
    day = datetime.fromtimestamp(now, ZONE).date().isoformat()
    for (node_id, role), (pool, run) in participants.items():
        key = digest([task_id, node_id, role])
        # Historical manual ledgers used a different physical key. They still
        # represent the same task/node/role sample, never a second reward.
        existing = db.execute("SELECT id, data FROM ri_task_assessments WHERE id=? OR (json_extract(data,'$.task_id')=? AND json_extract(data,'$.node_id')=? AND json_extract(data,'$.role')=?)",
                              (key, task_id, node_id, role)).fetchone()
        if existing:
            # valid_rows 现带真实表主键 id：旧手动账本的物理键也能正确识别，
            # 合法旧行不再误记 skipped，也不会记第二份分。
            if existing['id'] not in {r['id'] for r in audit['valid_rows']}:
                EventStore._append_event_tx(db, task_id, 'task_scoring.skipped', {
                    'task_id': task_id, 'node_id': node_id, 'role': role, 'reason': 'ledger_inconsistent'
                })
            continue

        receipt_sha = digest({'output': run['output'], 'status': run['status']})
        # 3. 记分前提：节点回执与接收事件核对
        if run['id'] in finishes and finishes[run['id']].get('receipt_sha256') != receipt_sha:
            EventStore._append_event_tx(db, task_id, 'task_scoring.skipped', {
                'task_id': task_id, 'node_id': node_id, 'role': role, 'reason': 'receipt_mismatch'
            })
            continue

        qualified = accepted(run, role, require_review_vote=require_review_vote)
        qualified = qualified and run['id'] in finishes
        good = completed and qualified
        value = dict(pool, task_id=task_id, task_type=snapshot.get('experience_policy', {}).get('task_type') or
                     ('collaboration' if role == 'collaborate' else 'review'),
                     date=day, completed_date=day, week_start=week_of(day)[0],
                     outcome='good' if good else 'not_good', points=int(good),
                     tasks=1, success_rate=float(good),
                     receipt_accepted=qualified, task_completed=bool(completed), run_id=run['id'],
                     receipt_sha256=receipt_sha,
                     evidence_refs=['agent-run:' + run['id']], receipt_digests=[],
                     evaluator='automatic_task_settlement', policy='weekly-reliability-v2')
        assessment_id = 'settlement:' + key
        db.execute('INSERT INTO ri_task_assessments VALUES (?,?,?,?,?)', (key, canonical(value), 1, assessment_id, now))
        EventStore._append_event_tx(db, task_id, 'task_scoring.assessed',
            {'assessment_id': assessment_id, 'key': key, 'revision': 1,
             'previous': None, 'current': value, 'delta': value['points']})


def audit_ledger_tx(db, chain_ok=None):
    """Audit the assessment ledger against event chain and audit events."""
    from .store import EventStore
    if chain_ok is None:
        chain_ok = EventStore.verify_event_chain_tx(db)['ok']
    if not chain_ok:
        return {'ok': False, 'reason': 'chain_invalid', 'valid_rows': [], 'dirty_rows': [], 'problems': ['事件链无效']}
    if not exists(db, 'ri_task_assessments'):
        return {'ok': True, 'reason': None, 'valid_rows': [], 'dirty_rows': [], 'problems': []}
    stored = list(db.execute('SELECT * FROM ri_task_assessments'))
    latest = {}
    for event in db.execute("SELECT payload_json FROM events WHERE event_type='task_scoring.assessed' ORDER BY sequence"):
        value = json.loads(event[0]); latest[value['key']] = value

    problems = []
    stored_ids = {r['id'] for r in stored}
    latest_ids = set(latest.keys())
    if stored_ids != latest_ids:
        problems.append(f'评分表键集合与审计事件不一致: stored={len(stored_ids)}, events={len(latest_ids)}')

    valid_rows = []
    dirty_rows = []
    for row in stored:
        try:
            value = json.loads(row['data'])
            if row['id'] not in latest:
                dirty_rows.append((row, value, '缺少审计事件'))
                continue
            event = latest[row['id']]
            if row['revision'] != event['revision'] or value != event['current'] or row['assessment_id'] != event['assessment_id']:
                dirty_rows.append((row, value, '内容与最新审计事件不一致'))
                continue
            if value.get('run_id'):
                run = db.execute('SELECT output,status FROM agent_runs WHERE id=? AND task_id=?',
                                 (value['run_id'], value['task_id'])).fetchone()
                if not run or digest(dict(run)) != value.get('receipt_sha256'):
                    dirty_rows.append((row, value, '历史评分的任务证据已失效'))
                    continue
            # 带上真实表主键，供 settlement_tx 判定旧手动账本的物理键是否合法。
            valid_rows.append(dict(value, id=row['id']))
        except Exception as e:
            dirty_rows.append((row, None, str(e)))

    ledger_ok = (len(problems) == 0 and len(dirty_rows) == 0)
    return {
        'ok': ledger_ok,
        'reason': 'ledger_inconsistent' if not ledger_ok else None,
        'valid_rows': valid_rows,
        'dirty_rows': dirty_rows,
        'problems': problems + [f"dirty row {d[0]['id']}: {d[2]}" for d in dirty_rows]
    }


def rows_tx(db, chain_ok=None, raise_on_inconsistent=False):
    """Return valid scoring rows, skipping any dirty historical rows unless raise_on_inconsistent is True."""
    audit = audit_ledger_tx(db, chain_ok=chain_ok)
    if raise_on_inconsistent and not audit['ok']:
        if audit['reason'] == 'chain_invalid':
            raise ValueError('事件链无效，不能使用评分择优')
        if any('评分表键集合与审计事件不一致' in p for p in audit['problems']):
            raise ValueError('评分表与审计事件不一致')
        if any(d[2] == '内容与最新审计事件不一致' for d in audit['dirty_rows']):
            raise ValueError('评分表内容与最新审计事件不一致')
        if any(d[2] == '历史评分的任务证据已失效' for d in audit['dirty_rows']):
            raise ValueError('历史评分的任务证据已失效')
        raise ValueError(audit['problems'][0] if audit['problems'] else '评分表与审计事件不一致')
    return audit['valid_rows']


def ledger_check(store_or_db):
    """Read-only health check for doctor."""
    if hasattr(store_or_db, 'read_only'):
        with store_or_db.read_only() as db:
            return audit_ledger_tx(db)
    return audit_ledger_tx(store_or_db)


def weekly(rows, day, task_type=None):
    start, end = week_of(day); groups = {}
    for value in rows:
        if value['week_start'] != start or (task_type and value['task_type'] != task_type):
            continue
        role = value.get('role') or ''
        bucket = groups.setdefault((role, value['source_ref'], value['model']),
            {'role': role, 'source_ref': value['source_ref'], 'model': value['model'], 'node_ids': [],
             'points': 0, 'tasks': 0, 'good_tasks': 0, 'not_good_tasks': 0, 'task_types': {}})
        if value['node_id'] not in bucket['node_ids']: bucket['node_ids'].append(value['node_id'])
        bucket['points'] += value['points']; bucket['tasks'] += 1
        bucket['good_tasks' if value['points'] else 'not_good_tasks'] += 1
        bucket['task_types'][value['task_type']] = bucket['task_types'].get(value['task_type'], 0) + 1
    ranking = list(groups.values())
    for value in ranking: value['success_rate'] = value['good_tasks'] / value['tasks']
    ranking.sort(key=lambda v: (-v['points'], -v['success_rate'], -v['tasks'], v['role'], v['source_ref'], v['model'] or ''))
    return {'week_start': start, 'week_end': end, 'timezone': 'Asia/Shanghai', 'task_type': task_type,
            'policy': 'accepted receipt and task completed +1; otherwise 0; once per task/node/role',
            'ranking': ranking, 'lifetime_score': False, 'resource_inputs_collected': False,
            'selection_requires_current_authorization_and_health': True}


def decorate_nodes(db, nodes, day=None, role='review', manual_inputs=None):
    """No writes or dispatches on a read. Four weeks = this natural week + prior 3."""
    day = day or datetime.now(ZONE).date().isoformat()
    start = week_of(day)[0]
    first = (date.fromisoformat(start) - timedelta(weeks=3)).isoformat()
    last = {}
    for event in db.execute("SELECT event_type,payload_json,created_at FROM events WHERE event_type IN ('agent.started','pool.node_dispatched') ORDER BY sequence"):
        payload = json.loads(event['payload_json'])
        pool = payload.get('execution', {}).get('pool_score') if event['event_type'] == 'agent.started' else payload
        if pool and pool.get('role') == role:
            last[(pool['node_id'], pool.get('model'))] = event['created_at']

    audit = audit_ledger_tx(db)
    # 仅事件链无效时清空全部分数降级；键集合不一致只丢掉不一致的那几行，
    # 其余周分照常参与择优，不清空全部周分。
    if not audit['ok'] and audit['reason'] == 'chain_invalid':
        rows = []
    else:
        valid_rows = []
        for value in audit['valid_rows']:
            if value.get('run_id') or value['week_start'] != start or not value.get('receipt_digests'):
                valid_rows.append(value)
                continue
            # Preserve the legacy manual ledger's work-log evidence gate for the
            # Worker too, without constructing an adapter or launching a process.
            try:
                if manual_inputs is not None:
                    facts = manual_inputs(value['node_id'], value['date'])
                else:
                    from xiaoba_review.agent_workspace import AgentWorkspace
                    binding = db.execute("SELECT payload_json FROM events WHERE event_type='agent_pool.workspace_bound' ORDER BY sequence DESC LIMIT 1").fetchone()
                    source = db.execute('SELECT data FROM ri_sources WHERE id=?', (value['node_id'],)).fetchone()
                    if not binding or not source:
                        continue
                    workspace = AgentWorkspace(json.loads(binding[0])['root'], json.loads(source[0]))
                    facts = workspace.workday_inputs(value['date'])
                evidence = {v['receipt_sha256'] for v in facts if v['task_id'] == value['task_id'] and not v['health_check']}
                if not set(value['receipt_digests']).issubset(evidence):
                    continue
                valid_rows.append(value)
            except Exception:
                continue
        rows = valid_rows

    ranking = weekly(rows, day)['ranking'] if rows else []
    for node in nodes:
        identity = (node.get('source_ref'), node.get('model'))
        match = next((v for v in ranking if (v['source_ref'], v['model']) == identity and v['role'] == role), None)
        node['weekly_score'] = match or {'role': role, 'points': 0, 'tasks': 0, 'success_rate': 0.0}
        node['recent_role_samples'] = sum(1 for v in rows if v.get('role') == role and
            (v['source_ref'], v['model']) == identity and first <= v['week_start'] <= start)
        node['last_role_dispatch'] = last.get((node['node_id'], node.get('model')), 0)
    return nodes


def choose_pair(nodes):
    """Highest same-role weekly score first; second seat explores <3 recent samples."""
    if not nodes: return []
    def score(n):
        value = n.get('weekly_score') or {}
        return (-value.get('points', 0), -value.get('success_rate', 0), -value.get('tasks', 0), str(n['node_id']))
    ranked = sorted(nodes, key=score); first = ranked[0]
    remaining = [n for n in ranked[1:] if tuple(n.get('independence') or ()) != tuple(first.get('independence') or ())]
    if not remaining: return [first]
    low = [n for n in remaining if n.get('recent_role_samples', 0) < 3]
    second = min(low, key=lambda n: (n.get('last_role_dispatch', 0), score(n))) if low else remaining[0]
    return [first, second]
