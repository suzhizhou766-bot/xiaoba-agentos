"""Formal revision and inactivity decisions projected from hash-chain events.

No schema changes or new-candidate trial transitions. Review receipts are
materials; only an explicit browser user decision publishes or archives.
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path
from .experience_workflow import digest
from .reuse_scores import REVISION, projection_tx, source_version

PREFIX = 'memory.formal_'
DAY = 86400


def _events(db):
    return [dict(r, value=json.loads(r['payload_json'])) for r in db.execute(
        "SELECT * FROM events WHERE event_type=? OR event_type LIKE 'memory.formal_%' ORDER BY sequence", (REVISION,))]


def _chain(db):
    from .store import EventStore
    if not EventStore.verify_event_chain_tx(db)['ok']: raise ValueError('event chain is invalid')


def _draft(ref):
    from .memory import MemoryBridge
    from .output_hygiene import contains_non_answer_markers, detect_reasoning_leak
    if not isinstance(ref, dict) or not isinstance(ref.get('path'), str):
        raise ValueError('complete revision file and sha256 required')
    p = Path(ref['path'])
    if not p.is_absolute() or p.is_symlink() or p.resolve() != p or not p.is_file() or p.stat().st_size > 1000000:
        raise ValueError('revision must be a regular absolute file of at most 1000000 bytes')
    raw = p.read_bytes()
    if hashlib.sha256(raw).hexdigest() != ref.get('sha256'): raise ValueError('revision body hash changed')
    body = raw.decode('utf-8')
    if (not body.strip() or MemoryBridge._sensitive_reason(body) or contains_non_answer_markers(body)
            or detect_reasoning_leak(body) or '[[' in body):
        raise ValueError('revision body failed sensitive/hygiene/tree-link gate')
    return body


def _source(db, item):
    return source_version(db, {'candidate_id': item['source_task_id'], 'candidate_sha256': item['source_sha256']})[0]


def review_packet(db, item):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='ri_inbox'").fetchone(): return None
    for row in db.execute('SELECT i.data,i.digest FROM ri_inbox i JOIN ri_requests r ON r.id=i.request_id WHERE r.candidate_id=? AND i.current=1 ORDER BY i.ack_at DESC', (item['task_id'],)):
        packet = json.loads(row['data'])
        if digest(packet) != row['digest']: raise ValueError('review packet digest mismatch')
        subject = packet['subject']
        if (subject.get('review_type') != 'reuse_change' or subject.get('candidate_sha256') != item['draft']['sha256']
                or subject.get('source_sha256') != item['source_sha256']): continue
        assignments = db.execute('SELECT * FROM ri_assignments WHERE request_id=? AND active=1', (packet['review_request_id'],)).fetchall()
        if len(assignments) != 2: continue
        excluded=_excluded_for(db,item)
        from xiaoba_review.agent_pool import independence
        sources=[json.loads(db.execute('SELECT data FROM ri_sources WHERE id=?',(a['source_id'],)).fetchone()[0]) for a in assignments]
        valid = len({independence(source) for source in sources})==2
        for a in assignments:
            if a['source_id'] in excluded: valid=False
            latest = db.execute('SELECT digest,status,attestation FROM ri_receipts WHERE assignment_id=? ORDER BY rowid DESC LIMIT 1', (a['id'],)).fetchone()
            matching = [r for r in packet['reviews'] if r['receipt']['assignment_id']==a['id']]
            if not latest or latest['status']!='valid' or not latest['attestation'] or len(matching)!=1 or matching[0]['digest']!=latest['digest']: valid=False
        if valid: return packet
    return None


def projection(db):
    objects = {}
    for event in _events(db):
        v, kind = event['value'], event['event_type']
        if kind == REVISION:
            objects[event['event_id']] = dict(v, task_id=event['event_id'], entry_type='reuse_change',
                status='awaiting_body', created_at=event['created_at'], updated_at=event['sequence'], draft=None, reviews=[])
        elif kind == PREFIX+'batch_created':
            objects[event['event_id']] = dict(v, task_id=event['event_id'], entry_type='unused_batch',
                status='pending', created_at=event['created_at'], updated_at=event['sequence'])
        elif v.get('request_id') in objects:
            item = objects[v['request_id']]; item['updated_at'] = event['sequence']
            if kind == PREFIX+'draft_submitted': item.update(draft=v['draft'], status='awaiting_review', reviews=[])
            elif kind == PREFIX+'host_review': item['host_packet']=v['packet']
            elif kind == PREFIX+'decided': item.update(status='withdrawn' if item['entry_type']=='unused_batch' and v['decision']=='approve' else 'approved' if v['decision']=='approve' else 'rejected', decision=v)
    for item in objects.values():
        if item['status'] in {'approved','rejected','withdrawn'} or item['entry_type']!='reuse_change': continue
        try: _source(db, item)
        except (ValueError, OSError): item['status'] = 'stale'
        if item['draft']:
            try:
                item['body'] = _draft(item['draft']); item['hash_matches'] = True
            except (ValueError, OSError, UnicodeError): item['hash_matches'] = False
        if item['status'] != 'stale' and item.get('hash_matches'):
            packet = review_packet(db, item)
            if not packet and item.get('host_packet'):
                h=item['host_packet']
                if h['subject']['candidate_sha256']==item['draft']['sha256'] and h['subject']['source_sha256']==item['source_sha256'] and _host_packet_current(db,item,h):
                    packet=h
            if packet:
                item.update(status='pending', reviews=packet['reviews'], review_packet_sha256=digest(packet), review_label=packet.get('label'))
                item['updated_at']=str(item['updated_at'])+':'+digest(packet)
    return objects


def _view(item):
    item = dict(item); kind, status = item['entry_type'], item['status']
    sha = item['draft']['sha256'] if item.get('draft') else digest(item.get('entries', item.get('applications', [])))
    item.update(evidence_sha256=sha, stage='final', title='复用改动终审' if kind=='reuse_change' else '长期未用确认（合批）',
        prompt=item.get('source_task_id','长期未用经验'), status_label={'awaiting_body':'待补正文','awaiting_review':'待审查','pending':'待终审','stale':'版本已过期'}.get(status,status),
        allowed_decisions=(['approve','reject'] if status=='pending' else ['reject'] if kind=='reuse_change' and status in {'awaiting_body','awaiting_review','stale'} else []),
        expected={'status':status,'updated_at':item['updated_at'],'evidence_sha256':sha}, hash_matches=item.get('hash_matches',True), reviews=item.get('reviews',[]), experience_validation={})
    if kind=='unused_batch':
        item['body']='\n\n'.join(f"{e['title']}\n经验：{e['source_task_id']}\n最后调用：{e['last_used_at']}\n计时起点：{e['clock_at']}\n当前间隔：{e['interval_days']}天\n永久总分：{e['score']}" for e in item['entries'])
    else:
        item['body']=item.get('body','待宿主补交完整修订稿')+'\n\n本次改动材料\n'+json.dumps({'source':item['source_task_id'],'version':item['source_sha256'],'task':item['real_task_id'],'applications':item.get('applications',[]),'reviews':item['reviews'],'label':item.get('review_label')},ensure_ascii=False,indent=2)
    return item


def items(store, include_closed=False):
    with store.read_only() as db:
        _chain(db)
        return [_view(v) for v in projection(db).values() if include_closed or v['status'] not in {'approved','rejected','withdrawn'}]


def subject(db, request_id):
    item=projection(db).get(request_id)
    if not item or item['entry_type']!='reuse_change' or item['status'] in {'stale','approved','rejected','withdrawn','awaiting_body'} or not item.get('hash_matches'):
        raise ValueError('revision is missing body, stale, closed or hash changed')
    return {'candidate_id':request_id,'candidate_sha256':item['draft']['sha256'],'delivery_review':None,
            'body':_draft(item['draft']),'source_task_id':item['source_task_id'],'source_sha256':item['source_sha256'],
            'real_task_id':item['real_task_id'],'applications':item.get('applications',[])}


def _excluded_for(db, item):
    from .task_participants import execution_context
    excluded=set()
    for tid in (item['source_task_id'],item['real_task_id']):
        task=db.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
        if not task: raise ValueError('revision source/real task missing')
        excluded.update(execution_context(None,dict(task),db)['excluded_ids'])
    return excluded


def exclusions(store, db, request_id):
    item=projection(db).get(request_id)
    return _excluded_for(db,item) if item and item['entry_type']=='reuse_change' else None


def _host_packet_current(db, item, packet):
    excluded=_excluded_for(db,item)
    for review in packet['reviews']:
        receipt=review['receipt']
        if receipt['reviewer_id'] in excluded: return False
        if review['identity_basis']=='bound task host': continue
        row=db.execute('SELECT digest,status,attestation FROM ri_receipts WHERE assignment_id=? ORDER BY rowid DESC LIMIT 1',(receipt['assignment_id'],)).fetchone()
        if not row or row['digest']!=review['digest'] or row['status']!='valid' or not row['attestation']: return False
    return True


def submit_draft(store, request_id, ref):
    _draft(ref); ref={k:ref[k] for k in ('path','sha256')}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE'); _chain(db); item=projection(db).get(request_id)
        if not item or item['entry_type']!='reuse_change' or item['status'] in {'approved','rejected','withdrawn','stale'}: raise ValueError('revision request missing, closed or expired')
        _source(db,item)
        if item.get('draft')!=ref: store._append_event_tx(db,item['source_task_id'],PREFIX+'draft_submitted',{'request_id':request_id,'draft':ref})
    return {'request_id':request_id,'draft':ref}


def record_call(store, source_id, source_sha256):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE'); _chain(db)
        source_version(db,{'candidate_id':source_id,'candidate_sha256':source_sha256})
        store._append_event_tx(db,source_id,PREFIX+'called',{'source_task_id':source_id,'called_at':time.time()})
    return {'source_task_id':source_id,'called':True}


def due(db, now):
    events=_events(db); enabled=next((e['value']['enabled_at'] for e in events if e['event_type']==PREFIX+'enabled'),None)
    if enabled is None: return []
    pending={e['source_task_id'] for x in projection(db).values() if x['entry_type']=='unused_batch' and x['status']=='pending' for e in x['entries']}
    scores=projection_tx(db); result=[]
    for r in db.execute("SELECT mc.*,t.prompt FROM memory_candidates mc JOIN tasks t ON t.id=mc.task_id WHERE mc.status='published' ORDER BY mc.task_id"):
        sid=r['task_id']
        if sid in pending: continue
        resets=[e['value']['decided_at'] for e in events if e['event_type']==PREFIX+'decided' and e['value'].get('entry_type')=='unused_batch' and e['value']['decision']=='approve' and sid in e['value']['source_ids']]
        interval=(14,30,90)[min(len(resets),2)]; start=max(enabled,r['published_at'] or enabled,*resets)
        hit=db.execute('SELECT max(created_at) FROM memory_reuse WHERE source_task_id=?',(sid,)).fetchone()[0]
        calls=[e['value']['called_at'] for e in events if e['event_type']==PREFIX+'called' and e['value']['source_task_id']==sid]
        calls.extend(e['value']['called_at'] for e in events if e['event_type']==PREFIX+'retrieved' and any(h['source_task_id']==sid for h in e['value']['sources']))
        used=max([v for v in [hit,scores.get(sid,{}).get('last_used_at'),*calls] if v is not None],default=None)
        clock=max(start,used if used is not None else start)
        if now-clock>interval*DAY:
            p=Path(r['published_path'])
            if p.is_symlink() or not p.is_file(): continue
            result.append({'source_task_id':sid,'source_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'title':r['prompt'],
                'last_used_at':used,'clock_at':clock,'interval_days':interval,'score':scores.get(sid,{}).get('score',0)})
    return result


def check_inactivity(store, now=None):
    now=time.time() if now is None else now
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE'); _chain(db)
        if not any(e['event_type']==PREFIX+'enabled' for e in _events(db)): store._append_event_tx(db,None,PREFIX+'enabled',{'enabled_at':now})
        entries=due(db,now)
        if not entries: return []
        rid=store._append_event_tx(db,None,PREFIX+'batch_created',{'entries':entries,'checked_at':now})
        return [_view(projection(db)[rid])]


def _published_revision(source, body, draft_sha256, request_id):
    # Only canonical lifecycle metadata changes; approved prose, including its
    # title, whitespace and final newline, is carried verbatim.
    fm=re.match(r'\A---[ \t]*\n.*?\n---[ \t]*(?:\n|\Z)',body,re.S)
    prose=body[fm.end():] if fm else body
    sid=source['source_task_id']
    header=('---\n'+f'id: "experience-{sid}"\n'+'type: "experience"\nparent: "experiences"\n'
        +'status: "active"\nauthority: false\n'+f'source_task_id: "{sid}"\n'
        +f'revision_request_id: "{request_id}"\napproved_draft_sha256: "{draft_sha256}"\n'
        +f'previous_version_sha256: "{source["source_sha256"]}"\n---\n')
    return header+prose


def decide(config, store, request_id, decision, expected, note, *, origin, now=None):
    """Verified browser action. Atomic DB settlement; file errors restore memory bytes."""
    from .memory import MemoryBridge
    if origin!='user' or decision not in {'approve','reject'}: raise ValueError('explicit user decision required')
    now=time.time() if now is None else now; bridge=MemoryBridge(config,store)
    originals={}; created=[]; moves=[]
    with bridge._publication_lock():
        try:
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE'); _chain(db); obj=projection(db).get(request_id)
                if not obj: raise ValueError('formal request not found')
                item=_view(obj)
                if item['expected']!=expected or decision not in item['allowed_decisions']: raise ValueError('request changed, unreviewed or closed; refresh')
                kind=item['entry_type']; result={'request_id':request_id,'entry_type':kind,'decision':decision}
                retiring=kind=='unused_batch' and decision=='reject'; publishing=kind=='reuse_change' and decision=='approve'
                if publishing or retiring:
                    if publishing and not config.memory.get('publish_enabled'): raise ValueError('memory.publish_enabled=false')
                    vault=Path(config.memory['obsidian_vault']).resolve(); catalog=vault/config.memory.get('catalog_note','70_复利规则/经验索引.md')
                    if catalog.is_symlink() or not catalog.is_file(): raise ValueError('catalog missing or symlink')
                    originals[catalog]=catalog.read_bytes(); text=originals[catalog].decode(); plans=[]
                    for entry in ([item] if publishing else item['entries']):
                        source=_source(db,entry); old=Path(source['published_path']); old.resolve().relative_to(vault)
                        link='- [['+str(old.relative_to(vault).with_suffix(''))+']]'
                        if text.splitlines().count(link)!=1: raise ValueError('source catalog link not unique')
                        archive=config.runtime_dir/('memory-versions' if publishing else 'memory-retired')/(entry['source_task_id']+'-'+entry['source_sha256']+'.md')
                        if archive.exists() or archive.is_symlink(): raise ValueError('archive conflicts; reconciliation required')
                        
                        if archive.parent.is_symlink(): raise ValueError('archive directory must not be symlink')
                        archive.parent.mkdir(parents=True,exist_ok=True,mode=0o700); new=None
                        if publishing:
                            body=_draft(item['draft']); new=old.with_name('experience-'+entry['source_task_id']+'-revision-'+request_id+'.md')
                            if new.exists() or new.is_symlink(): raise ValueError('new version conflicts; reconciliation required')
                            bridge._atomic_write_text(new,_published_revision(entry,body,item['draft']['sha256'],request_id)); created.append(new)
                            new_link='- [['+str(new.relative_to(vault).with_suffix(''))+']]'
                            text='\n'.join(new_link if line==link else line for line in text.splitlines())+'\n'
                        else: text=bridge._without_catalog_link(text,link)
                        plans.append((entry,old,archive,new))
                    for entry,old,archive,new in plans:
                        os.replace(old,archive); moves.append((old,archive))
                        if publishing:
                            db.execute('UPDATE memory_candidates SET published_path=?,updated_at=? WHERE task_id=?',(str(new),now,entry['source_task_id']))
                            result.update(path=str(new),archive_path=str(archive),draft_sha256=item['draft']['sha256'])
                        else: db.execute("UPDATE memory_candidates SET status='retired',published_path=?,updated_at=? WHERE task_id=?",(str(archive),now,entry['source_task_id']))
                    bridge._atomic_write_text(catalog,text); result['index']=bridge._rebuild_and_verify_index(vault)
                    if retiring: result['archives']=[str(a) for _,a in moves]
                source_ids=[e['source_task_id'] for e in item['entries']] if kind=='unused_batch' else [item['source_task_id']]
                payload=dict(result,request_id=request_id,source_ids=source_ids,decided_at=now,note=note,reviewed_sha256=item['evidence_sha256'],review_packet_sha256=item.get('review_packet_sha256'))
                receipt=store._append_event_tx(db,source_ids[0] if len(source_ids)==1 else None,PREFIX+'decided',payload)
                result.update(review_id=receipt,status='withdrawn' if kind=='unused_batch' and decision=='approve' else 'approved' if decision=='approve' else 'rejected')
            return result
        except Exception:
            with store.read_only() as db: settled=projection(db).get(request_id,{})
            if settled.get('status') in {'approved','rejected','withdrawn'}: raise RuntimeError('decision committed; reconcile receipt, do not repeat side effects')
            for old,archive in reversed(moves): os.replace(archive,old)
            for p,data in originals.items(): bridge._atomic_write_text(p,data.decode())
            for p in created:
                if p.exists():
                    pending=config.runtime_dir/'_待删'; pending.mkdir(exist_ok=True)
                    os.replace(p,pending/p.name)
                    with (pending/'说明.md').open('a') as log: log.write(f'\n失败发布的新文件：{p.name}，由用户清理。\n')
            if originals: bridge._rebuild_and_verify_index(Path(config.memory['obsidian_vault']).resolve())
            raise


def submit_host_review(store, request_id, receipt, *, user_chose_host=False):
    """Explicit host handoff, using the pool's existing eligibility decision.

    The host identity comes from the real task record; worked/author flags
    cannot be supplied by the caller. Fallback requires the user's choice.
    """
    from .task_participants import execution_context
    from .pool_dispatch import select_reviewers, load_nodes
    from .host_completion import _text
    import math
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE'); _chain(db)
        item=projection(db).get(request_id)
        if not item: raise ValueError('revision missing')
        frozen=subject(db,request_id)
        task=db.execute('SELECT * FROM tasks WHERE id=?',(item['real_task_id'],)).fetchone()
        context=execution_context(store,dict(task),db); host=context.get('host')
        excluded=exclusions(store,db,request_id)
        if not host or receipt.get('reviewer_id')!=host['id'] or host['id'] in excluded: raise ValueError('宿主已执行或是作者，或身份未绑定')
        selection=select_reviewers(load_nodes(store),exclude_ids=excluded,host=host,user_chose_host=user_chose_host)
        if not selection.get('host') or selection['route'] not in {'run','host_fallback'}: raise ValueError('当前不需要宿主补票或尚未由用户选择兜底')
        if receipt.get('candidate_sha256')!=frozen['candidate_sha256'] or receipt.get('request_id')!=request_id or receipt.get('review_type')!='reuse_change': raise ValueError('宿主回执未绑定完整修订稿')
        if receipt.get('verdict') not in {'PASS','FAIL','NEEDS_INFO'} or not _text(receipt.get('findings')) or not _text(receipt.get('applicability')): raise ValueError('宿主回执不完整')
        start,finish=receipt.get('started_at'),receipt.get('finished_at')
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (start,finish)) or not 0<start<=finish<=time.time()+300: raise ValueError('宿主回执执行时间无效')
        score=receipt.get('score')
        if type(score) not in (int,float) or not math.isfinite(score) or not 0<=score<=10: raise ValueError('宿主评分无效')
        if not receipt.get('receipt_id') or receipt.get('evidence_refs')!=[item['draft']]: raise ValueError('宿主证据未绑定修订稿文件和哈希')
        reviews=[]
        for node in selection['reviewers']:
            match=None
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='ri_receipts'").fetchone():
                rows=db.execute('SELECT r.*,a.card,a.source_id FROM ri_receipts r JOIN ri_assignments a ON a.id=r.assignment_id JOIN ri_requests q ON q.id=r.request_id WHERE q.candidate_id=? AND a.active=1 AND a.source_id=? ORDER BY r.rowid DESC',(request_id,node['node_id'])).fetchall()
                if rows:
                    row=rows[0]; value=json.loads(row['data']); card=json.loads(row['card'])
                    if row['status']=='valid' and row['attestation'] and value.get('candidate_sha256')==frozen['candidate_sha256'] and card.get('source_sha256')==item['source_sha256']:
                        match={'receipt':value,'digest':row['digest'],'identity_basis':row['attestation']}
            if not match: raise ValueError('池节点尚未提供绑定本修订稿的独立合格回执')
            reviews.append(match)
        from .memory import MemoryBridge
        if MemoryBridge._sensitive_reason(json.dumps(receipt,ensure_ascii=False)): raise ValueError('宿主回执含敏感内容')
        reviews.append({'receipt':receipt,'digest':digest(receipt),'identity_basis':'bound task host'})
        packet={'subject':dict(frozen,review_type='reuse_change'),'reviews':reviews,'label':selection.get('label'),'approval':'human_required'}
        old=[e['value'] for e in _events(db) if e['event_type']==PREFIX+'host_review' and e['value'].get('request_id')==request_id]
        if not old or old[-1]['packet']!=packet:
            store._append_event_tx(db,item['source_task_id'],PREFIX+'host_review',{'request_id':request_id,'packet':packet})
    return {'request_id':request_id,'status':'awaiting_user_final','label':selection.get('label')}


def record_hits(store, hits):
    """Persist verified index calls separately from delivery scores; no query text."""
    formal=[h for h in hits if h.get('source_task_id')]
    if not formal: return
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE'); _chain(db)
        ids=[]
        for hit in formal:
            try:
                source_version(db,{'candidate_id':hit['source_task_id'],'candidate_sha256':hit['source_sha256']})
            except (ValueError,OSError): continue
            ids.append({'source_task_id':hit['source_task_id'],'source_sha256':hit['source_sha256']})
        if ids: store._append_event_tx(db,None,PREFIX+'retrieved',{'sources':ids,'called_at':time.time()})


def resubmit(store, request_id, application_id, draft):
    """An expired request needs a newly registered current-version application.

    Reuse scoring stays settled once per real task/source; this creates only a
    replacement review request and never merges drafts or repeats a score.
    """
    from .experience_loop import _events as applications, _application_files
    _draft(draft)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE'); _chain(db)
        old=projection(db).get(request_id)
        if not old or old['entry_type']!='reuse_change' or old['status']!='stale': raise ValueError('only an expired revision can be resubmitted')
        app=next((a for a in applications(db,'memory.application_recorded',old['source_task_id']) if a['application_id']==application_id),None)
        if not app or not app.get('formal_reuse') or app['real_task']['id']!=old['real_task_id']: raise ValueError('resubmit requires a current application of the same real task')
        source,sha=source_version(db,app); _application_files(app)
        deliveries=[dict(r,value=json.loads(r['payload_json'])) for r in db.execute("SELECT event_id,payload_json FROM events WHERE event_type='task.delivery_recorded' ORDER BY sequence")]
        accepted=[d for d in deliveries if d['value']['real_task_id']==old['real_task_id'] and application_id in d['value']['application_ids']]
        if not accepted or accepted[-1]['value']['decision']!='accept': raise ValueError('current application has no accepted delivery')
        assessment=app.get('reuse_assessment') or {}
        if assessment.get('outcome') not in {'modified','inapplicable'} or any(not isinstance(assessment.get(k),str) for k in ('changed','added','discarded')) or not any(assessment[k].strip() for k in ('changed','added','discarded')): raise ValueError('resubmit needs explicit modified/inapplicable assessment')
        for item in projection(db).values():
            if item.get('replaces_request_id')==request_id and item['source_sha256']==sha and item.get('draft')==draft: return {'request_id':item['task_id'],'replayed':True}
        payload=dict(source_task_id=old['source_task_id'],source_sha256=sha,real_task_id=old['real_task_id'],
            application_ids=[application_id],delivery_event_id=accepted[-1]['event_id'],replaces_request_id=request_id,
            reuse_assessment=assessment,applications=[{k:app[k] for k in ('application_id','application_location','steps_applied','deviations','reuse_assessment')}])
        rid=store._append_event_tx(db,old['source_task_id'],REVISION,payload)
        store._append_event_tx(db,old['source_task_id'],PREFIX+'draft_submitted',{'request_id':rid,'draft':draft})
    return {'request_id':rid,'replaces_request_id':request_id}
