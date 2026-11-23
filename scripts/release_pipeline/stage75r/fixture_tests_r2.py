"""75R r2 point-1 isolation: the official acceptance-database two-vote gate.

Proves the revised switch_core.verify_two_votes reuses the FROZEN candidate's own
build_release.validate_acceptance_database (real product function, real src/config/
SOURCE_MANIFEST) against a real acceptance SQLite database, and refuses — before any
SWITCH marker or apply — when the database/task/snapshot is missing, when there is a
single vote, when two names share one entry+model, on FAIL / non-answer, on a
non-production task, or on a wrong frozen-source binding. The lead's exact
"two names, no database" counterexample files are replayed read-only against the
revised code and must now refuse.

NO production mutation. All fixtures live under the card r2/tmp. The real candidate
is read-only. Run: python3.14 -B fixture_tests_r2.py  (PYTHONOPTIMIZE cleared).
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import switch_core
from common import StopError, sha

HERE = Path(__file__).resolve().parent
REAL_CANDIDATE = Path('/Users/suzhizhou/Documents/小八AgentOS/xiaoba-agentos/development/integrated-candidate')
CARD = Path('/Users/suzhizhou/Documents/私人助理/工作区/小八架构修复-20261030/任务卡/主力接手-v1614-20261111/75R-真实切换与后置恢复工具')
LEAD_COUNTEREXAMPLE = CARD / '主力-两名字无审查数据库反例'
TMP = CARD / 'r2' / 'tmp'
REPORT_DIR = HERE.parent.parent.parent / 'reports/v1615-r1/stage75r/r2'

MANIFEST_SHA = sha(REAL_CANDIDATE / 'SOURCE_MANIFEST.json')
RESULT_LABEL = '本回答：小八 AgentOS 双 Agent 裁判\n\n有效结论'
DEFAULT_NODES = [('south-node', 'cli:codex', 'alpha'), ('north-node', 'cli:claude', 'beta')]

RESULTS = []


def record(name, ok, detail=''):
    RESULTS.append({'name': name, 'ok': bool(ok), 'detail': str(detail)[:500]})
    print(('PASS ' if ok else 'FAIL ') + name + (' :: ' + str(detail)[:200] if detail else ''), flush=True)


def expect_stop(name, func):
    try:
        func()
    except StopError as exc:
        record(name, True, str(exc))
        return
    except Exception as exc:
        record(name, False, 'wrong exception %s: %s' % (type(exc).__name__, exc))
        return
    record(name, False, 'expected StopError but call succeeded')


def _w(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode('utf-8') if isinstance(data, str) else data)
    return path


def _j(path, value):
    return _w(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def make_db(path, *, nodes=None, runs=None, task_id='task-1', task_status='completed',
            execution_mode='production', snapshot_manifest=None, include_judge=True,
            judge_payload=None, include_candidate=True, include_work_task=True,
            result=RESULT_LABEL):
    """Build a real acceptance SQLite database modeled on the candidate's own
    tests/test_release.py fixture. Only the schema/rows the official validator
    reads are created; nothing is fabricated beyond the synthetic vote facts."""
    nodes = DEFAULT_NODES if nodes is None else nodes
    if runs is None:
        runs = [(name, 'completed', 'FINAL_ACCEPTANCE: PASS\n审查正文') for name, _, _ in nodes]
    snapshot_manifest = MANIFEST_SHA if snapshot_manifest is None else snapshot_manifest
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE tasks (id TEXT, status TEXT, execution_mode TEXT, result TEXT,
            provenance_json TEXT, execution_snapshot_json TEXT);
        CREATE TABLE agent_runs (task_id TEXT, agent_name TEXT, status TEXT, output TEXT,
            created_at REAL, failure_class TEXT);
        CREATE TABLE memory_candidates (task_id TEXT, status TEXT, evidence_sha256 TEXT);
        CREATE TABLE events (task_id TEXT, event_type TEXT, payload_json TEXT, sequence INTEGER);
        CREATE TABLE agent_health (agent_name TEXT, status TEXT, provider TEXT, model TEXT, last_probe_at REAL);
        CREATE TABLE ri_sources (id TEXT PRIMARY KEY, data TEXT NOT NULL);
    """)
    snapshot = json.dumps({'source_manifest_sha256': snapshot_manifest,
                           'review_context': {'work_task_id': 'work-1', 'release_acceptance': True}})
    provenance = json.dumps({'kind': 'agentos_multi_adjudicated', 'minimum_successful_agents': 2})
    conn.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?)',
                 (task_id, task_status, execution_mode, result, provenance, snapshot))
    if include_work_task:
        conn.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?)',
                     ('work-1', 'completed', 'production', '工作结果', '{}',
                      json.dumps({'prompt': '工作', 'needs_review': False,
                                  'review_context': {'needs_review': False}})))
    for index, (name, status, output) in enumerate(runs):
        conn.execute('INSERT INTO agent_runs VALUES (?,?,?,?,?,?)',
                     (task_id, name, status, output, float(index), None))
    if include_candidate:
        conn.execute('INSERT INTO memory_candidates VALUES (?,?,?)',
                     (task_id, 'pending_delivery_review', 'a' * 64))
    if include_judge:
        payload = judge_payload if judge_payload is not None else {
            'winner': runs[0][0] if runs else 'south-node', 'successful_agents': 2}
        conn.execute('INSERT INTO events VALUES (?,?,?,?)',
                     (task_id, 'judge.decided', json.dumps(payload), 1))
    for node_id, entry, model in nodes:
        conn.execute('INSERT INTO ri_sources VALUES (?,?)', (node_id, json.dumps({
            'reviewer_id': node_id, 'kind': 'review', 'source_ref': 'agent-entry:' + node_id,
            'node': {'enabled': True, 'removed': False, 'interface': 'cli', 'entry': entry,
                     'model': model, 'agent_type': entry.split(':')[-1],
                     'test': {'status': 'passed'}}}, ensure_ascii=False)))
    conn.commit()
    conn.close()
    return path


