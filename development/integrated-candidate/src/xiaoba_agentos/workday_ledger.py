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

REFERENCE_BATCH_LIMIT=500
REFERENCE_FAILURE_LIMIT=3

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
            'nodes':nodes,'workday':workday_status(store),'scope':'local configuration, entry and test qualification; no model call'}


class RootConflict(Exception):
    pass


MESSAGES={
    'unconfigured':'台账未配置根目录，已跳过生成',
    'binding_conflict':'台账根目录与已绑定位置不一致，需要用户决定',
    'lock_timeout':'台账生成锁等待超时，已跳过本次生成',
    'registration_failed':'台账引用登记失败，业务结算不受影响',
    'metadata_rejected':'台账元数据已跳过，业务结算不受影响',
    'generation_failed':'台账生成失败，业务结算不受影响',
    'reference_dead':'台账引用连续失败，已移入死信目录，不再自动重试',
    'marker_failed':'台账待刷新标记写入失败，业务结算不受影响',
    'local_check_failed':'上班本地检查失败，本次未生成台账；请看开工结果里的本地检查错误'}


def resolve_root(config):
    """The only config-to-root rule; never substitute runtime or a DB binding."""
    try:
        value=config.memory.get('obsidian_vault')
        if not isinstance(value,str) or not value.strip():return None
        path=Path(value).expanduser()
        return path.resolve().parent if path.is_absolute() else None
    except Exception:return None


def configure(store,config):
    store.workday_root=resolve_root(config)
    return store.workday_root


def _sidecar(store,name):
    # 只在真实数据库旁边写诊断文件；路径不是绝对路径（如测试替身）时拒绝，避免在工作目录下建目录（卡 64 复查观察 2）。
    path=Path(store.path)
    if not path.is_absolute():
        raise ValueError('workday sidecar needs an absolute database path')
    return path.parent/('.workday-'+name)


def warn(store,code):
    """Best-effort, payload-free diagnostics. Never throw, including from logging."""
    value={'ok':False,'warning_code':code,'message':MESSAGES.get(code,MESSAGES['generation_failed'])}
    try:
        repeated=getattr(store,'_workday_last_warning',None)==code
        store._workday_last_warning=code
        _write(_sidecar(store,'status.json'),canonical(value)+'\n')
        if not repeated:
            import logging
            logging.getLogger(__name__).warning('%s',value['message'])
    except Exception:
        try:
            import logging
            logging.getLogger(__name__).warning('%s',value['message'])
        except Exception:pass
    return value


def workday_status(store,config=None):
    """Read-only diagnostics; warnings are deliberately outside doctor.ok."""
    try:
        root=resolve_root(config) if config is not None else getattr(store,'workday_root',None)
        if not isinstance(root,(str,Path)):
            return {'ok':False,'warning_code':'unconfigured','message':MESSAGES['unconfigured'],'pending':_pending(store)}
        with store.read_only() as db:
            found=db.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone()
            bound=db.execute("SELECT payload_json FROM events WHERE event_type='workday.root_bound' ORDER BY sequence DESC LIMIT 1").fetchone() if found else None
        if bound and Path(json.loads(bound[0])['root']).resolve()!=Path(root).resolve():
            return {'ok':False,'warning_code':'binding_conflict','message':MESSAGES['binding_conflict'],'pending':_pending(store)}
        path=_sidecar(store,'status.json')
        value=json.loads(path.read_text()) if path.is_file() else {'ok':True}
        # Root warnings above describe current configuration; a stored warning
        # becomes history once the configuration/binding has been reconciled.
        if value.get('warning_code') in ('unconfigured','binding_conflict'):
            value={'ok':True,'last_warning':value}
        return dict(value,pending=_pending(store),root=str(root))
    except Exception:
        return {'ok':False,'warning_code':'generation_failed','message':MESSAGES['generation_failed'],'pending':True}


def _pending(store):
    pending=_sidecar(store,'pending');rendered=_sidecar(store,'rendered')
    return pending.is_file() and (not rendered.is_file() or pending.read_bytes()!=rendered.read_bytes())


def mark_dirty(store):
    import uuid
    _write(_sidecar(store,'pending'),uuid.uuid4().hex+'\n')


def bound_ledger(store):
    try:
        root=getattr(store,'workday_root',None)
        if not isinstance(root,(str,Path)):
            warn(store,'unconfigured');return None
        return WorkdayLedger(store,root)
    except Exception:
        warn(store,'generation_failed');return None


def bind_root(store,root):
    # Explicit SDK/test binding, not an alternative config default.
    try:
        store.workday_root=Path(root).resolve()
        return WorkdayLedger(store,root)
    except Exception:
        warn(store,'binding_conflict');return None


