"""Resolve review exclusions from task records, never an idle self-declaration."""
import json

from .pool_dispatch import independence_pair, snapshot_of


def _related_task_ids(snapshot):
    """Collect direct related ids from one snapshot (no recursion)."""
    related = []
    for item in snapshot.get("source_task_ids") or []:
        if isinstance(item, str) and item.strip():
            related.append(item.strip())
    trial = snapshot.get("experience_trial")
    if isinstance(trial, dict):
        source = trial.get("source_task_id")
        if isinstance(source, str) and source.strip():
            related.append(source.strip())
        for item in trial.get("source_task_ids") or []:
            if isinstance(item, str) and item.strip():
                related.append(item.strip())
        real = trial.get("real_task")
        if isinstance(real, dict):
            real_id = real.get("id")
            if isinstance(real_id, str) and real_id.strip():
                related.append(real_id.strip())
        real_id = trial.get("real_task_id")
        if isinstance(real_id, str) and real_id.strip():
            related.append(real_id.strip())
    real = snapshot.get("real_task")
    if isinstance(real, dict):
        real_id = real.get("id")
        if isinstance(real_id, str) and real_id.strip():
            related.append(real_id.strip())
    real_id = snapshot.get("real_task_id")
    if isinstance(real_id, str) and real_id.strip():
        related.append(real_id.strip())
    return related


def _local_hard_ids(snapshot, task_id, work_id=None):
    """Local source/trial/work links that must resolve to a tasks row.

    External real_task ids are allowed to be absent locally (experience_loop);
    they are soft-missing and never wash out exclusions gathered elsewhere.
    """
    hard = {task_id}
    for item in snapshot.get("source_task_ids") or []:
        if isinstance(item, str) and item.strip():
            hard.add(item.strip())
    trial = snapshot.get("experience_trial")
    if isinstance(trial, dict):
        source = trial.get("source_task_id")
        if isinstance(source, str) and source.strip():
            hard.add(source.strip())
        for item in trial.get("source_task_ids") or []:
            if isinstance(item, str) and item.strip():
                hard.add(item.strip())
    if work_id:
        hard.add(work_id)
    return hard


