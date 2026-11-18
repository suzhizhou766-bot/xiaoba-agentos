"""New-experience admission: reviewer scores, never a human approval."""
import json
import math
import re


class ConflictingValueReceipt(ValueError):
    """A receipt gives incompatible explicit values for the same field."""


def requires_value_receipt(task):
    """Only newly frozen contracts require v2 fields; old receipts stay readable."""
    from .pool_dispatch import snapshot_of, needs_review
    from .experience_workflow import policy
    contract = snapshot_of(task).get('value_receipt_contract') or {}
    return (contract.get('version') == 2 and needs_review(task)
            and policy(task)['kind'] in {'method', 'rule'})


def expects_value_receipt(task):
    """An explicit review requires a vote. Ordinary adjudication may just answer."""
    from .pool_dispatch import declared_work_tasks, snapshot_of, task_host
    if not requires_value_receipt(task):
        return False
    snapshot = snapshot_of(task)
    context = snapshot.get('review_context') or {}
    work_tasks, _ = declared_work_tasks(snapshot)
    return bool(context.get('host') or task_host(task) or work_tasks
                or (snapshot.get('workflow') or {}).get('phase') == 'review')


def declares_value_receipt(text):
    """Recognize a submitted review without treating a score in an answer as a vote."""
    if not isinstance(text, str):
        return False
    if text.lstrip().startswith('{'):
        try:
            structured = json.loads(text)
        except ValueError:
            pass
        else:
            if isinstance(structured, dict):
                return structured.get('review_type') == 'new_experience' or (
                    structured.get('verdict') in ('PASS', 'FAIL', 'NEEDS_INFO') and 'evidence_refs' in structured
                )
    if re.search(r'^[ \t]*(?:审查类型|review_type)[ \t]*[:：][ \t]*new_experience[ \t]*$', text, re.M | re.I):
        return True
    from .pool_dispatch import REVIEW_VERDICT_FIELD
    return bool(REVIEW_VERDICT_FIELD.search(text)
                and re.search(r'^[ \t]*(?:证据引用|evidence_refs)[ \t]*[:：]', text, re.M | re.I))


def settlement_value_receipt_problem(task, text, *, host_review=False):
    if host_review or expects_value_receipt(task) or declares_value_receipt(text):
        return value_receipt_problem(task, text)
    return ''


def value_evidence(task):
    from .pool_dispatch import snapshot_of
    snapshot = snapshot_of(task)
    refs = {'task:' + str(task['id']): task.get('execution_snapshot_hash')}
    work_id = (snapshot.get('review_context') or {}).get('work_task_id')
    if work_id:
        refs['task:' + str(work_id)] = None
    for material in snapshot.get('material_files') or []:
        if isinstance(material, dict):
            ref = material.get('ref') or material.get('path')
            if isinstance(ref, str) and ref:
                refs[ref] = material.get('sha256')
    return refs