def record_reference(store,path,**metadata):
    try:return _register_reference(store,path,**metadata)
    except Exception:return warn(store,'registration_failed')


def _register_reference(store,path,**metadata):
    """Queue metadata cheaply; do not acquire SQLite's writer or render here."""
    try:return _queue_reference(store,path,**metadata)
    except Exception:return warn(store,'registration_failed')


def _queue_reference(store,path,*,node,task_id,agent,model,kind,status):
    import uuid
    if not isinstance(getattr(store,'workday_root',None),(str,Path)):
        return warn(store,'unconfigured')
    path=Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():raise ValueError('invalid output path')
    if kind not in KINDS or not all(isinstance(v,str) and v.strip() for v in (node,agent,model,status)):raise ValueError('invalid metadata')
    if task_id is not None and (not isinstance(task_id,str) or not task_id.strip()):raise ValueError('invalid task ID')
    ref=dict(path=str(path),node=node,task_id=task_id,agent=agent,model=model,kind=kind,status=status)
    from .memory import MemoryBridge
    if MemoryBridge._sensitive_reason(canonical(ref)):return warn(store,'metadata_rejected')
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as handle:
        before=os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):raise ValueError('invalid output')
        ref['sha256']=hashlib.file_digest(handle,'sha256').hexdigest();after=os.fstat(handle.fileno())
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('output changed')
    ref['recorded_at']=time.time()
    queue=_sidecar(store,'references')
    _write(queue/(str(time.time_ns()).zfill(20)+'-'+uuid.uuid4().hex+'.json'),canonical(ref)+'\n')
    mark_dirty(store)
    return {'ok':True,'pending':True,'source':'sqlite_after_idle_registration'}


def _flush_references(store):
    """At most 500 queued objects, one event scan and one atomic transaction.

    The per-day key retains the latest revision, preserving A -> B -> A while
    skipping consecutive identical references. No schema/index changes.
    """
    import heapq
    queue=_sidecar(store,'references')
    if not queue.is_dir():return
    paths=heapq.nsmallest(REFERENCE_BATCH_LIMIT,queue.glob('*.json'))
    if not paths:return
    failure_path=_sidecar(store,'reference-failures.json')
    failures=json.loads(failure_path.read_text()) if failure_path.is_file() else {}
    valid=[];failed=[];consumed=[]
    for path in paths:
        try:
            if path.is_symlink():raise ValueError('invalid queued reference')
            ref=json.loads(path.read_text());stamp=ref.pop('recorded_at')
            # Queue corruption cannot cause a partial batch or poison SQL keys.
            required={'path','node','task_id','agent','model','kind','status','sha256'}
            if set(ref)!=required or ref['kind'] not in KINDS:raise ValueError('invalid queued metadata')
            if not all(isinstance(ref[k],str) and ref[k] for k in required-{'task_id'}):raise ValueError('invalid queued field')
            if not Path(ref['path']).is_absolute() or (ref['task_id'] is not None and not isinstance(ref['task_id'],str)):raise ValueError('invalid queued identity')
            valid.append((path,ref,stamp,day_of(stamp)))
        except Exception:failed.append(path)
    if valid:
        try:
            with store.connect(refresh_views=False,timeout=.1) as db:
                db.execute('BEGIN IMMEDIATE')
                days={day for _,_,_,day in valid};latest={}
                # One scan for the batch, never a history scan per queued row.
                for row in db.execute("SELECT payload_json,created_at FROM events WHERE event_type='workday.output_recorded' ORDER BY sequence"):
                    previous=json.loads(row[0]);old_stamp=previous.pop('recorded_at',row[1]);day=day_of(old_stamp)
                    if day in days:
                        key=(day,previous['task_id'],previous['path'],previous['node'],previous['kind'])
                        latest[key]=previous
                for path,ref,stamp,day in valid:
                    key=(day,ref['task_id'],ref['path'],ref['node'],ref['kind'])
                    if latest.get(key)!=ref:
                        store._append_event_tx(db,ref['task_id'],'workday.output_recorded',dict(ref,recorded_at=stamp))
                        latest[key]=ref
            consumed=[path for path,_,_,_ in valid]
        except Exception:failed.extend(path for path,_,_,_ in valid)
    for path in consumed:
        try:
            os.replace(path,_sidecar(store,'processed-reference.json'))
            failures.pop(path.name,None)
        except Exception:failed.append(path)
    for path in failed:
        count=int(failures.get(path.name,0))+1
        if count>=REFERENCE_FAILURE_LIMIT:
            dead=_sidecar(store,'references-dead');_directory(dead)
            os.replace(path,dead/path.name);failures.pop(path.name,None)
            warn(store,'reference_dead')
        else:
            failures[path.name]=count
            warn(store,'registration_failed')
    if failures or failure_path.exists():_write(failure_path,canonical(failures)+'\n')
    # The renderer only acknowledges its pre-flush token. A remaining queue
    # (including a retry) must keep the next read/idle cycle dirty.
    if next(queue.glob('*.json'),None) is not None:mark_dirty(store)