def build_vote_ctx(root, db_path, *, distinct=None, state_overrides=None, runs_overrides=None):
    """Minimal ctx focused on verify_two_votes: real candidate as frozen source,
    card-local acceptance STATE/FREEZE + reports/review summary and per-agent txt."""
    root = Path(root)
    acceptance = root / 'acceptance'
    reports = root / 'reports'
    review_dir = reports / 'review'
    distinct = ['south-node', 'north-node'] if distinct is None else distinct
    _j(acceptance / 'FREEZE.json', {'base': 'b' * 40, 'production_switch': False})
    freeze_sha = sha(acceptance / 'FREEZE.json')
    state = {'acceptance_passed': True, 'freeze_sha256': freeze_sha,
             'source_manifest_sha256': MANIFEST_SHA,
             'database': str(db_path), 'task_id': 'task-1'}
    state.update(state_overrides or {})
    _j(acceptance / 'STATE.json', state)
    runs = {'ok': True, 'task_status': 'completed', 'passed_votes': 2, 'failed_votes': 0,
            'distinct_agents': list(distinct)}
    runs.update(runs_overrides or {})
    _j(review_dir / '11-acceptance.json', runs)
    for agent in distinct:
        _w(review_dir / ('acceptance-%s.txt' % agent), '审查正文\nFINAL_ACCEPTANCE: PASS\n')
    return {'candidate': REAL_CANDIDATE, 'acceptance': acceptance, 'reports': reports,
            'assigned': {'source_manifest_sha256': MANIFEST_SHA}}


