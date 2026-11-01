"""Resolve review exclusions from task records, never an idle self-declaration."""
import json

from .pool_dispatch import independence_pair, snapshot_of


def execution_context(store, task, db=None):
    if db is None:
        with store.read_only() as connection:
            return execution_context(store, task, connection)
    snapshot = snapshot_of(task)
    context = snapshot.get('review_context') or {}
    work_id = context.get('work_task_id') or snapshot.get('workflow', {}).get('work_task_id')
    authors, executors, collaborators = set(), set(), set()
    ids = [task['id']]
    ids.extend(snapshot.get('source_task_ids') or [])
    if work_id:
        work = db.execute('SELECT * FROM tasks WHERE id=?', (work_id,)).fetchone()
        if work is None:
            raise ValueError('来源工作任务不存在')
        work = dict(work)
        if snapshot_of(work).get('review_context', {}).get('needs_review', True) and snapshot_of(work).get('workflow', {}).get('phase') != 'work':
            raise ValueError('来源必须是工作任务，不能拿审查任务冒充执行记录')
        ids.append(work_id)
        collaborators.update(snapshot_of(work).get('workflow', {}).get('agents') or [])
    for task_id in ids:
        for event in db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='task.participant_recorded'", (task_id,)):
            record = json.loads(event[0])
            {'author': authors, 'executor': executors, 'collaborator': collaborators}[record['role']].add(record['actor_id'])
        record_task = db.execute('SELECT execution_snapshot_json FROM tasks WHERE id=?', (task_id,)).fetchone()
        if record_task is None:raise ValueError('来源任务记录缺失')
        record_snapshot = snapshot_of({'execution_snapshot_json': record_task[0]})
        if record_snapshot.get('kind') == 'host_authored_candidate' and record_snapshot.get('author'):
            authors.add(record_snapshot['author'])
        work_context = record_snapshot.get('review_context', {})
        if work_context.get('needs_review') is False and work_context.get('host'):
            executors.add(work_context['host']['id'])

        work_phase = snapshot_of({'execution_snapshot_json': record_task[0]}).get('workflow', {}).get('phase') == 'work'
        # Review runs on this task are votes, not work. Every run of the linked
        # work task counts, including failed/partial runs.
        if task_id == work_id or work_phase or record_snapshot.get('needs_review') is False:
            executors.update(r[0] for r in db.execute('SELECT agent_name FROM agent_runs WHERE task_id=?', (task_id,)))
    excluded = authors | executors | collaborators
    bound = db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='task.host_bound' ORDER BY rowid DESC LIMIT 1", (task['id'],)).fetchone()
    host = context.get('host') or (json.loads(bound[0])['host'] if bound else None)
    if host:
        host = dict(host, worked=host['id'] in (executors | collaborators), author=host['id'] in authors,
                    independence=independence_pair({'interface': 'cli', **host}))
    return {'author_ids': sorted(authors), 'executor_ids': sorted(executors),
            'collaborator_ids': sorted(collaborators), 'excluded_ids': sorted(excluded), 'host': host}


def bind_host(store, task_id, host):
    if set(host) != {'id','entry','model'} or any(not isinstance(v,str) or not v.strip() or len(v)>2048 for v in host.values()):
        raise ValueError('需要明确宿主 id、入口和模型；不接受未干活自述')
    from .memory import MemoryBridge
    if MemoryBridge._sensitive_reason(json.dumps(host,ensure_ascii=False)):raise ValueError('宿主身份含敏感资料')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not store.verify_event_chain_tx(db)['ok']:raise ValueError('事件链校验失败')
        task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not task:raise KeyError(task_id)
        if task['status'] not in {'pending','retry'}:raise ValueError('只能给未运行的待处理任务绑定宿主')
        snapshot = snapshot_of(dict(task))
        previous = db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='task.host_bound'",(task_id,)).fetchone()
        declared = snapshot.get('review_context',{}).get('host') or (json.loads(previous[0])['host'] if previous else None)
        if declared and declared != host:raise ValueError('宿主身份已绑定，不能覆盖')
        legacy = snapshot.get('host') or {}
        if legacy and legacy.get('id') != host['id']:raise ValueError('宿主与旧任务记录不符')
        if legacy.get('worked') or legacy.get('author'):raise ValueError('宿主这次干过活或是作者，不能补票')
        if not previous:
            store._append_event_tx(db,task_id,'task.host_bound',{'host':host})


def record_participant(store, task_id, actor_id, role):
    if role not in {'author', 'executor', 'collaborator'} or not isinstance(actor_id, str) or not actor_id.strip() or len(actor_id)>128:
        raise ValueError('需要明确参与者及 author/executor/collaborator 角色')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not store.verify_event_chain_tx(db)['ok']:
            raise ValueError('事件链校验失败')
        if not db.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone():raise KeyError(task_id)
        record = {'actor_id': actor_id, 'role': role}
        if not any(json.loads(r[0]) == record for r in db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='task.participant_recorded'", (task_id,))):
            store._append_event_tx(db, task_id, 'task.participant_recorded', record)
