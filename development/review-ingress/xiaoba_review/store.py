"""Transactional review requests and receipts attached to native experience cards.

All writes use the native database and append native hash-chain events. Additive
tables are owned by this extension; native schema version and approvals stay put.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import re
from pathlib import Path
import sqlite3
import time
import uuid

from xiaoba_agentos.output_hygiene import detect_reasoning_leak, contains_non_answer_markers


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identifier():
    return str(uuid.uuid4())


class Conflict(ValueError):
    pass


class ReviewStore:
    def __init__(self, native_store, outbox_root):
        self.native = native_store
        self.path = native_store.path
        self.outbox_root = Path(outbox_root).resolve()
        with self.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS ri_version (version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO ri_version VALUES (1);
                CREATE TABLE IF NOT EXISTS ri_sources (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ri_requests (
                    id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    common TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ri_assignments (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL, role TEXT NOT NULL,
                    source_id TEXT NOT NULL, active INTEGER NOT NULL, card TEXT NOT NULL,
                    running TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS ri_active_role ON ri_assignments(request_id,role) WHERE active=1;
                CREATE TABLE IF NOT EXISTS ri_receipts (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL, assignment_id TEXT NOT NULL,
                    data TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL,
                    reason TEXT NOT NULL, attestation TEXT);
                CREATE TABLE IF NOT EXISTS ri_outbox (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL, data TEXT NOT NULL,
                    digest TEXT NOT NULL, status TEXT NOT NULL, error TEXT);
                CREATE TABLE IF NOT EXISTS ri_inbox (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL, data TEXT NOT NULL,
                    digest TEXT NOT NULL, current INTEGER NOT NULL, ack_at REAL NOT NULL);
            ''')

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            if not self.native.verify_event_chain().get('ok'):
                raise ValueError('原生审计事件链损坏，停止双审接入写入')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, db, request_id, kind, data):
        row = db.execute('SELECT candidate_id FROM ri_requests WHERE id=?', (request_id,)).fetchone()
        self.native._append_event_tx(db, row['candidate_id'] if row else None,
                                     'open_review.' + kind, {'request_id': request_id, **data})

    def subject(self, db, candidate_id):
        row = db.execute('SELECT * FROM memory_candidates WHERE task_id=?', (candidate_id,)).fetchone()
        if row is None:
            raise ValueError('找不到原经验卡')
        if row['status'] not in {'pending_delivery_review', 'revision_requested', 'delivery_approved',
                                  'pending_final_review', 'revalidation_required', 'publication_failed'}:
            raise ValueError('原卡已结束，不能补审')
        path = Path(row['staged_path']).resolve(strict=True)
        path.relative_to(self.outbox_root)
        body = path.read_bytes()
        if len(body) > 1024 * 1024 or hashlib.sha256(body).hexdigest() != row['evidence_sha256']:
            raise ValueError('原卡内容与哈希不一致')
        review = db.execute("SELECT id,decision,candidate_sha256 FROM memory_reviews WHERE task_id=? AND stage='delivery' ORDER BY created_at DESC,rowid DESC LIMIT 1", (candidate_id,)).fetchone()
        return {'candidate_id': candidate_id, 'candidate_sha256': row['evidence_sha256'],
                'delivery_review': dict(review) if review else None, 'body': body.decode('utf-8')}

    def source(self, db, source_id):
        row = db.execute('SELECT data FROM ri_sources WHERE id=?', (source_id,)).fetchone()
        if row is None:
            raise ValueError('未知审查来源')
        return json.loads(row['data'])

    def register_source(self, data):
        for key in ('reviewer_id', 'source_ref', 'provenance'):
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise ValueError('来源缺少 ' + key)
        if data.get('kind') not in {'dsh', 'peer'}:
            raise ValueError('来源类型必须为 dsh 或 peer')
        clean = {key: data[key] for key in ('reviewer_id', 'source_ref', 'provenance', 'kind')}
        with self.transaction() as db:
            existing = db.execute('SELECT data FROM ri_sources WHERE id=?', (clean['reviewer_id'],)).fetchone()
            if existing and json.loads(existing['data']) != clean:
                raise Conflict('来源身份不可原地改写，请登记新身份')
            db.execute('INSERT OR IGNORE INTO ri_sources VALUES (?,?)', (clean['reviewer_id'], canonical(clean)))
            if not existing:
                self.native._append_event_tx(db, None, 'open_review.source_registered', clean)
        return clean

    def assignment(self, db, request, role, source_id, channel):
        source = self.source(db, source_id)
        if role == 'dsh' and source['kind'] != 'dsh':
            raise ValueError('固定审查方必须是已登记 DSH 来源')
        if channel not in {'external_handoff', 'backend_adapter'}:
            raise ValueError('未知接入通道')
        common = json.loads(request['common'])
        card = {'assignment_id': identifier(), 'review_request_id': request['id'],
                'plan_revision': request['revision'], 'reviewer_role': role,
                'reviewer_id': source_id, 'source_ref': source['source_ref'],
                'channel': channel, 'input_digest': digest(common), **common,
                'receipt_contract': {'version': 1, 'verdicts': ['PASS', 'FAIL', 'NEEDS_INFO'],
                    'required': ['receipt_id', 'card_digest', 'review_request_id', 'assignment_id', 'reviewer_role', 'plan_revision',
                                 'reviewer_id', 'source_ref', 'input_digest', 'candidate_sha256',
                                 'verdict', 'findings', 'evidence_refs', 'started_at', 'finished_at']}}
        card['card_digest'] = digest(card)
        db.execute('INSERT INTO ri_assignments VALUES (?,?,?,?,1,?,NULL)',
                   (card['assignment_id'], request['id'], role, source_id, canonical(card)))
        return card

    def create(self, candidate_id, dsh_id, peer_id, scope, evidence, request_id=None,
               dsh_channel='external_handoff', peer_channel='external_handoff'):
        if not isinstance(scope, str) or not scope.strip() or not isinstance(evidence, list) or not evidence:
            raise ValueError('需要审查范围和证据引用')
        if any(not isinstance(ref, dict) or not isinstance(ref.get('ref'), str) or not ref['ref'].strip()
               or not isinstance(ref.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', ref['sha256']) for ref in evidence):
            raise ValueError('每份证据需要非空 ref 和 SHA-256；摘要只绑定材料，不证明实际执行')
        request_id = request_id or identifier()
        with self.transaction() as db:
            subject = self.subject(db, candidate_id)
            dsh, peer = self.source(db, dsh_id), self.source(db, peer_id)
            if dsh['source_ref'] == peer['source_ref']:
                raise ValueError('两方实际来源相同，别名不能凑独立意见')
            common = {**subject, 'scope': scope, 'evidence_refs': evidence}
            fingerprint = digest([common, dsh_id, peer_id, dsh_channel, peer_channel])
            existing = db.execute('SELECT * FROM ri_requests WHERE id=?', (request_id,)).fetchone()
            if existing:
                if existing['fingerprint'] != fingerprint:
                    raise Conflict('同一请求 ID 的内容冲突')
            else:
                db.execute('INSERT INTO ri_requests VALUES (?,?,1,?,?,?)',
                           (request_id, candidate_id, canonical(common), fingerprint, 'waiting_receipts'))
                row = db.execute('SELECT * FROM ri_requests WHERE id=?', (request_id,)).fetchone()
                self.assignment(db, row, 'dsh', dsh_id, dsh_channel)
                self.assignment(db, row, 'peer', peer_id, peer_channel)
                self.event(db, request_id, 'created', {'revision': 1})
        return self.get(request_id)

    def fresh(self, db, request):
        common = json.loads(request['common'])
        try:
            current = self.subject(db, request['candidate_id'])
        except (ValueError, OSError):
            return False
        return all(current[key] == common[key] for key in ('candidate_sha256', 'delivery_review', 'body'))

    def refresh(self, db, request):
        if not self.fresh(db, request):
            if request['state'] != 'stale':
                self.event(db, request['id'], 'binding_invalidated', {'reason': '原卡内容、状态或初审版本变化'})
            db.execute("UPDATE ri_requests SET state='stale' WHERE id=?", (request['id'],))
            db.execute('UPDATE ri_inbox SET current=0 WHERE request_id=?', (request['id'],))
            db.execute("UPDATE ri_outbox SET status='stale' WHERE request_id=?", (request['id'],))
            return False
        return True

    def receive(self, data):
        if not isinstance(data, dict) or not isinstance(data.get('receipt_id'), str) or not data['receipt_id']:
            raise ValueError('需要稳定回执 ID')
        checksum = digest(data)
        with self.transaction() as db:
            old = db.execute('SELECT * FROM ri_receipts WHERE id=?', (data['receipt_id'],)).fetchone()
            if old:
                if old['digest'] != checksum:
                    raise Conflict('同 ID 不同回执内容')
                return dict(old)
            assignment = db.execute('SELECT * FROM ri_assignments WHERE id=?', (data.get('assignment_id'),)).fetchone()
            if assignment is None:
                raise ValueError('找不到任务卡；未关联回执不写入经验')
            request = db.execute('SELECT * FROM ri_requests WHERE id=?', (assignment['request_id'],)).fetchone()
            card = json.loads(assignment['card'])
            reason = ''
            for key in ('review_request_id', 'assignment_id', 'reviewer_role', 'plan_revision', 'reviewer_id', 'source_ref',
                        'candidate_sha256', 'input_digest', 'card_digest'):
                if data.get(key) != card[key]:
                    reason = '任务卡关联不符：' + key
                    break
            text = data.get('findings')
            if not reason and (data.get('verdict') not in {'PASS', 'FAIL', 'NEEDS_INFO'}
                               or not isinstance(text, str) or not text.strip()
                               or contains_non_answer_markers(text) or detect_reasoning_leak(text)
                               or not isinstance(data.get('evidence_refs'), list) or not data['evidence_refs']):
                reason = '缺少有效审查正文、结论或证据，或违反 B19'
            start, finish = data.get('started_at'), data.get('finished_at')
            if not reason and (any(type(x) not in (int, float) or not math.isfinite(x) for x in (start, finish))
                               or finish < start or start <= 0 or finish > time.time() + 300):
                reason = '执行时间无效'
            if not assignment['active'] or not self.refresh(db, request):
                reason = '任务卡已被替换或原卡/初审版本失效'
            # Public imports NEVER trust a payload claiming verified identity.
            status = 'invalid' if reason else 'identity_pending'
            db.execute('INSERT INTO ri_receipts VALUES (?,?,?,?,?,?,?,NULL)',
                       (data['receipt_id'], request['id'], assignment['id'], canonical(data), checksum, status, reason))
            self.event(db, request['id'], 'receipt_received', {'receipt_id': data['receipt_id'], 'status': status, 'reason': reason})
            self.prepare(db, request)
        return {'id': data['receipt_id'], 'digest': checksum, 'status': status, 'reason': reason}

    def attest(self, receipt_id, expected_digest, note):
        if not isinstance(note, str) or not note.strip():
            raise ValueError('请记录实际来源核对或交接依据')
        with self.transaction() as db:
            row = db.execute('SELECT * FROM ri_receipts WHERE id=?', (receipt_id,)).fetchone()
            if row is None or row['digest'] != expected_digest:
                raise Conflict('回执不存在或版本变化')
            request = db.execute('SELECT * FROM ri_requests WHERE id=?', (row['request_id'],)).fetchone()
            assignment = db.execute('SELECT * FROM ri_assignments WHERE id=?', (row['assignment_id'],)).fetchone()
            if row['status'] == 'invalid' or not assignment['active'] or not self.refresh(db, request):
                raise ValueError('失效回执不能确认来源')
            if row['status'] != 'valid':
                db.execute("UPDATE ri_receipts SET status='valid',attestation=? WHERE id=?", (note, receipt_id))
                self.event(db, request['id'], 'source_attested', {'receipt_id': receipt_id, 'basis': note})
            self.prepare(db, request)
        return self.get(row['request_id'])

    def prepare(self, db, request):
        if not self.refresh(db, request):
            return
        assignments = db.execute('SELECT * FROM ri_assignments WHERE request_id=? AND active=1 ORDER BY role', (request['id'],)).fetchall()
        receipts = []
        for assignment in assignments:
            # The newest receipt supersedes prior success, including invalid/incomplete imports.
            receipt = db.execute('SELECT * FROM ri_receipts WHERE assignment_id=? ORDER BY rowid DESC LIMIT 1', (assignment['id'],)).fetchone()
            if not receipt or receipt['status'] != 'valid':
                db.execute('UPDATE ri_inbox SET current=0 WHERE request_id=?', (request['id'],))
                db.execute("UPDATE ri_outbox SET status='superseded' WHERE request_id=?", (request['id'],))
                db.execute("UPDATE ri_requests SET state='waiting_receipts' WHERE id=?", (request['id'],))
                return
            receipts.append({'receipt': json.loads(receipt['data']), 'digest': receipt['digest'],
                             'identity_basis': receipt['attestation']})
        if len(assignments) != 2 or {a['role'] for a in assignments} != {'dsh', 'peer'}:
            return
        if len({self.source(db, a['source_id'])['source_ref'] for a in assignments}) != 2:
            raise ValueError('实际来源不独立')
        prior_failures = [json.loads(r['data']) for r in db.execute(
            "SELECT data FROM ri_receipts WHERE request_id=? AND status='valid' ORDER BY rowid", (request['id'],))
            if json.loads(r['data']).get('verdict') == 'FAIL']
        packet = {'review_request_id': request['id'], 'plan_revision': request['revision'],
                  'subject': json.loads(request['common']), 'reviews': receipts,
                  'prior_failures': prior_failures,
                  'all_pass': all(r['receipt']['verdict'] == 'PASS' for r in receipts) and not prior_failures,
                  'approval': 'human_required', 'native_validation_gate': 'unchanged'}
        checksum = digest(packet)
        # Receipt-version changes supersede old packets, retaining their full history.
        packet_id = request['id'] + ':' + checksum
        db.execute('UPDATE ri_inbox SET current=0 WHERE request_id=? AND id!=?', (request['id'], packet_id))
        db.execute("UPDATE ri_outbox SET status='superseded' WHERE request_id=? AND id!=?", (request['id'], packet_id))
        db.execute('INSERT OR IGNORE INTO ri_outbox VALUES (?,?,?,?,?,NULL)', (packet_id, request['id'], canonical(packet), checksum, 'pending'))
        status = db.execute('SELECT status FROM ri_outbox WHERE id=?', (packet_id,)).fetchone()['status']
        db.execute('UPDATE ri_requests SET state=? WHERE id=?',
                   ('submitted' if status == 'acknowledged' else 'pending_center', request['id']))

    def replace_peer(self, request_id, source_id, reason, channel='external_handoff'):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('需要替换原因')
        with self.transaction() as db:
            request = db.execute('SELECT * FROM ri_requests WHERE id=?', (request_id,)).fetchone()
            if request is None or not self.refresh(db, request):
                raise ValueError('请求不存在或已失效')
            old = db.execute("SELECT * FROM ri_assignments WHERE request_id=? AND role='peer' AND active=1", (request_id,)).fetchone()
            if old['running']:
                raise Conflict('后台仍执行中，不能无痕换人；先等待执行结束')
            dsh = db.execute("SELECT source_id FROM ri_assignments WHERE request_id=? AND role='dsh' AND active=1", (request_id,)).fetchone()
            if self.source(db, source_id)['source_ref'] == self.source(db, dsh['source_id'])['source_ref']:
                raise ValueError('实际来源不独立')
            db.execute('UPDATE ri_assignments SET active=0 WHERE id=?', (old['id'],))
            db.execute("UPDATE ri_requests SET revision=revision+1,state='waiting_receipts' WHERE id=?", (request_id,))
            db.execute('UPDATE ri_inbox SET current=0 WHERE request_id=?', (request_id,))
            db.execute("UPDATE ri_outbox SET status='superseded' WHERE request_id=?", (request_id,))
            request = db.execute('SELECT * FROM ri_requests WHERE id=?', (request_id,)).fetchone()
            card = self.assignment(db, request, 'peer', source_id, channel)
            self.event(db, request_id, 'peer_replaced', {'old_assignment': old['id'], 'new_assignment': card['assignment_id'], 'reason': reason})
        return self.get(request_id)

    def deliver(self, inbox):
        """Durable outbox retry: no rerunning reviewers and no premature ACK."""
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM ri_outbox WHERE status='pending'").fetchall()
        for row in rows:
            with self.transaction() as db:
                request = db.execute('SELECT * FROM ri_requests WHERE id=?', (row['request_id'],)).fetchone()
                if not self.refresh(db, request):
                    continue
                current = db.execute('SELECT status FROM ri_outbox WHERE id=?', (row['id'],)).fetchone()
                if current['status'] != 'pending':
                    continue
                db.execute('SAVEPOINT inbox_delivery')
                try:
                    # The inbox consumer uses the same transaction, serializing
                    # replacement/invalidation with ACK and original-card binding.
                    inbox.accept(db, dict(row))
                except (OSError, ValueError) as exc:
                    db.execute('ROLLBACK TO inbox_delivery')
                    db.execute('RELEASE inbox_delivery')
                    db.execute('UPDATE ri_outbox SET error=? WHERE id=?', (str(exc), row['id']))
                    continue
                db.execute('RELEASE inbox_delivery')
                db.execute("UPDATE ri_outbox SET status='acknowledged',error=NULL WHERE id=?", (row['id'],))
                db.execute("UPDATE ri_requests SET state='submitted' WHERE id=?", (row['request_id'],))
                self.event(db, row['request_id'], 'center_acknowledged', {'packet_id': row['id']})

    def get(self, request_id):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM ri_requests WHERE id=?', (request_id,)).fetchone()
            if row is None:
                raise ValueError('找不到审查请求')
            if self.refresh(db, row):
                self.prepare(db, row)
            row = dict(db.execute('SELECT * FROM ri_requests WHERE id=?', (request_id,)).fetchone())
            row['subject'] = json.loads(row.pop('common'))
            row['assignments'] = [dict(r) for r in db.execute('SELECT * FROM ri_assignments WHERE request_id=? ORDER BY rowid', (request_id,))]
            for item in row['assignments']:
                item['card'] = json.loads(item['card'])
            row['receipts'] = [dict(r) for r in db.execute('SELECT * FROM ri_receipts WHERE request_id=? ORDER BY rowid', (request_id,))]
            for receipt in row['receipts']:
                receipt['data'] = json.loads(receipt['data'])
            row['deliveries'] = [dict(r) for r in db.execute('SELECT id,status,error FROM ri_outbox WHERE request_id=?', (request_id,))]
            row['history'] = [dict(r) for r in db.execute("SELECT event_type,payload_json,created_at FROM events WHERE task_id=? AND event_type LIKE 'open_review.%' ORDER BY sequence", (row['candidate_id'],)) if json.loads(r['payload_json']).get('request_id') == request_id]
            return row

    def state(self):
        with self.transaction() as db:
            ids = [r['id'] for r in db.execute('SELECT id FROM ri_requests ORDER BY rowid DESC')]
            sources = [json.loads(r['data']) for r in db.execute('SELECT data FROM ri_sources')]
        requests = [self.get(key) for key in ids]
        with self.transaction() as db:
            packets = [dict(r) for r in db.execute('SELECT * FROM ri_inbox ORDER BY ack_at DESC')]
            for packet in packets:
                packet['data'] = json.loads(packet['data'])
        return {'sources': sources, 'requests': requests, 'decision_packets': packets}


class DecisionInbox:
    """The decision-center extension inbox; displaying materials never approves."""
    def __init__(self, online=True):
        self.online = online

    def accept(self, db, row):
        if not self.online:
            raise OSError('裁决材料中心离线，等待重试')
        if digest(json.loads(row['data'])) != row['digest']:
            raise ValueError('裁决材料摘要不一致')
        existing = db.execute('SELECT digest FROM ri_inbox WHERE id=?', (row['id'],)).fetchone()
        if existing and existing['digest'] != row['digest']:
            raise Conflict('裁决材料 ID 冲突')
        db.execute('INSERT OR IGNORE INTO ri_inbox VALUES (?,?,?,?,1,?)',
                   (row['id'], row['request_id'], row['data'], row['digest'], time.time()))
