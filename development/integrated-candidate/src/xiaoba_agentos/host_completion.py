"""Explicit host receipt entry, independent of experience approval or judging."""
import hashlib
import json
import math
import time

from .output_hygiene import contains_non_answer_markers, detect_reasoning_leak
from .pool_dispatch import (needs_review, review_vote_problem, review_vote_runs, select_reviewers,
                            load_nodes, task_exclude_ids)
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
        result = _settle_host(config, store, task_id, receipt, checksum,
                              user_chose_host=user_chose_host, staged_out=staged.append)
        if result.get('candidate_status') == 'value_discarded' and staged:
            store.cleanup_discarded_candidate(task_id, str(staged[0]), result.get('candidate_sha256'))
        return result
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
        trial = snapshot.get('experience_trial')
        if trial and not needs_review(task):
            # Execution-only trial shells still settle via complete_audit.
            raise ValueError('受控验证执行任务不能由宿主结算，请等池节点凑够审查结论')
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
        from .value_scoring import requires_value_receipt, value_receipt_problem

        def incomplete(reason):
            if not requires_value_receipt(task):
                raise ValueError(reason)
            # Rejected evidence is retained separately, never a completion or
            # an accepted receipt. The same receipt can be corrected and sent
            # again without changing the frozen task or double settlement.
            message = 'NEEDS_INFO: ' + reason
            db.execute("UPDATE tasks SET status='pending',last_error=?,lease_until=NULL,updated_at=? WHERE id=?",
                       (message, time.time(), task_id))
            prior = db.execute("SELECT 1 FROM events WHERE task_id=? AND event_type='host.receipt_needs_info' AND json_extract(payload_json,'$.receipt_sha256')=?",
                               (task_id, checksum)).fetchone()
            if not prior:
                store._append_event_tx(db, task_id, 'host.receipt_needs_info', {
                    'receipt_sha256': checksum, 'receipt': receipt,
                    'verdict': 'NEEDS_INFO', 'reason': reason,
                })
            return {'status': 'needs_info', 'verdict': 'NEEDS_INFO', 'task_id': task_id,
                    'reason': reason, 'candidate_status': 'none'}
        label = None
        verdict = None
        pool_runs = []
        if needs_review(task):
            if host['worked'] or host['author']:raise ValueError('宿主这次干过活或是作者，不能审查')
            if host['id'] in set(context['excluded_ids']):
                raise ValueError('宿主是本任务来源作者或执行者，不能自审补票')
            decision = select_reviewers(load_nodes(store), exclude_ids=set(context['excluded_ids']) | set(task_exclude_ids(task)),
                                        host=host, user_chose_host=user_chose_host)
            if not decision.get('host') or decision['route'] not in {'run','host_fallback'}:
                raise ValueError('当前不需要宿主补票，或尚未由用户选定宿主兜底')
            # 这一票按本任务的审查运行（run_id）取，不取“该节点最新一行”：
            # 等宿主补票期间点一次“测试运行”或别的用途运行，都不会让审查票作废。
            votes = review_vote_runs(db, task_id)
            for node in decision['reviewers']:
                run = votes.get(node['node_id'])
                if not run or run['status'] != 'completed' or not _text(run['output']):
                    raise ValueError('池节点还没有交合格结论')
                problem = review_vote_problem(run['output'], task)
                if problem:
                    return incomplete('池节点这一票不能当审查回执：' + problem)
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
                return incomplete('缺少有效审查正文、结论或回执ID，或违反B19')
            problem = value_receipt_problem(task, receipt)
            if problem:
                return incomplete(problem)
            refs = receipt.get('evidence_refs')
            from .value_scoring import value_evidence
            allowed_map = value_evidence(task)
            if not isinstance(refs, list) or not refs:
                return incomplete('证据引用未对应任务卡材料')
            for item in refs:
                ref_key = item.get('ref') if isinstance(item, dict) else item
                if not isinstance(ref_key, str) or ref_key not in allowed_map:
                    return incomplete('证据引用未对应任务卡材料')
                if isinstance(item, dict):
                    expected_sha = allowed_map[ref_key]
                    sha = item.get('sha256')
                    if not isinstance(sha, str) or len(sha) != 64 or (expected_sha is not None and sha != expected_sha):
                        return incomplete('证据引用哈希与任务卡材料不符')
            score = receipt.get('score')
            if type(score) not in (int,float) or not math.isfinite(score) or not 0 <= score <= 10:return incomplete('评分无效')
            if receipt.get('review_type') not in {'new_experience','revalidation','reuse_change'} or not _text(receipt.get('applicability')):
                return incomplete('缺少审查类型、适用条件与限制')
            if receipt.get('duplicate_of') is not None and not isinstance(receipt['duplicate_of'],str):return incomplete('重复经验标识无效')
            start, finish = receipt.get('started_at'), receipt.get('finished_at')
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in (start,finish)) or not 0 < start <= finish <= time.time()+300:
                return incomplete('执行时间无效')
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
        if not produces_candidate:
            provenance['memory_disposition'] = 'audit_only'
        evidence_sha256 = None
        stage_candidate = produces_candidate and (not needs_review(task) or receipt.get('verdict') == 'PASS')
        # Bind the host vote before evaluating admission, in this same locked
        # transaction. A failed staging or score write rolls this back too.
        pool_run_digests = {
            r['id']: hashlib.sha256((r.get('output') or '').encode()).hexdigest() for r in pool_runs
        }
        store._append_event_tx(db, task_id, 'host.receipt_accepted', {'receipt_sha256': checksum, 'receipt': receipt,
                               'user_chose_host': user_chose_host, 'label': label, 'pool_run_ids': provenance['pool_run_ids'],
                               'pool_run_digests': pool_run_digests})
        from .value_scoring import ConflictingValueReceipt, native_value
        score_parse_error = False
        try:
            assessment = native_value(db, task_id) if stage_candidate else None
        except ConflictingValueReceipt:
            raise
        except (ValueError, KeyError, TypeError, IndexError):
            # Keep the completed work result; _insert_candidate_tx will place
            # malformed scoring receipts in the value-review preparation queue.
            assessment = None
            score_parse_error = True
        if stage_candidate:
            # The host vote is still uncommitted: pass the combined assessment
            # instead of reading only the pool vote on a second connection.
            staged = MemoryBridge(config, store).stage_experience(task_id, task['prompt'], final,
                store.task_runs(task_id), value_assessment=assessment,
                skip_value_scoring=score_parse_error)
            staged_out(staged)
            evidence_sha256 = hashlib.sha256(staged.read_bytes()).hexdigest()
            store._require_staged_evidence(str(staged), evidence_sha256)
        db.execute("UPDATE tasks SET status='completed',result=?,provenance_json=?,last_error=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                   (final,json.dumps(provenance,ensure_ascii=False,sort_keys=True),time.time(),task_id))
        completed_payload = {'result_sha256': provenance['result_sha256'], 'manual': True}
        if not produces_candidate:
            completed_payload['memory_disposition'] = 'audit_only'
        store._append_event_tx(db, task_id, 'task.completed', completed_payload)
        candidate_status = 'none'
        if stage_candidate:
            source_task_ids = [str(hit.get('source_task_id')) for hit in snapshot.get('memory_hits', []) if hit.get('source_task_id')]
            admitted = store._insert_candidate_tx(db, task_id, str(staged), evidence_sha256, source_task_ids, time.time())
            if admitted:
                store._append_event_tx(db, task_id, 'memory.candidate_staged', {'evidence_sha256': evidence_sha256})
                candidate_status = 'pending_delivery_review'
            else:
                from .value_queue import pending_items
                pending = any(item['task_id'] == task_id
                              and item.get('queue_status') == 'awaiting_value_review'
                              for item in pending_items(db))
                candidate_status = 'awaiting_value_review' if pending else 'value_discarded'
        if not produces_candidate:
            store._append_event_tx(db, task_id, 'audit.settled', {'result_sha256': provenance['result_sha256'], 'host_vote': True})
            if trial:
                # The host vote joins the audit and round advancement in the same
                # transaction; the round reads the bound receipts itself.
                from .experience_workflow import advance_validation_round, _table_exists
                round_data = {
                    'task_id': task_id,
                    'candidate_sha256': trial.get('candidate_sha256'),
                    'delivery_review_id': trial.get('delivery_review_id'),
                    'plan_digest': trial.get('card_sha256'),
                    'execution': trial.get('execution'),
                    'execution_digest': trial.get('execution_sha256'),
                    'diff_explanation': (trial.get('card') or {}).get('diff_explanation'),
                }
                if _table_exists(db, 'experience_validation_lineages'):
                    advance_validation_round(store, db, trial['source_task_id'], round_data)
                store._append_event_tx(db, trial['source_task_id'], 'memory.trial_review_settled',
                                      {'task_id': task_id, 'host_vote': True})
        return {'status': 'completed', 'task_id': task_id, 'label': label,
                'value_assessment': assessment, 'candidate_status': candidate_status,
                'candidate_sha256': evidence_sha256}