def refresh_after_write(store,after_sequence=0):
    """Post-commit work is one cheap pending token, never a scan or rebuild."""
    try:mark_dirty(store)
    except Exception:warn(store,'marker_failed')


def rebuild_view(store,*,config=None,day=None,checks=None,only_if_dirty=False):
    try:
        if config is not None:configure(store,config)
        if only_if_dirty and not _pending(store):return workday_status(store,config)
        view=bound_ledger(store)
        result=view.rebuild(day,checks=checks) if view else warn(store,'unconfigured')
    except Exception:result=warn(store,'generation_failed')
    if checks is not None:
        try:_write(_sidecar(store,'local-check.json'),canonical(dict(checks,workday=result))+'\n')
        except Exception:warn(store,'generation_failed')
    return result


def read_workday(store,day=None):
    try:
        view=bound_ledger(store)
        if view is None:return warn(store,'unconfigured')
        date_value=date.fromisoformat(day or day_of(time.time())).isoformat()
        folder=view.root/'工作区'/(date_value+'-小八台账')
        if _pending(store) or not (folder/'_索引.jsonl').is_file():return rebuild_view(store,day=date_value)
        return {'ok':True,'directory':str(folder),'date':date_value,'pending':False,'source':'sqlite'}
    except Exception:return warn(store,'generation_failed')


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
        """Queue an original reference; rendering is deferred to an explicit trigger."""
        try:return _register_reference(self.store,path,node=node,task_id=task_id,agent=agent,model=model,kind=kind,status=status)
        except Exception:return warn(self.store,'registration_failed')

    def _snapshot(self):
        with self.store.read_only() as db:
            db.execute('BEGIN')
            if not self.store.verify_event_chain_tx(db)['ok']:
                raise ValueError('workday requires a valid event chain')
            events = [dict(row) for row in db.execute('SELECT * FROM events ORDER BY sequence')]
            tasks = {r['id']:dict(r) for r in db.execute('SELECT * FROM tasks')}
            candidates = [dict(r) for r in db.execute('SELECT * FROM memory_candidates ORDER BY created_at,task_id')]
            reuse = [dict(r) for r in db.execute('SELECT * FROM memory_reuse ORDER BY created_at,id')]
        starts={};stages={};reuses={}
        for event in events:
            event['value']=json.loads(event['payload_json'])
            kind=event['event_type'];value=event['value']
            if kind=='agent.started':starts.setdefault(event['task_id'],[]).append(event)
            elif kind=='memory.candidate_staged':stages.setdefault(event['task_id'],[]).append(event)
            elif kind=='memory.reused':reuses.setdefault((event['task_id'],value.get('source_task_id')),event)
        rows = []
        event_order={e['event_id']:e['sequence'] for e in events}
        def add(value, timestamp, identity):
            rows.append(dict(value, time=timestamp, date=day_of(timestamp), record_id=identity,
                             sequence=event_order.get(identity.split(':')[0],0)))
        def identity(task_id, sequence, node=None, run_id=None):
            agents=[];models=[]
            for e in starts.get(task_id,[]):
                if e['sequence']>sequence:break
                v=e['value']
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
                value=e['value']
                add(value,value.get('recorded_at',e['created_at']),e['event_id'])
            elif e['event_type']=='agent.finished':
                v=e['value']
                if v.get('receipt_path') and v.get('receipt_file_sha256'):
                    agent,model=identity(e['task_id'],e['sequence'],v['agent'],v['run_id'])
                    add(dict(path=v['receipt_path'],sha256=v['receipt_file_sha256'],node=v['agent'],task_id=e['task_id'],
                             agent=agent,model=model,kind='回执',status=v['status']),e['created_at'],e['event_id'])
            elif e['event_type']=='agent_pool.tmux_job':
                v=e['value'];tmux[v['job_id']]=v
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
                v=e['value'];task_id=v.get('real_task_id') or (v.get('real_task') or {}).get('id')
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
            for stage in stages.get(c['task_id'],[]):
                payload=stage['value']
                add(dict(path=payload.get('staged_path',c['staged_path']),sha256=payload.get('evidence_sha256',c['evidence_sha256']),node='memory',task_id=c['task_id'],
                     agent=identity(c['task_id'],stage['sequence'])[0],
                     model=identity(c['task_id'],stage['sequence'])[1],kind='新经验',status='pending_delivery_review'),
                    stage['created_at'],stage['event_id'])
        for r in reuse:
            event=reuses.get((r['task_id'],r['source_task_id']))
            sha=event['value'].get('source_sha256') if event else None
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

    def rebuild(self,day=None,*,checks=None,lock_timeout=.25):
        try:return self._rebuild(day,checks=checks,lock_timeout=lock_timeout)
        except RootConflict:return warn(self.store,'binding_conflict')
        except Exception:return warn(self.store,'generation_failed')

    def _rebuild(self,day=None,*,checks=None,lock_timeout=.25):
        day=date.fromisoformat(day or day_of(time.time())).isoformat()
        state=workday_status(self.store)
        if state.get('warning_code') in ('unconfigured','binding_conflict'):
            return warn(self.store,state['warning_code'])
        _directory(self.catalog)
        lock=self.catalog/'.workday.lock'
        with os.fdopen(os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600),'a') as handle:
            deadline=time.monotonic()+lock_timeout
            while True:
                try:
                    fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=deadline:return warn(self.store,'lock_timeout')
                    time.sleep(min(.02,max(0,deadline-time.monotonic())))
            token=_sidecar(self.store,'pending').read_bytes() if _sidecar(self.store,'pending').is_file() else b''
            _flush_references(self.store)
            # Snapshot AFTER locking so an older snapshot cannot overwrite a new one.
            rows,tasks,events = self._snapshot()
            # The sole chain check and all projection data came from the closed
            # read-only snapshot above. This short metadata transaction ends
            # before any view file is rendered.
            with self.store.connect(refresh_views=False,timeout=.1) as db:
                db.execute('BEGIN IMMEDIATE')
                bound=db.execute("SELECT payload_json FROM events WHERE event_type='workday.root_bound' ORDER BY sequence DESC LIMIT 1").fetchone()
                if bound and Path(json.loads(bound[0])['root']).resolve()!=self.root:raise RootConflict()
                if not bound:self.store._append_event_tx(db,None,'workday.root_bound',{'root':str(self.root)})
                exists=db.execute("SELECT 1 FROM events WHERE event_type='workday.render_requested' AND json_extract(payload_json,'$.date')=?",(day,)).fetchone()
                if not exists:self.store._append_event_tx(db,None,'workday.render_requested',{'date':day})

            folder=self.root/'工作区'/(day+'-小八台账')
            _directory(folder)
            for kind in KINDS[:3]:_directory(folder/kind)
            daily=[r for r in rows if r['date']==day]
            daily.sort(key=lambda r:(r['sequence'],r['time'],r['record_id']))
            _write(folder/'_索引.jsonl',''.join(canonical(r)+'\n' for r in daily))
            for kind in KINDS[:3]:
                _write(folder/kind/'引用.jsonl',''.join(canonical(r)+'\n' for r in daily if r['kind']==kind))
            _write(folder/'台账.md','# '+day+' 小八台账\n\nSQLite 阅读视图；原文件留在项目目录。\n\n'+
                   ''.join('- '+r['kind']+' · '+str(r['task_id'] or '无任务 ID')+' · '+r['status']+' · '+r['path']+' · '+('SHA256 '+r['sha256'] if r.get('sha256') else r.get('hash_state','历史记录未保存文件哈希'))+'\n' for r in daily))
            # The previous ledger day is reconstructed from DB dates, never files.
            days={r['date'] for r in rows}
            days.update(e['value']['date'] for e in events if e['event_type']=='workday.render_requested')
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
            if checks is not None:_write(folder/'本地检测报告.json',canonical(dict(checks,workday=workday_status(self.store)))+'\n')
            catalog=[]
            for d in sorted(days|{day}):
                location=self.root/'工作区'/(d+'-小八台账')
                counts=Counter(r['kind'] for r in rows if r['date']==d)
                pending=sum(1 for t in tasks.values() if day_of(t['created_at'])<=d and t['status'] not in TERMINAL)
                catalog.append(dict(date=d,path=str(location),counts={k:counts[k] for k in KINDS},unfinished=pending,
                                    state='可读取' if (location/'_索引.jsonl').is_file() else '已清理、可重新生成'))
            _write(self.catalog/'_索引.jsonl',''.join(canonical(r)+'\n' for r in catalog))
            _write(self.catalog/'总目录.md','# 每日台账总目录\n\n'+''.join('- '+r['date']+' · '+r['state']+' · '+r['path']+' · '+canonical(r['counts'])+' · 未完成 '+str(r['unfinished'])+'\n' for r in catalog))
            _write(_sidecar(self.store,'rendered'),token.decode())
            previous_status=workday_status(self.store)
            status={'ok':True}
            if not previous_status.get('ok'):status['last_warning']=previous_status
            _write(_sidecar(self.store,'status.json'),canonical(status)+'\n')
            return dict(directory=str(folder),date=day,entries=len(daily),continuation=len(unfinished),source='sqlite',model_tasks_sent=0)
