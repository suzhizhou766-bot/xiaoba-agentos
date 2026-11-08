"""New-experience admission: reviewer scores, never a human approval."""
import json
import math
import re


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
        raise ValueError('新经验回执评分字段自相矛盾')
    if duplicates and any(value != duplicates[0] for value in duplicates[1:]):
        raise ValueError('新经验回执重复经验字段自相矛盾')
    return {'reviewer_id': reviewer_id, 'score': score,
            'duplicate_of': duplicates[0] if duplicates else None}


def native_value(db, task_id, host_receipt=None):
    from .pool_dispatch import review_vote_runs,needs_review
    from .experience_workflow import policy
    row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if not row or policy(dict(row))['kind'] not in {'method', 'rule'}:
        return None  # Historical unclassified tasks retain their old contract.
    votes = review_vote_runs(db, task_id)
    if host_receipt is None:
        event = db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='host.receipt_accepted' ORDER BY sequence DESC LIMIT 1", (task_id,)).fetchone()
        host_receipt = json.loads(event[0]).get('receipt') if event else None
    if not needs_review(dict(row)):
        host_receipt = None
    actual_pool = any(json.loads(e[0]).get('execution',{}).get('pool_score')
        for e in db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='agent.started'",(task_id,)))
    if not actual_pool and not host_receipt:
        return None  # Mock-only fixtures and historical non-pool protocols.
    reviews = [from_body(node, run['output']) for node, run in votes.items() if run['status'] == 'completed']
    if host_receipt and 'score' in host_receipt:
        reviews.append({'reviewer_id':host_receipt['reviewer_id'], 'score':host_receipt['score'],
                        'duplicate_of':host_receipt.get('duplicate_of')})
    if not reviews and not votes:
        return None  # Host-authored drafts may collect scores through ingress.
    return decide(reviews)


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
