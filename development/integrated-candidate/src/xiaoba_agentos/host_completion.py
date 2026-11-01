"""Explicit host receipt entry, independent of experience approval or judging."""
import hashlib
import json
import math
import time

from .output_hygiene import contains_non_answer_markers, detect_reasoning_leak
from .pool_dispatch import needs_review, select_reviewers, load_nodes, task_exclude_ids
from .task_participants import execution_context
from .workflow import verified_snapshot


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and not contains_non_answer_markers(value) and not detect_reasoning_leak(value)


def complete_host(config, store, task_id, receipt, *, user_chose_host=False):
    if not isinstance(receipt, dict):raise ValueError('需要JSON宿主回执')
    checksum = hashlib.sha256(json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not store.verify_event_chain_tx(db)['ok']:raise ValueError('事件链校验失败')
        row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:raise KeyError(task_id)
        task = dict(row);snapshot = verified_snapshot(task)
        old = db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='host.receipt_accepted'", (task_id,)).fetchone()
        if old:
            if json.loads(old[0])['receipt_sha256'] != checksum:raise ValueError('同一任务已接收不同宿主回执')
            return {'status': 'completed', 'task_id': task_id, 'replayed': True}
        if task['status'] not in {'pending','retry'}:raise ValueError('宿主不能结算正在运行或已结束的任务')
        context = execution_context(store, task, db)
        host = context.get('host')
        if not host or receipt.get('reviewer_id') != host['id']:raise ValueError('回执不属于本任务指定宿主')
        if receipt.get('task_id') != task_id or receipt.get('snapshot_sha256') != task['execution_snapshot_hash']:
            raise ValueError('宿主回执未绑定冻结任务卡')
        from .memory import MemoryBridge
        if MemoryBridge._sensitive_reason(json.dumps(receipt, ensure_ascii=False)):
            raise ValueError('宿主回执含敏感资料')
        label = None
        pool_runs = []
        if needs_review(task):
            if host['worked'] or host['author']:raise ValueError('宿主这次干过活或是作者，不能审查')
            decision = select_reviewers(load_nodes(store), exclude_ids=set(context['excluded_ids']) | set(task_exclude_ids(task)),
                                        host=host, user_chose_host=user_chose_host)
            if not decision.get('host') or decision['route'] not in {'run','host_fallback'}:
                raise ValueError('当前不需要宿主补票，或尚未由用户选定宿主兜底')
            latest = store.latest_agent_runs(task_id)
            for node in decision['reviewers']:
                run = latest.get(node['node_id'])
                if not run or run['status'] != 'completed' or not _text(run['output']):
                    raise ValueError('池节点还没有交合格结论')
                pool_runs.append(run)
            if decision['route'] != 'host_fallback' and len(pool_runs) < 1:raise ValueError('至少需要一份池节点结论')
            from .adapters import AgentResult
            from .orchestrator import Orchestrator
            results = []
            for run in pool_runs:
                result = AgentResult(run['agent_name'], run['output'], run.get('latency_ms') or 0)
                result.run_id = run['id']
                results.append(result)
            if results:
                error = Orchestrator(config, store)._verify_settlement_receipts(task, results)
                if error:raise ValueError(error)
            if not receipt.get('receipt_id') or receipt.get('verdict') not in {'PASS','FAIL','NEEDS_INFO'} or not _text(receipt.get('findings')):
                raise ValueError('缺少有效审查正文、结论或回执ID，或违反B19')
            refs = receipt.get('evidence_refs')
            allowed = {'task:'+task_id}
            work_id = snapshot.get('review_context', {}).get('work_task_id')
            if work_id:allowed.add('task:'+work_id)
            if not isinstance(refs,list) or not refs or any(not isinstance(ref,str) or ref not in allowed for ref in refs):
                raise ValueError('证据引用未对应任务卡材料')
            score = receipt.get('score')
            if type(score) not in (int,float) or not math.isfinite(score) or not 0 <= score <= 10:raise ValueError('评分无效')
            if receipt.get('review_type') not in {'new_experience','revalidation','reuse_change'} or not _text(receipt.get('applicability')):
                raise ValueError('缺少审查类型、适用条件与限制')
            if receipt.get('duplicate_of') is not None and not isinstance(receipt['duplicate_of'],str):raise ValueError('重复经验标识无效')
            start, finish = receipt.get('started_at'), receipt.get('finished_at')
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in (start,finish)) or not 0 < start <= finish <= time.time()+300:
                raise ValueError('执行时间无效')
            label = decision.get('label')
            final = receipt['findings']
            if pool_runs:
                final += '\n\n池节点审查结论：\n'+'\n\n'.join(r['agent_name']+': '+r['output'] for r in pool_runs)
            if label:final = label+'\n\n'+final
        else:
            if not _text(receipt.get('result')):raise ValueError('宿主实际结果为空或违反B19')
            final = receipt['result']
        provenance = {'kind': 'host_review' if needs_review(task) else 'host_manual', 'status': 'completed',
                      'result_sha256': hashlib.sha256(final.encode()).hexdigest(), 'receipt_sha256': checksum,
                      'host_id': host['id'], 'pool_run_ids': [r['id'] for r in pool_runs], 'label': label}
        store._append_event_tx(db, task_id, 'host.receipt_accepted', {'receipt_sha256': checksum, 'receipt': receipt,
                               'user_chose_host': user_chose_host, 'label': label, 'pool_run_ids': provenance['pool_run_ids']})
        db.execute("UPDATE tasks SET status='completed',result=?,provenance_json=?,last_error=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                   (final,json.dumps(provenance,ensure_ascii=False,sort_keys=True),time.time(),task_id))
        store._append_event_tx(db, task_id, 'task.completed', {'result_sha256': provenance['result_sha256'], 'manual': True})
        return {'status': 'completed', 'task_id': task_id, 'label': label}