def value_receipt_problem(task, receipt):
    """Validate the same value fields for pool text and structured host receipts."""
    if not requires_value_receipt(task):
        return ''
    from .output_hygiene import contains_non_answer_markers, detect_reasoning_leak
    if isinstance(receipt, str):
        try:
            data = json.loads(receipt)
        except (TypeError, ValueError):
            data = None
        if not isinstance(data, dict):
            data = {'evidence_refs': []}
            from .pool_dispatch import REVIEW_VERDICT_FIELD
            verdicts = set(REVIEW_VERDICT_FIELD.findall(receipt))
            if len(verdicts) != 1:
                return '缺少唯一审查结论（PASS / FAIL / NEEDS_INFO）'
            data['verdict'] = next(iter(verdicts))
            names = {'适用条件': 'applicability', '适用条件与限制': 'applicability',
                     'applicability': 'applicability', '审查类型': 'review_type',
                     'review_type': 'review_type', '证据引用': 'evidence_refs',
                     'evidence_refs': 'evidence_refs'}
            for line in receipt.splitlines():
                match = re.fullmatch(r'[ \t]*([^:：]+)[ \t]*[:：][ \t]*(.*?)[ \t]*', line)
                if not match:
                    continue
                field = names.get(match[1].strip().casefold())
                if field == 'evidence_refs':
                    raw = match[2]
                    try: parsed = json.loads(raw)
                    except ValueError: parsed = raw
                    data[field].extend(parsed if isinstance(parsed, list) else [parsed])
                elif field:
                    if field in data and data[field] != match[2]:
                        return field + ' 字段自相矛盾'
                    data[field] = match[2]
            try:
                data['score'] = from_body('value-receipt', receipt)['score']
            except (ValueError, TypeError):
                return '缺少有效的唯一价值评分（0–10）'
    elif isinstance(receipt, dict):
        data = receipt
    else:
        return '缺少有效价值审查回执'
    if data.get('verdict') not in {'PASS', 'FAIL', 'NEEDS_INFO'}:
        return '缺少唯一审查结论（PASS / FAIL / NEEDS_INFO）'
    score = data.get('score')
    if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 10:
        return '缺少有效的唯一价值评分（0–10）'
    applicability = data.get('applicability')
    if not isinstance(applicability, str) or not applicability.strip():
        return '缺少适用条件与限制说明'
    if contains_non_answer_markers(applicability) or detect_reasoning_leak(applicability):
        return '适用条件违反 B19'
    if data.get('review_type') != 'new_experience':
        return '缺少审查类型或审查类型与新经验任务不符'
    refs = data.get('evidence_refs')
    allowed = value_evidence(task)
    if not isinstance(refs, list) or not refs:
        return '缺少对应任务卡材料的证据引用'
    for item in refs:
        ref = item.get('ref') if isinstance(item, dict) else item
        if not isinstance(ref, str) or not ref.strip():
            return '证据引用未对应任务卡材料'
        if contains_non_answer_markers(ref) or detect_reasoning_leak(ref):
            return '证据引用违反 B19'
        if ref not in allowed:
            return '证据引用未对应任务卡材料'
        if isinstance(item, dict):
            sha = item.get('sha256')
            expected_sha = allowed[ref]
            if (not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha)
                    or (expected_sha is not None and sha != expected_sha)):
                return '证据引用哈希与任务卡材料不符'
    return ''


def value_receipt_instruction(task, *, host_review=False):
    if not (expects_value_receipt(task) or (host_review and requires_value_receipt(task))):
        return ''
    return ('价值审查回执 v2：正文须逐行写明审查结论（PASS / FAIL / NEEDS_INFO）、'
            '审查类型: new_experience、评分（0–10）、适用条件与限制、证据引用。'
            '证据必须来自本任务冻结材料，不能用泛称或自行编造。允许的证据引用：' +
            json.dumps(list(value_evidence(task)), ensure_ascii=False))


def decide(reviews, duplicate_of=None):
    if not reviews:
        raise ValueError('新经验缺少独立价值评分')
    scores = {}
    duplicates = [duplicate_of] if duplicate_of else []
    for review in reviews:
        score = review.get('score')
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 10:
            raise ValueError('新经验价值评分必须为0–10的有限数')
        reviewer = review['reviewer_id']
        if reviewer in scores:
            raise ValueError('同一审查员不能重复提供价值评分')
        scores[reviewer] = score
        if review.get('duplicate_of'): duplicates.append(review['duplicate_of'])
    average = sum(scores.values()) / len(scores)
    spread = max(scores.values()) - min(scores.values())
    reason = 'duplicate' if duplicates else 'disagreement' if spread >= 4 else 'average' if average >= 6 else 'below_threshold'
    return {'scores': scores, 'samples': len(scores), 'average': average, 'spread': spread,
            'duplicate_of': sorted(set(duplicates)), 'reason': reason,
            'decision': 'discard' if reason == 'below_threshold' else 'initial_review',
            'human_approval': False}


