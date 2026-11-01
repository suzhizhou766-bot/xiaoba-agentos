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


def _settlement_committed(store, task_id):
    """Decide from the database, never from the exception, whether this task settled."""
    try:
        return store.get_task(task_id)['status'] == 'completed'
    except Exception:
        return True  # 读不出来就不删，宁可留下待人工核对


def complete_host(config, store, task_id, receipt, *, user_chose_host=False):
    if not isinstance(receipt, dict):raise ValueError('需要JSON宿主回执')
    checksum = hashlib.sha256(json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    staged = []
    try:
        return _settle_host(config, store, task_id, receipt, checksum,
                            user_chose_host=user_chose_host, staged_out=staged.append)
    except Exception:
        # 事务回滚后不留一个没有数据库行的候选文件；提交与否由数据库决定。
        if staged and not _settlement_committed(store, task_id):
            for leftover in (staged[0], staged[0].with_suffix(staged[0].suffix+'.status.json')):
                try: leftover.unlink()
                except OSError: pass
        raise


def _settle_host(config, store, task_id, receipt, checksum, *, user_chose_host=False, staged_out=None):
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
        if snapshot.get('experience_trial'):
            # 受控验证的轮次由 complete_audit 推进；宿主结算不走这条线，放行会让轮次卡住。
            raise ValueError('受控验证任务不能由宿主结算，请等池节点凑够审查结论')
        context = execution_context(store, task, db)
        host = context.get('host')
        if not host or receipt.get('reviewer_id') != host['id']:raise ValueError('回执不属于本任务指定宿主')
        if receipt.get('task_id') != task_id or receipt.get('snapshot_sha256') != task['execution_snapshot_hash']:
            raise ValueError('宿主回执未绑定冻结任务卡')
        from .memory import MemoryBridge
        if MemoryBridge._sensitive_reason(json.dumps(receipt, ensure_ascii=False)):
            raise ValueError('宿主回执含敏感资料')
        # 要进初审的新经验走宿主补票时候选不能丢：走 Worker 那一条路径、同一套门禁。
        # 审计任务本来就不产候选，宿主也不在这里结算审计回执或调用裁判。
        from .experience_workflow import policy
        produces_candidate = policy(task)['kind'] != 'audit'
        if produces_candidate and not store._candidate_gates_tx(db, task_id):
            raise ValueError('约束保留还没由用户释放，宿主不能结算这个任务')
        label = None
        verdict = None
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
            verdict = receipt['verdict']
            # 宿主的 FAIL、NEEDS_INFO 照样结算（结论必须留下来），但不得被读成通过。
            final = '宿主审查结论：'+verdict+{'PASS':'','FAIL':'（未通过）','NEEDS_INFO':'（未通过，需补资料）'}[verdict]+'\n\n'+final
        else:
            if not _text(receipt.get('result')):raise ValueError('宿主实际结果为空或违反B19')
            final = receipt['result']
        provenance = {'kind': 'host_review' if needs_review(task) else 'host_manual', 'status': 'completed',
                      'verdict': verdict,
                      'result_sha256': hashlib.sha256(final.encode()).hexdigest(), 'receipt_sha256': checksum,
                      'host_id': host['id'], 'pool_run_ids': [r['id'] for r in pool_runs], 'label': label}
        evidence_sha256 = None
        if produces_candidate:
            staged = MemoryBridge(config, store).stage_experience(task_id, task['prompt'], final, store.task_runs(task_id))
            staged_out(staged)
            evidence_sha256 = hashlib.sha256(staged.read_bytes()).hexdigest()
            store._require_staged_evidence(str(staged), evidence_sha256)
        store._append_event_tx(db, task_id, 'host.receipt_accepted', {'receipt_sha256': checksum, 'receipt': receipt,
                               'user_chose_host': user_chose_host, 'label': label, 'pool_run_ids': provenance['pool_run_ids']})
        db.execute("UPDATE tasks SET status='completed',result=?,provenance_json=?,last_error=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                   (final,json.dumps(provenance,ensure_ascii=False,sort_keys=True),time.time(),task_id))
        store._append_event_tx(db, task_id, 'task.completed', {'result_sha256': provenance['result_sha256'], 'manual': True})
        if produces_candidate:
            source_task_ids = [str(hit.get('source_task_id')) for hit in snapshot.get('memory_hits', []) if hit.get('source_task_id')]
            store._insert_candidate_tx(db, task_id, str(staged), evidence_sha256, source_task_ids, time.time())
            store._append_event_tx(db, task_id, 'memory.candidate_staged', {'evidence_sha256': evidence_sha256})
        return {'status': 'completed', 'task_id': task_id, 'label': label}