def _expand_task_ids(db, root_ids, *, hard_ids):
    """BFS expand source/trial/real-task links with cycle protection.

    Missing ids in hard_ids raise at the caller; other missing ids (external
    real_task) stay soft and are not walked as local rows.
    """
    ordered = []
    seen = set()
    missing_hard = []
    missing_soft = []
    hard = set(hard_ids)
    queue = []
    for item in root_ids:
        if isinstance(item, str) and item.strip() and item.strip() not in seen:
            queue.append(item.strip())
    while queue:
        task_id = queue.pop(0)
        if task_id in seen:
            continue
        seen.add(task_id)
        row = db.execute(
            "SELECT execution_snapshot_json FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            if task_id in hard:
                missing_hard.append(task_id)
            else:
                missing_soft.append(task_id)
            continue
        ordered.append(task_id)
        snapshot = snapshot_of({"execution_snapshot_json": row[0]})
        # Nested local source/trial/work links stay hard; external real_task does not.
        hard |= _local_hard_ids(snapshot, task_id)
        for related in _related_task_ids(snapshot):
            if related not in seen:
                queue.append(related)
        # Applications on this candidate may name another real_task.
        for event in db.execute(
            "SELECT payload_json FROM events WHERE task_id=? AND event_type IN "
            "('memory.application_recorded','memory.applied')",
            (task_id,),
        ):
            try:
                payload = json.loads(event[0])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            real = payload.get("real_task")
            if isinstance(real, dict):
                real_id = real.get("id")
                if isinstance(real_id, str) and real_id.strip() and real_id.strip() not in seen:
                    queue.append(real_id.strip())
            real_id = payload.get("real_task_id")
            if isinstance(real_id, str) and real_id.strip() and real_id.strip() not in seen:
                queue.append(real_id.strip())
    return ordered, missing_hard, missing_soft


def execution_context(store, task, db=None):
    if db is None:
        with store.read_only() as connection:
            return execution_context(store, task, connection)
    snapshot = snapshot_of(task)
    context = snapshot.get('review_context') or {}
    work_id = context.get('work_task_id') or snapshot.get('workflow', {}).get('work_task_id')
    authors, executors, collaborators = set(), set(), set()
    seed = [task['id']]
    seed.extend(_related_task_ids(snapshot))
    if work_id:
        seed.append(work_id)
    hard_ids = _local_hard_ids(snapshot, task['id'], work_id)
    ids, missing_hard, missing_soft = _expand_task_ids(db, seed, hard_ids=hard_ids)
    hard_only = [item for item in missing_hard if item != task['id']]
    if hard_only:
        raise ValueError('来源任务记录缺失: ' + ','.join(hard_only))
    missing = [item for item in missing_soft if item != task['id']]
    if work_id:
        work = db.execute('SELECT * FROM tasks WHERE id=?', (work_id,)).fetchone()
        if work is None:
            raise ValueError('来源工作任务不存在')
        work = dict(work)
        if snapshot_of(work).get('review_context', {}).get('needs_review', True) and snapshot_of(work).get('workflow', {}).get('phase') != 'work':
            raise ValueError('来源必须是工作任务，不能拿审查任务冒充执行记录')
        collaborators.update(snapshot_of(work).get('workflow', {}).get('agents') or [])
    for task_id in ids:
        for event in db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='task.participant_recorded'", (task_id,)):
            record = json.loads(event[0])
            {'author': authors, 'executor': executors, 'collaborator': collaborators}[record['role']].add(record['actor_id'])
        record_task = db.execute('SELECT execution_snapshot_json FROM tasks WHERE id=?', (task_id,)).fetchone()
        if record_task is None:
            # Soft-missing external ids are never placed in ordered ids.
            raise ValueError('来源任务记录缺失')
        record_snapshot = snapshot_of({'execution_snapshot_json': record_task[0]})
        if record_snapshot.get('kind') == 'host_authored_candidate' and record_snapshot.get('author'):
            authors.add(record_snapshot['author'])
        work_context = record_snapshot.get('review_context', {})
        if work_context.get('needs_review') is False and work_context.get('host'):
            executors.add(work_context['host']['id'])

        work_phase = snapshot_of({'execution_snapshot_json': record_task[0]}).get('workflow', {}).get('phase') == 'work'
        # Review runs on this task are votes, not work. Every run of the linked
        # work task counts, including failed/partial runs.
        # Validation-shell tasks must still inherit executors from the expanded
        # source/real-task set (work_id / work_phase / non-review contexts).
        if task_id == work_id or work_phase or record_snapshot.get('needs_review') is False:
            executors.update(r[0] for r in db.execute('SELECT agent_name FROM agent_runs WHERE task_id=?', (task_id,)))
        elif record_snapshot.get('experience_trial') or record_snapshot.get('real_task') or record_snapshot.get('real_task_id'):
            # experience_trial / real_task shells: count runs on the source itself
            # only when this id is the original work, not the review shell.
            if task_id != task['id']:
                executors.update(r[0] for r in db.execute('SELECT agent_name FROM agent_runs WHERE task_id=?', (task_id,)))
    excluded = authors | executors | collaborators
    bound = db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='task.host_bound' ORDER BY rowid DESC LIMIT 1", (task['id'],)).fetchone()
    host = context.get('host') or (json.loads(bound[0])['host'] if bound else None)
    if host:
        host = dict(host, worked=host['id'] in (executors | collaborators), author=host['id'] in authors,
                    independence=independence_pair({'interface': 'cli', **host}))
    return {'author_ids': sorted(authors), 'executor_ids': sorted(executors),
            'collaborator_ids': sorted(collaborators), 'excluded_ids': sorted(excluded), 'host': host,
            'related_task_ids': ids, 'missing_related_task_ids': missing}


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
        if task['status'] == 'running':raise ValueError('不能给执行中的任务绑定宿主')
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