def from_body(reviewer_id, body):
    """Only explicit receipt fields count; deterministic judge scores do not."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        return {'reviewer_id': reviewer_id, 'score': data.get('score'), 'duplicate_of': data.get('duplicate_of')}
    # 按行读取：空字段不能跨换行吞掉证据引用或正文。页眉和正文同名字段
    # 都保留取证；按值归一后相同算一处，有分歧时不挑其中一处覆盖。
    scores, duplicates = [], []
    field = re.compile(r'[ \t]*(评分|score|重复经验|duplicate_of)[ \t]*[:：][ \t]*(.*?)[ \t]*', re.I)
    for line in (body or '').splitlines():
        match = field.fullmatch(line)
        if not match:
            continue
        name, raw = match.groups()
        if name.casefold() in {'评分', 'score'}:
            try: value = float(raw)
            except ValueError: value = None
            scores.append((value, raw))
        else:
            duplicates.append(None if raw.casefold() in {'', '无', 'none', 'null'} else raw)
    if not scores:
        raise ValueError('新经验回执必须有唯一的0–10价值评分字段')
    score, first_raw = scores[0]
    if any((value != score if value is not None and score is not None else raw != first_raw)
           for value, raw in scores[1:]):
        raise ConflictingValueReceipt('新经验回执评分字段自相矛盾')
    if duplicates and any(value != duplicates[0] for value in duplicates[1:]):
        raise ConflictingValueReceipt('新经验回执重复经验字段自相矛盾')
    return {'reviewer_id': reviewer_id, 'score': score,
            'duplicate_of': duplicates[0] if duplicates else None}


def native_value(db, task_id, host_receipt=None, *, reason_out=None):
    def unscored(reason):
        if reason_out is not None: reason_out.append(reason)
        return None
    from .pool_dispatch import review_vote_runs, needs_review
    from .experience_workflow import policy
    row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if not row or policy(dict(row))['kind'] not in {'method', 'rule'}:
        return unscored('kind_not_method_rule')
    task = dict(row)
    votes = review_vote_runs(db, task_id)
    runs_by_id = {
        str(run["id"]): dict(run)
        for run in db.execute("SELECT * FROM agent_runs WHERE task_id=?", (task_id,))
    }
    pool_by_run = {}
    for event in db.execute(
        "SELECT payload_json FROM events WHERE task_id=? AND event_type='agent.started' ORDER BY sequence",
        (task_id,),
    ):
        try:
            event_payload = json.loads(event['payload_json'])
        except (TypeError, ValueError):
            continue
        if not isinstance(event_payload, dict):
            continue
        execution = event_payload.get('execution')
        pool = execution.get('pool_score') if isinstance(execution, dict) else None
        run_id = event_payload.get('run_id')
        if (isinstance(pool, dict) and isinstance(run_id, str)
                and pool.get('role') in {'review', 'both'}
                and isinstance(pool.get('node_id'), str) and pool['node_id']):
            pool_by_run[run_id] = pool

    from .task_participants import execution_context
    participants = execution_context(None, task, db)
    excluded_ids = set(participants.get('excluded_ids', []))
    from .validation_evidence import current_pool_review_participants
    current_seats = current_pool_review_participants(db, task_id)
    pool_reviews = []
    pool_review_runs = []
    pool_node_ids = set()
    if current_seats:
        for seat in current_seats:
            node_id = seat['node_id']
            run = votes.get(node_id)
            if (run is None or str(run.get('id')) != str(seat['run'].get('id'))
                    or run.get('status') != 'completed' or node_id in excluded_ids):
                return unscored('invalid_score_receipt')
            pool = pool_by_run.get(str(run.get('id')))
            if not pool:
                return unscored('invalid_score_receipt')
            if node_id in pool_node_ids:
                return unscored('non_independent_pool_reviewers')
            pool_node_ids.add(node_id)
            pool_review_runs.append((node_id, run))

    host_event = db.execute(
        "SELECT payload_json FROM events WHERE task_id=? AND event_type='host.receipt_accepted' ORDER BY sequence DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    user_chose_host = False
    accepted_event = None
    if host_receipt is None:
        try:
            accepted_event = json.loads(host_event['payload_json']) if host_event else None
        except (TypeError, ValueError):
            accepted_event = None
        host_receipt = accepted_event.get('receipt') if isinstance(accepted_event, dict) else None
        user_chose_host = bool(accepted_event and accepted_event.get('user_chose_host') is True)
    elif host_event:
        try:
            accepted_event = json.loads(host_event['payload_json'])
        except (TypeError, ValueError):
            accepted_event = None
        user_chose_host = bool(accepted_event and accepted_event.get('user_chose_host') is True)
    if not needs_review(task):
        host_receipt = None

    if host_receipt and isinstance(accepted_event, dict):
        # Host settlement freezes exactly which pool runs were paired with its
        # receipt. This also supports one current pool vote plus the host, while
        # rejecting historical runs that were replaced before settlement.
        run_ids = accepted_event.get('pool_run_ids')
        if not isinstance(run_ids, list) or any(not isinstance(run_id, str) for run_id in run_ids):
            return unscored('invalid_score_receipt')
        pool_review_runs = []
        pool_node_ids = set()
        for run_id in run_ids:
            run = runs_by_id.get(run_id)
            pool = pool_by_run.get(run_id)
            if not run or not pool or run.get('status') != 'completed':
                return unscored('invalid_score_receipt')
            node_id = pool['node_id']
            if node_id in excluded_ids or node_id in pool_node_ids:
                return unscored('non_independent_pool_reviewers')
            pool_node_ids.add(node_id)
            pool_review_runs.append((node_id, run))
    optional_receipts = requires_value_receipt(task) and not (expects_value_receipt(task) or host_receipt)
    for node_id, run in pool_review_runs:
        if optional_receipts and not declares_value_receipt(run['output']):
            continue
        if value_receipt_problem(task, run['output']):
            return unscored('invalid_score_receipt')
        pool_reviews.append(from_body(node_id, run['output']))
    if not pool_reviews and not host_receipt:
        reason = 'host_draft' if row['execution_mode'] == 'host_authored' else 'mock_or_non_pool'
        if optional_receipts or current_seats == []:
            reason = 'no_scoring_source'
        return unscored(reason)

    reviews = list(pool_reviews)
    host_used = False
    if host_receipt:
        if value_receipt_problem(task, host_receipt):
            return unscored('invalid_score_receipt')
        host = participants.get('host')
        host_id = host.get('id') if isinstance(host, dict) else None
        if (not host_id or host_receipt.get('reviewer_id') != host_id
                or host_id in excluded_ids or host_id in pool_node_ids):
            return unscored('host_not_independent')
        verdict = host_receipt.get('verdict')
        if verdict != 'PASS':
            return unscored('host_non_pass')
        score = host_receipt.get('score')
        if (isinstance(score, bool) or type(score) not in (int, float)
                or not math.isfinite(score) or not 0 <= score <= 10):
            return unscored('invalid_score_receipt')
        duplicate = host_receipt.get('duplicate_of')
        if duplicate is not None and not isinstance(duplicate, str):
            return unscored('invalid_score_receipt')
        if not reviews and not user_chose_host:
            return unscored('host_fallback_not_selected')
        reviews.append({'reviewer_id': host_id, 'score': score, 'duplicate_of': duplicate})
        host_used = True

    if len(reviews) < 2 and not (host_used and user_chose_host and not pool_reviews):
        return unscored('requires_two_independent_value_reviewers')
    result = decide(reviews)
    if host_used and not pool_reviews:
        result['review_mode'] = 'host_fallback_no_independent_review'
    elif host_used:
        result['review_mode'] = 'pool_plus_host'
    else:
        result['review_mode'] = 'two_pool'
    return result


def summary(value):
    return ('新经验价值分未达准入线，丢弃候选；仅保留本摘要与各审查员分数。\n' +
            json.dumps(value, ensure_ascii=False, sort_keys=True))


def record_tx(db, task_id, value):
    from .store import EventStore
    from .task_scores import canonical
    if not value: return
    previous = db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.value_assessed' ORDER BY sequence DESC LIMIT 1", (task_id,)).fetchone()
    if previous and json.loads(previous[0]) == value: return
    EventStore._append_event_tx(db, task_id, 'memory.value_assessed', value)


def unscored_reason(db, task_id):
    reasons = []
    native_value(db, task_id, reason_out=reasons)
    return reasons[0] if reasons else None
