"""TERMINAL ONLY: case12 under lead ruling; only the local report is not a DB view."""

# --- rebound by stage75q ---
"""stage75q helper — rebound from mature v1614-r7 source; paths via STAGE75Q_BIND_JSON."""
from pathlib import Path
import os, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bind import load_bind
_BIND = load_bind()
_BIND.require_ready()
R = _BIND.root
O = _BIND.O
A = _BIND.A
W = _BIND.W
C = _BIND.C
PKG = _BIND.PKG
VERSION = _BIND.version
LIVE = _BIND.live
P = LIVE
os.chdir(R)
# --- end bind header; original body follows with path overrides ---
from contextlib import contextmanager
from types import SimpleNamespace
import hashlib
import sqlite3
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime,timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

R = _BIND.root
W = _BIND.work;O = _BIND.O
assert (_BIND.work/'BASE_READY.json').is_file()
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from workday_isolation import assert_config,real_views,unchanged
from history_execution import HistoryExecution,reference_history_checks

LOCAL_CHECK_REPORT='本地检测报告.json'


def files(folder):
    return {str(p.relative_to(folder)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob('*')) if p.is_file()}


def card65_checks(root, EventStore, ledger, ReviewStore):
    """Exercise real candidate code only in independent scene-owned databases."""
    probe=root/'case65-isolated'
    probe.mkdir(exist_ok=False)
    assert probe.resolve().is_relative_to(root)
    def make_store(name,configured):
        location=probe/name;location.mkdir()
        store=EventStore(location/'runtime/db.sqlite');store.initialize()
        cfg=SimpleNamespace(memory={'obsidian_vault':str(location/'vault')} if configured else {})
        selected=ledger.configure(store,cfg)
        assert selected==(location if configured else None)
        return location,store
    def count(store,task):
        with store.read_only() as db:
            return db.execute("SELECT COUNT(*) FROM events WHERE event_type='workday.output_recorded' AND task_id=?",(task,)).fetchone()[0]
    def enqueue(store,location,task,quantity):
        product=location/'product.txt';product.write_text('TEST card65 isolated product')
        for index in range(quantity):
            result=ledger.record_reference(store,product,node='TEST card65 '+str(index),task_id=task,
                agent='TEST host',model='TEST no model',kind='产出',status='pending')
            assert result.get('ok') and result.get('pending'),result
        return product

    # Count history executions through the scoped store/direct SQLite factories.
    location,batch=make_store('batch',True)
    assert ledger.REFERENCE_BATCH_LIMIT==500
    enqueue(batch,location,'TEST-card65-batch',501)
    queue=batch.path.parent/'.workday-references'
    with batch.read_only() as db:
        schema_before=[tuple(row) for row in db.execute('SELECT name,sql FROM sqlite_master ORDER BY name')]
    meter=HistoryExecution(batch.path)
    with meter.window(batch):
        ledger._flush_references(batch)
    meter.require_budget()
    assert count(batch,'TEST-card65-batch')==500 and len(list(queue.glob('*.json')))==1
    assert ledger.workday_status(batch)['pending']
    assert 'directory' in ledger.rebuild_view(batch)
    assert count(batch,'TEST-card65-batch')==501 and not list(queue.glob('*.json'))
    assert not ledger.workday_status(batch)['pending']
    with batch.read_only() as db:
        assert schema_before==[tuple(row) for row in db.execute('SELECT name,sql FROM sqlite_master ORDER BY name')]

    # A failure during the batch must roll every queued event back atomically.
    rollback_root,rollback=make_store('rollback',True)
    enqueue(rollback,rollback_root,'TEST-card65-rollback',3)
    rollback_queue=rollback.path.parent/'.workday-references'
    append=rollback._append_event_tx;calls=0
    def fail_second(*args,**kwargs):
        nonlocal calls
        calls+=1
        if calls==2:raise sqlite3.OperationalError('TEST card65 batch failure')
        return append(*args,**kwargs)
    with patch.object(rollback,'_append_event_tx',side_effect=fail_second):
        ledger._flush_references(rollback)
    assert count(rollback,'TEST-card65-rollback')==0 and len(list(rollback_queue.glob('*.json')))==3
    ledger._flush_references(rollback)
    assert count(rollback,'TEST-card65-rollback')==3 and not list(rollback_queue.glob('*.json'))
    batch_result={'ok':True,'budget':500,'first_processed':500,'first_remaining':1,
        'batch_connections':meter.connections,'begin_immediate_count':meter.transaction_begins,
        'reference_scans':meter.history_queries,'execution_meter':meter.result(),
        'pending_preserved':True,'eventual_count':501,'rollback_whole_batch':True,'schema_unchanged':True}
    batch_result['reference_history']=reference_history_checks(probe,EventStore,ledger)

    # Missing config must not create a queue or modify an existing/absent marker.
    missing_root,missing=make_store('unconfigured',False)
    product=missing_root/'product.txt';product.write_text('TEST card65 unconfigured')
    marker=missing.path.parent/'.workday-pending';before=marker.read_bytes() if marker.exists() else None
    with patch('logging.Logger.warning') as warnings:
        for _ in range(3):
            value=ledger.record_reference(missing,product,node='TEST card65',task_id=None,
                agent='TEST host',model='TEST no model',kind='产出',status='pending')
            assert value.get('warning_code')=='unconfigured'
    after=marker.read_bytes() if marker.exists() else None
    assert before==after and not (marker.parent/'.workday-references').exists() and warnings.call_count==1
    unconfigured_result={'ok':True,'queue_absent':True,'marker_unchanged':True,'warning_count':warnings.call_count}

    # Exercise the actual ingress transaction and the actual tmux event shape.
    ingress_root,ingress=make_store('ingress',True)
    review=ReviewStore(ingress,ingress_root/'outbox')
    assert 'directory' in ledger.rebuild_view(ingress)
    assert not ledger.workday_status(ingress)['pending']
    card=ingress_root/'task-card.txt';card.write_text('TEST attended task card')
    card_hash=hashlib.sha256(card.read_bytes()).hexdigest()
    event={'job_id':'TEST-card65-job','node_id':'TEST-card65-node','status':'waiting_receipt',
        'created_at':time.time(),'ticket':{'task_id':'TEST-card65-external','card':str(card),'card_sha256':card_hash},
        'source':{'node':{'agent':'TEST host','model':'TEST no model'}}}
    with review.transaction() as db:
        ingress._append_event_tx(db,None,'agent_pool.tmux_job',event)
    assert ledger.workday_status(ingress)['pending']
    view=ledger.read_workday(ingress)
    rows=[json.loads(line) for line in Path(view['directory'],'_索引.jsonl').read_text().splitlines()]
    assert any(row.get('path')==str(card) and row.get('sha256')==card_hash for row in rows)
    assert not ledger.workday_status(ingress)['pending']
    try:
        with review.transaction() as db:
            ingress._append_event_tx(db,None,'workday.finished',{'message_id':'TEST-card65-rollback'})
            raise ValueError('TEST card65 rollback')
    except ValueError:pass
    assert not ledger.workday_status(ingress)['pending']
    ingress_result={'ok':True,'committed_tmux_event_marks_pending':True,'read_refreshes_reference':True,
        'rollback_does_not_mark_pending':True}
    return batch_result,unconfigured_result,ingress_result