def run_all():
    TMP.mkdir(parents=True, exist_ok=True)

    # ---- positive: a real, valid acceptance database is accepted ----
    pos_root = Path(tempfile.mkdtemp(prefix='pos-', dir=TMP))
    pos_db = make_db(pos_root / 'acceptance.sqlite3')
    ctx = build_vote_ctx(pos_root, pos_db)
    votes = switch_core.verify_two_votes(ctx)
    gate = votes['acceptance_database']
    record('official_db_positive_accepted',
           votes['reverified_passed'] == ['north-node', 'south-node']
           and gate['qualified_conclusions'] >= 2 and gate['pool_votes'] >= 1
           and gate['minimum_successful_agents'] == 2
           and len({tuple(k) for k in gate['independence_keys']}) == len(gate['independence_keys'])
           and bool(gate['database_record_sha256']) and bool(gate['frozen_prompt_sha256'])
           and gate['source_manifest_sha256'] == MANIFEST_SHA,
           'qualified=%s pool=%s keys=%s' % (gate['qualified_conclusions'], gate['pool_votes'],
                                            gate['independence_keys']))

    # ---- the lead's EXACT counterexample, replayed read-only against revised code ----
    ce_ctx = {'candidate': REAL_CANDIDATE,
              'acceptance': LEAD_COUNTEREXAMPLE / 'acceptance',
              'reports': LEAD_COUNTEREXAMPLE / 'reports',
              'assigned': {'source_manifest_sha256': MANIFEST_SHA}}
    expect_stop('lead_counterexample_two_names_no_db_refused',
                lambda: switch_core.verify_two_votes(ce_ctx))

    # ---- missing database file (STATE points at a non-existent DB) ----
    miss_root = Path(tempfile.mkdtemp(prefix='miss-', dir=TMP))
    miss_ctx = build_vote_ctx(miss_root, miss_root / 'missing.sqlite3')
    expect_stop('missing_database_refused', lambda: switch_core.verify_two_votes(miss_ctx))

    # ---- STATE lacks database / task_id entirely ----
    nofield_root = Path(tempfile.mkdtemp(prefix='nofield-', dir=TMP))
    nofield_db = make_db(nofield_root / 'acceptance.sqlite3')
    nofield_ctx = build_vote_ctx(nofield_root, nofield_db, state_overrides={'database': '', 'task_id': ''})
    expect_stop('state_missing_db_task_fields_refused', lambda: switch_core.verify_two_votes(nofield_ctx))

    # ---- same entry + model under two different names (not independent) ----
    same_root = Path(tempfile.mkdtemp(prefix='same-', dir=TMP))
    same_db = make_db(same_root / 'acceptance.sqlite3',
                      nodes=[('south-node', 'cli:codex', 'alpha'), ('north-node', 'cli:codex', 'alpha')])
    same_ctx = build_vote_ctx(same_root, same_db)
    expect_stop('same_entry_model_two_names_refused', lambda: switch_core.verify_two_votes(same_ctx))

    # ---- only one successful vote ----
    one_root = Path(tempfile.mkdtemp(prefix='one-', dir=TMP))
    one_db = make_db(one_root / 'acceptance.sqlite3',
                     nodes=[('south-node', 'cli:codex', 'alpha')],
                     runs=[('south-node', 'completed', 'FINAL_ACCEPTANCE: PASS\nA')])
    one_ctx = build_vote_ctx(one_root, one_db, distinct=['south-node', 'north-node'])
    expect_stop('single_vote_refused', lambda: switch_core.verify_two_votes(one_ctx))

    # ---- a FAIL verdict among the runs ----
    fail_root = Path(tempfile.mkdtemp(prefix='fail-', dir=TMP))
    fail_db = make_db(fail_root / 'acceptance.sqlite3',
                      runs=[('south-node', 'completed', 'FINAL_ACCEPTANCE: PASS\nA'),
                            ('north-node', 'completed', 'FINAL_ACCEPTANCE: FAIL\nB')])
    fail_ctx = build_vote_ctx(fail_root, fail_db)
    expect_stop('fail_verdict_refused', lambda: switch_core.verify_two_votes(fail_ctx))

    # ---- non-answer (unexecuted tool markup) in a vote ----
    na_root = Path(tempfile.mkdtemp(prefix='nonanswer-', dir=TMP))
    na_db = make_db(na_root / 'acceptance.sqlite3',
                    runs=[('south-node', 'completed', 'FINAL_ACCEPTANCE: PASS\nA'),
                          ('north-node', 'completed', 'FINAL_ACCEPTANCE: PASS\n<invoke name="x">')])
    na_ctx = build_vote_ctx(na_root, na_db)
    expect_stop('non_answer_markup_refused', lambda: switch_core.verify_two_votes(na_ctx))

    # ---- task id absent from the database ----
    notask_root = Path(tempfile.mkdtemp(prefix='notask-', dir=TMP))
    notask_db = make_db(notask_root / 'acceptance.sqlite3')
    notask_ctx = build_vote_ctx(notask_root, notask_db, state_overrides={'task_id': 'absent-task'})
    expect_stop('missing_task_refused', lambda: switch_core.verify_two_votes(notask_ctx))

    # ---- non-production execution mode ----
    nonprod_root = Path(tempfile.mkdtemp(prefix='nonprod-', dir=TMP))
    nonprod_db = make_db(nonprod_root / 'acceptance.sqlite3', execution_mode='candidate')
    nonprod_ctx = build_vote_ctx(nonprod_root, nonprod_db)
    expect_stop('non_production_task_refused', lambda: switch_core.verify_two_votes(nonprod_ctx))

    # ---- snapshot bound to a different source manifest ----
    wrongman_root = Path(tempfile.mkdtemp(prefix='wrongman-', dir=TMP))
    wrongman_db = make_db(wrongman_root / 'acceptance.sqlite3', snapshot_manifest='0' * 64)
    wrongman_ctx = build_vote_ctx(wrongman_root, wrongman_db)
    expect_stop('wrong_frozen_source_binding_refused', lambda: switch_core.verify_two_votes(wrongman_ctx))

    # ---- no judge decision in the database ----
    nojudge_root = Path(tempfile.mkdtemp(prefix='nojudge-', dir=TMP))
    nojudge_db = make_db(nojudge_root / 'acceptance.sqlite3', include_judge=False)
    nojudge_ctx = build_vote_ctx(nojudge_root, nojudge_db)
    expect_stop('no_judge_decision_refused', lambda: switch_core.verify_two_votes(nojudge_ctx))

    # ---- task not completed ----
    incomplete_root = Path(tempfile.mkdtemp(prefix='incomplete-', dir=TMP))
    incomplete_db = make_db(incomplete_root / 'acceptance.sqlite3', task_status='pending')
    incomplete_ctx = build_vote_ctx(incomplete_root, incomplete_db)
    expect_stop('task_not_completed_refused', lambda: switch_core.verify_two_votes(incomplete_ctx))

    # ---- lightweight summary checks still fire (rewritable summary not trusted) ----
    sum_root = Path(tempfile.mkdtemp(prefix='summary-', dir=TMP))
    sum_db = make_db(sum_root / 'acceptance.sqlite3')
    expect_stop('summary_passed_votes_lt2_refused',
                lambda: switch_core.verify_two_votes(build_vote_ctx(sum_root, sum_db, runs_overrides={'passed_votes': 1})))
    expect_stop('summary_failed_vote_refused',
                lambda: switch_core.verify_two_votes(build_vote_ctx(sum_root, sum_db, runs_overrides={'failed_votes': 1})))
    expect_stop('summary_single_distinct_agent_refused',
                lambda: switch_core.verify_two_votes(build_vote_ctx(sum_root, sum_db, distinct=['south-node'])))
    expect_stop('state_acceptance_not_passed_refused',
                lambda: switch_core.verify_two_votes(build_vote_ctx(sum_root, sum_db, state_overrides={'acceptance_passed': False})))
    expect_stop('state_manifest_differs_from_assigned_refused',
                lambda: switch_core.verify_two_votes(build_vote_ctx(sum_root, sum_db, state_overrides={'source_manifest_sha256': '0' * 64})))


def main():
    print('r2 acceptance-gate isolation; real candidate=%s' % REAL_CANDIDATE, flush=True)
    print('frozen candidate SOURCE_MANIFEST sha256=%s' % MANIFEST_SHA, flush=True)
    run_all()
    report = {'ok': all(r['ok'] for r in RESULTS), 'total': len(RESULTS),
              'passed': sum(r['ok'] for r in RESULTS), 'results': RESULTS,
              'real_candidate': str(REAL_CANDIDATE), 'frozen_manifest_sha256': MANIFEST_SHA,
              'counterexample_replayed': str(LEAD_COUNTEREXAMPLE)}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / 'acceptance-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('SUMMARY %d/%d passed' % (report['passed'], report['total']), flush=True)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())