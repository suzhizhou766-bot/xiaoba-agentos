"""Disposable workday reading views. SQLite/events are the only record.

No model calls, schema changes, product copies, or scoring writes. A single
filesystem lock protects independent hosts; files publish by atomic rename.
"""
from collections import Counter
from datetime import date, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')
KINDS = ('回执', '新经验', '复用经验', '产出')
TERMINAL = {'completed', 'failed', 'cancelled', 'abandoned', 'done', 'execution_completed', 'published'}


def local_checks(store):
    """Inspect enabled state, current spec/runner binding and entry; no probes."""
    from .pool_dispatch import load_nodes, _entry_present
    nodes=[]
    for node in load_nodes(store):
        nodes.append({k:node.get(k) for k in ('node_id','enabled','removed','model','interface','readiness')})
        nodes[-1]['entry_exists']=_entry_present(node.get('entry'))
    return {'model_tasks_sent':0,'live_probe_sent':False,'remote_inference_tested':False,
            'nodes':nodes,'scope':'local configuration, entry and test qualification; no model call'}


def bound_ledger(store):
    root = getattr(store, 'workday_root', None)
    if root is None:
        with store.read_only() as db:
            row=db.execute("SELECT payload_json FROM events WHERE event_type='workday.root_bound' ORDER BY sequence DESC LIMIT 1").fetchone()
        if row:root=json.loads(row[0])['root']
    return WorkdayLedger(store, root) if root else None


def bind_root(store, root):
    root=str(Path(root).resolve())
    with store.connect() as db:
        row=db.execute("SELECT payload_json FROM events WHERE event_type='workday.root_bound' ORDER BY sequence DESC LIMIT 1").fetchone()
        if row and json.loads(row[0])['root'] != root:
            raise ValueError('workday root binding changed; user decision required')
        if not row:store._append_event_tx(db,None,'workday.root_bound',{'root':root})
    store.workday_root=Path(root)
    return WorkdayLedger(store,root)


def record_reference(store, path, **metadata):
    """Persist receipts before rendering; unchanged current versions are no-ops."""
    _register_reference(store,path,**metadata)
    # connect() already attempts automatic refresh after commit. A disposable
    # view error must not invalidate the original adapter receipt.


def _register_reference(store, path, *, node, task_id, agent, model, kind, status):
    path=Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError('original output must be a regular absolute file')
    if kind not in KINDS or not all(isinstance(v,str) and v.strip() for v in (node,agent,model,status)):
        raise ValueError('explicit node, agent, model, kind and status required')
    if task_id is not None and (not isinstance(task_id,str) or not task_id.strip()):
        raise ValueError('task ID must be nonempty or null')
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):raise ValueError('original output must be a regular file')
        before=os.fstat(handle.fileno());sha=hashlib.file_digest(handle,'sha256').hexdigest();after=os.fstat(handle.fileno())
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise ValueError('original output changed while hashing; retry registration')
    ref=dict(path=str(path),node=node,task_id=task_id,agent=agent,model=model,kind=kind,status=status,sha256=sha)
    from .memory import MemoryBridge
    if MemoryBridge._sensitive_reason(canonical(ref)):raise ValueError('sensitive output metadata refused')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not store.verify_event_chain_tx(db)['ok']:raise ValueError('workday requires a valid event chain')
        old=db.execute("SELECT payload_json,created_at FROM events WHERE event_type='workday.output_recorded' AND task_id IS ? AND json_extract(payload_json,'$.path')=? AND json_extract(payload_json,'$.node')=? AND json_extract(payload_json,'$.kind')=? ORDER BY sequence DESC LIMIT 1",(task_id,str(path),node,kind)).fetchone()
        if not old or json.loads(old[0])!=ref or day_of(old[1])!=day_of(time.time()):
            store._append_event_tx(db,task_id,'workday.output_recorded',ref)


def refresh_after_write(store, after_sequence=0):
    # A reading view failure must not turn committed task settlement into an
    # apparent rollback. The durable record survives and can be rebuilt manually.
    try:
        with store.read_only() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone():return
            relevant=db.execute("SELECT 1 FROM events WHERE sequence>? AND (event_type IN ('workday.output_recorded','agent_pool.tmux_job','agent.finished','task.delivery_recorded','task.completed','task.failed','task.cancelled','task.abandoned') OR event_type LIKE 'memory.%') LIMIT 1",(after_sequence,)).fetchone()
        if not relevant:return
        ledger=bound_ledger(store)
        if ledger:ledger.rebuild()
    except (OSError, ValueError) as exc:
        import warnings
        warnings.warn('workday view needs rebuild: '+type(exc).__name__,RuntimeWarning)