def main():
    scene=json.loads((O/'SCENE.json').read_text());root=Path(scene['root']).resolve()
    assert root.is_relative_to(R/'rehearsal')
    checked=assert_config(Path(scene['config']),root)
    assistant=Path(checked['ledger_root']);package=Path(scene['runtime'])/f'.runtime/releases/{VERSION}'
    sys.path.insert(0,str(package/'agentos/src'))
    from xiaoba_agentos.config import AppConfig
    from xiaoba_agentos.store import EventStore
    from xiaoba_agentos.release import release_binding_status
    from xiaoba_agentos.workday_ledger import WorkdayLedger,configure,rebuild_view,workday_status
    from xiaoba_agentos import workday_ledger as ledger_module
    from xiaoba_review.store import ReviewStore
    cfg=AppConfig.load(Path(scene['config']));assert release_binding_status(cfg)['ok']
    native=EventStore(cfg.database_path);assert configure(native,cfg)==assistant
    env=dict(os.environ,PYTHONPATH=str(package/'agentos/src'),PYTHONDONTWRITEBYTECODE='1')
    before_real=real_views();report={'ok':False,'site':checked,'production_writes':0}
    def cli(label,*args):
        with (O/('49-'+label+'.json')).open('x') as handle:
            q=subprocess.run([sys.executable,'-B','-m','xiaoba_agentos.cli','--config',str(cfg.path),*args],
                             cwd=Path(scene['runtime']),env=env,stdout=handle,stderr=subprocess.STDOUT)
        assert q.returncode==0,'STOP: case12 '+label+' failed; preserve output'
        return json.loads((O/('49-'+label+'.json')).read_text())
    try:
        with native.read_only() as db:before_runs=db.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0]
        # The preceding eleven cases used a live scene Worker. Stop it before
        # testing deferred views, so its idle refresh cannot race this observer.
        gate=json.loads((O/'STEP4-5-result.json').read_text())
        assert gate['cleanup_before_case12']['ok']
        current_day=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        expected_folder=assistant/'工作区'/(current_day+'-小八台账')
        before_write=files(expected_folder)
        lazy_task=native.submit('TEST lazy ledger settlement',1)
        lazy_product=root/'case12-lazy-product.txt';lazy_product.write_text('TEST lazy reference')
        ledger=WorkdayLedger(native,assistant)
        queued=ledger.record(lazy_product,node='TEST case12',task_id=lazy_task,agent='TEST host',
            model='TEST no model call',kind='产出',status='pending')
        assert queued['ok'] and queued['pending']
        after_write=files(expected_folder)
        assert before_write==after_write,'STOP: business write synchronously changed the daily view'
        assert workday_status(native)['pending']
        report['lazy_refresh']={'ok':True,'task_id':lazy_task,'daily_view_unchanged_after_write':True,
            'before':before_write,'after':after_write,'explicit_trigger':'session-start'}
        session=cli('session-start','session-start')
        folder=Path(session['workday']['directory']).resolve()
        assert folder.parent==assistant/'工作区' and folder.is_relative_to(root)
        required={'台账.md','_索引.jsonl','接续清单.md','本地检测报告.json'}
        assert all((folder/p).is_file() for p in required)
        assert (assistant/'工具/每日台账索引总目录/_索引.jsonl').is_file()
        checks=json.loads((folder/'本地检测报告.json').read_text());assert checks['model_tasks_sent']==0
        with native.read_only() as db:assert db.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0]==before_runs
        assert lazy_task in (folder/'_索引.jsonl').read_text(),'STOP: explicit session refresh missed queued reference'
        day=folder.name.removesuffix('-小八台账')
        # Ruling 2: rebuilding an existing directory must leave its session-time
        # local report untouched. Do this before removing the daily directory.
        report_path=folder/LOCAL_CHECK_REPORT
        report_hash_before=hashlib.sha256(report_path.read_bytes()).hexdigest()
        cli('workday-rebuild-preserve-report','workday-rebuild','--day',day)
        assert report_path.is_file(),'STOP: rebuild removed the existing local report'
        report_hash_after=hashlib.sha256(report_path.read_bytes()).hexdigest()
        report['existing_report_preserved']={'ok':report_hash_after==report_hash_before,
            'before_sha256':report_hash_before,'after_sha256':report_hash_after}
        assert report_hash_after==report_hash_before,'STOP: rebuild overwrote the existing local report'
        # Ruling 1: every other file is a strict byte-for-byte DB projection.
        original={name:digest for name,digest in files(folder).items() if name!=LOCAL_CHECK_REPORT}
        # User-authorized removal in the scene follows AGENTS: move originals;
        # never duplicate a backup or touch the real workspace.
        pending=root/'_待删';pending.mkdir(exist_ok=True)
        retained=pending/folder.name;assert not retained.exists();folder.rename(retained)
        with (pending/'说明.md').open('a') as f:f.write('卡63第12项：移入被清理的演练日台账原对象，验证 SQLite 重建；不是备份。\n')
        cli('workday-rebuild','workday-rebuild','--day',day)
        rebuilt={name:digest for name,digest in files(folder).items() if name!=LOCAL_CHECK_REPORT}
        report_not_fabricated=not report_path.exists()
        report['rebuild']={'ok':original==rebuilt and report_not_fabricated,'before':original,'after':rebuilt,
            'excluded_files':[LOCAL_CHECK_REPORT],'local_report_not_fabricated':report_not_fabricated,
            'missing':sorted(original.keys()-rebuilt.keys()),'extra':sorted(rebuilt.keys()-original.keys()),
            'changed':sorted(p for p in original.keys()&rebuilt.keys() if original[p]!=rebuilt[p])}
        # Use one stable task ID across two simulated calendar days, no model.
        zone=ZoneInfo('Asia/Shanghai');today=datetime.now(zone).date();yesterday=today-timedelta(days=1)
        product=root/'case12-product.txt';product.write_text('TEST isolated continuation reference')
        ledger=WorkdayLedger(native,assistant)
        moment=datetime.combine(yesterday,datetime.min.time(),zone).timestamp()+3600
        with patch('xiaoba_agentos.workday_ledger.time.time',return_value=moment):
            task=native.submit('TEST card63 cross-day continuation',1)
            ledger.record(product,node='TEST case12',task_id=task,agent='TEST host',model='TEST no model call',kind='回执',status='pending')
        value=ledger.rebuild(today.isoformat())
        continuation=Path(value['directory'])/'接续清单.md'
        assert task in continuation.read_text()
        native.complete_manually(task,'TEST case12 completed without a model')
        ledger.rebuild(today.isoformat());assert task not in continuation.read_text()
        report['continuation']={'ok':True,'task_id':task,'from':str(yesterday),'to':str(today),'native_task_api':True}
        # Inject exactly one reconstruction failure after business settlement;
        # completing a task itself must never call the renderer.
        with patch('xiaoba_agentos.workday_ledger.WorkdayLedger._rebuild',side_effect=RuntimeError('TEST injected view failure')) as failed:
            native.complete_manually(lazy_task,'TEST completed before view failure')
            assert failed.call_count==0,'STOP: settlement synchronously invoked reconstruction'
            failure=rebuild_view(native,config=cfg)
            assert failed.call_count==1
        settled=native.get_task(lazy_task)
        assert failure['ok'] is False and failure['warning_code']=='generation_failed'
        assert settled['status']=='completed','STOP: ledger failure altered business settlement'
        report['settlement_isolated']={'ok':True,'task_id':lazy_task,'status':settled['status'],
            'injected_failures':1,'view_result':failure}
        report['card65_batch'],report['card65_unconfigured'],report['card65_ingress']=card65_checks(root,EventStore,ledger_module,ReviewStore)
        report['ok']=report['existing_report_preserved']['ok'] and report['rebuild']['ok'] and report['continuation']['ok'] and report['lazy_refresh']['ok'] and report['settlement_isolated']['ok'] and report['card65_batch']['ok'] and report['card65_unconfigured']['ok'] and report['card65_ingress']['ok']
    finally:
        report['real_workspace_guard']=unchanged(before_real)
        report['ok']=report['ok'] and report['real_workspace_guard']['ok']
        (O/'49-workday-case12.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    assert report['ok'],'STOP: workday case12 failed; only the local report is excluded after removal; never restore retained files'


if __name__=='__main__':main()