def day_of(timestamp):
    return datetime.fromtimestamp(timestamp, ZONE).date().isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _directory(path):
    # Refuse symlinked destinations, including an existing parent component.
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('workday destination must not contain symlinks')
    path.mkdir(parents=True, exist_ok=True)


def _write(path, text):
    _directory(path.parent)
    if path.is_symlink():
        raise ValueError('workday view must not be a symlink')
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.workday-', delete=False) as handle:
        handle.write(text); handle.flush(); os.fsync(handle.fileno())
        temporary = handle.name
    os.replace(temporary, path)


class WorkdayLedger:
    def __init__(self, store, assistant_root):
        self.store = store
        self.root = Path(assistant_root).resolve()
        self.catalog = self.root/'工具/每日台账索引总目录'

    def record(self, path, *, node, task_id, agent, model, kind, status):
        """Register an original output in the existing event chain, then render."""
        _register_reference(self.store,path,node=node,task_id=task_id,agent=agent,model=model,kind=kind,status=status)
        return self.rebuild(day_of(time.time()))

    def _snapshot(self):
        with self.store.read_only() as db:
            db.execute('BEGIN')
            if not self.store.verify_event_chain_tx(db)['ok']:
                raise ValueError('workday requires a valid event chain')
            events = [dict(row) for row in db.execute('SELECT * FROM events ORDER BY sequence')]
            tasks = {r['id']:dict(r) for r in db.execute('SELECT * FROM tasks')}
            candidates = [dict(r) for r in db.execute('SELECT * FROM memory_candidates ORDER BY created_at,task_id')]
            reuse = [dict(r) for r in db.execute('SELECT * FROM memory_reuse ORDER BY created_at,id')]
        rows = []
        event_order={e['event_id']:e['sequence'] for e in events}
        def add(value, timestamp, identity):
            rows.append(dict(value, time=timestamp, date=day_of(timestamp), record_id=identity,
                             sequence=event_order.get(identity.split(':')[0],0)))
        def identity(task_id, sequence, node=None, run_id=None):
            agents=[];models=[]
            for e in events:
                if e['event_type']!='agent.started' or e['task_id']!=task_id or e['sequence']>sequence:continue
                v=json.loads(e['payload_json'])
                if node is not None and v.get('agent')!=node:continue
                if run_id is not None and v.get('run_id')!=run_id:continue
                pool=(v.get('execution') or {}).get('pool_score') or {}
                frozen=v.get('workday_identity') or {}
                agents.append(frozen.get('agent') or v.get('agent','未记录'))
                models.append(frozen.get('model') or pool.get('model') or '未记录')
            if not agents:return self._identity(tasks.get(task_id),[])
            return ', '.join(dict.fromkeys(agents)), ', '.join(dict.fromkeys(models))
        tmux={};seen_cards=set();seen_receipts=set()
        for e in events:
            if e['event_type'] == 'workday.output_recorded':
                add(json.loads(e['payload_json']), e['created_at'], e['event_id'])
            elif e['event_type']=='agent.finished':
                v=json.loads(e['payload_json'])
                if v.get('receipt_path') and v.get('receipt_file_sha256'):
                    agent,model=identity(e['task_id'],e['sequence'],v['agent'],v['run_id'])
                    add(dict(path=v['receipt_path'],sha256=v['receipt_file_sha256'],node=v['agent'],task_id=e['task_id'],
                             agent=agent,model=model,kind='回执',status=v['status']),e['created_at'],e['event_id'])
            elif e['event_type']=='agent_pool.tmux_job':
                v=json.loads(e['payload_json']);tmux[v['job_id']]=v
                ticket=v.get('ticket') or {};node=(v.get('source') or {}).get('node') or {}
                base=dict(node=v.get('node_id'),task_id=ticket.get('task_id'),agent=node.get('agent','未记录'),model=node.get('model','未记录'))
                if v['job_id'] not in seen_cards and ticket.get('card') and ticket.get('card_sha256'):
                    add(dict(base,path=ticket['card'],sha256=ticket['card_sha256'],kind='产出',status=v['status']),e['created_at'],e['event_id']+':card')
                    seen_cards.add(v['job_id'])
                key=(v['job_id'],v.get('execution_receipt_sha256'))
                if v.get('execution_receipt') and key[1] and key not in seen_receipts:
                    add(dict(base,path=v['execution_receipt'],sha256=key[1],kind='回执',status=v['status']),e['created_at'],e['event_id']+':receipt')
                    seen_receipts.add(key)
            elif e['event_type'] in {'memory.application_recorded','task.delivery_recorded'}:
                v=json.loads(e['payload_json']);task_id=v.get('real_task_id') or (v.get('real_task') or {}).get('id')
                agent,model=identity(task_id,e['sequence'])
                for key,kind in (('artifact','产出'),('delivery_record','回执')):
                    ref=v.get(key)
                    if isinstance(ref,dict) and ref.get('path') and ref.get('sha256'):
                        add(dict(path=ref['path'],sha256=ref['sha256'],node='delivery',task_id=task_id,
                                 agent=agent,model=model,kind=kind,status=v.get('decision','recorded')),
                            e['created_at'],e['event_id']+':'+key)
        # Existing durable references are projected without opening originals.
        # Missing/cleaned products do not erase their registered evidence hashes.
        for c in candidates:
            stages=[e for e in events if e['task_id']==c['task_id'] and e['event_type']=='memory.candidate_staged']
            for stage in stages:
                payload=json.loads(stage['payload_json'])
                add(dict(path=payload.get('staged_path',c['staged_path']),sha256=payload.get('evidence_sha256',c['evidence_sha256']),node='memory',task_id=c['task_id'],
                     agent=identity(c['task_id'],stage['sequence'])[0],
                     model=identity(c['task_id'],stage['sequence'])[1],kind='新经验',status='pending_delivery_review'),
                    stage['created_at'],stage['event_id'])
        for r in reuse:
            evidence=[e for e in events if e['event_type']=='memory.reused' and e['task_id']==r['task_id']
                      and json.loads(e['payload_json']).get('source_task_id')==r['source_task_id']]
            event=evidence[0] if evidence else None
            sha=json.loads(event['payload_json']).get('source_sha256') if event else None
            who=identity(r['task_id'],event['sequence'] if event else 0)
            add(dict(path=r['source_path'],sha256=sha,hash_state='recorded' if sha else '历史记录未保存文件哈希',
                     node='memory',task_id=r['task_id'],agent=who[0],model=who[1],kind='复用经验',status='pending'),
                r['created_at'],event['event_id'] if event else 'reuse:'+r['id'])
        native_ids=set(tasks)
        latest={}
        for r in sorted(rows,key=lambda r:(r['sequence'],r['time'],r['record_id'])):
            tid=r.get('task_id')
            if tid and tid not in native_ids:
                original=latest.get(tid)
                latest[tid]=dict(id=tid,status=r['status'],created_at=original['created_at'] if original else r['time'])
        tasks.update(latest)
        grouped={}
        for job in tmux.values():
            tid=(job.get('ticket') or {}).get('task_id')
            if tid and tid not in native_ids:grouped.setdefault(tid,[]).append(job)
        for tid,jobs in grouped.items():
            pending=[j for j in jobs if j['status'] not in TERMINAL]
            state=(pending or jobs)[-1]['status']
            tasks[tid]=dict(id=tid,status=state,created_at=min(j['created_at'] for j in jobs))
        for c in candidates:
            if c['status'] in {'pending_delivery_review','delivery_approved','revision_requested','pending_final_review','publication_failed','revalidation_required'}:
                tasks[c['task_id']]=dict(tasks[c['task_id']],status='经验:'+c['status'])
        return rows, tasks, events

    @staticmethod
    def _identity(task, runs):
        if not task:return '未记录','未记录'
        snapshot=json.loads(task.get('execution_snapshot_json') or '{}')
        selected=[r['agent_name'] for r in runs if r['task_id']==task['id']]
        host=(snapshot.get('review_context') or {}).get('host') or {}
        provenance=json.loads(task.get('provenance_json') or '{}')
        if not isinstance(host,dict):host={}
        return ', '.join(dict.fromkeys(selected)) or host.get('id') or provenance.get('author','未记录'), host.get('model','未记录')

    def rebuild(self, day=None, *, checks=None):
        day = date.fromisoformat(day or day_of(time.time())).isoformat()
        with self.store.read_only() as db:
            if not self.store.verify_event_chain_tx(db)['ok']:
                raise ValueError('workday requires a valid event chain')
        _directory(self.catalog)
        lock=self.catalog/'.workday.lock'
        if lock.is_symlink():raise ValueError('workday lock must not be a symlink')
        with lock.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            with self.store.connect(refresh_views=False) as db:
                db.execute('BEGIN IMMEDIATE')
                if not self.store.verify_event_chain_tx(db)['ok']:
                    raise ValueError('workday requires a valid event chain')
                bound=db.execute("SELECT payload_json FROM events WHERE event_type='workday.root_bound' ORDER BY sequence DESC LIMIT 1").fetchone()
                if bound and Path(json.loads(bound[0])['root']).resolve()!=self.root:
                    raise ValueError('workday root binding changed; user decision required')
                if not bound:self.store._append_event_tx(db,None,'workday.root_bound',{'root':str(self.root)})
                exists=db.execute("SELECT 1 FROM events WHERE event_type='workday.render_requested' AND json_extract(payload_json,'$.date')=?",(day,)).fetchone()
                if not exists:self.store._append_event_tx(db,None,'workday.render_requested',{'date':day})
            # Snapshot AFTER locking so an older snapshot cannot overwrite a new one.
            rows,tasks,events = self._snapshot()
            folder=self.root/'工作区'/(day+'-小八台账')
            _directory(folder)
            for kind in KINDS[:3]:_directory(folder/kind)
            daily=[r for r in rows if r['date']==day]
            daily.sort(key=lambda r:(r['sequence'],r['time'],r['record_id']))
            _write(folder/'_索引.jsonl',''.join(canonical(r)+'\n' for r in daily))
            for kind in KINDS[:3]:
                _write(folder/kind/'引用.jsonl',''.join(canonical(r)+'\n' for r in daily if r['kind']==kind))
            _write(folder/'台账.md','# '+day+' 小八台账\n\nSQLite 阅读视图；原文件留在项目目录。\n\n'+
                   ''.join('- '+r['kind']+' · '+str(r['task_id'] or '无任务 ID')+' · '+r['status']+' · '+r['path']+' · SHA256 '+str(r['sha256'])+'\n' for r in daily))
            # The previous ledger day is reconstructed from DB dates, never files.
            days={r['date'] for r in rows}
            days.update(json.loads(e['payload_json'])['date'] for e in events if e['event_type']=='workday.render_requested')
            days.update(day_of(e['created_at']) for e in events if e['event_type']=='session.started')
            previous=max((d for d in days if d<day),default=None)
            unfinished=[]
            if previous:
                # Carry all unresolved IDs already present on the most recent day,
                # including tasks carried there from an older day.
                unfinished=[t for t in tasks.values() if day_of(t['created_at'])<=previous and t['status'] not in TERMINAL]
            unfinished.sort(key=lambda t:(t['created_at'],t['id']))
            _write(folder/'接续清单.md','# 接续清单\n\n最近台账：'+str(previous or '无')+'；任务 ID 跨天接续，以 SQLite 当前状态为准。\n\n'+
                   ''.join('- '+t['id']+' · '+t['status']+'\n' for t in unfinished))
            if checks is not None:_write(folder/'本地检测报告.json',canonical(checks)+'\n')
            catalog=[]
            for d in sorted(days|{day}):
                location=self.root/'工作区'/(d+'-小八台账')
                counts=Counter(r['kind'] for r in rows if r['date']==d)
                pending=sum(1 for t in tasks.values() if day_of(t['created_at'])<=d and t['status'] not in TERMINAL)
                catalog.append(dict(date=d,path=str(location),counts={k:counts[k] for k in KINDS},unfinished=pending,
                                    state='可读取' if (location/'_索引.jsonl').is_file() else '已清理、可重新生成'))
            _write(self.catalog/'_索引.jsonl',''.join(canonical(r)+'\n' for r in catalog))
            _write(self.catalog/'总目录.md','# 每日台账总目录\n\n'+''.join('- '+r['date']+' · '+r['state']+' · '+r['path']+' · '+canonical(r['counts'])+' · 未完成 '+str(r['unfinished'])+'\n' for r in catalog))
            return dict(directory=str(folder),date=day,entries=len(daily),continuation=len(unfinished),source='sqlite',model_tasks_sent=0)
