"""75R review-recovery explicit-path contract (dedicated, parameterized wiring only).

75V publishes a NEW qualified two-vote report for the SAME formal task at an explicit
absolute file (STATE.review_evidence_path), keeping the old failure report. This suite
proves context_adapter.review_evidence:

  * default compat: no field -> original reports/review/11-acceptance.json contract;
  * explicit positive: field present -> ONLY that file (non-symlink, regular, strictly
    inside ctx.reports, no escape); review_dir = its parent, runs_path = the file;
  * no search / no guess / no fallback to the old failed report;
  * out-of-bounds / symlink / missing / ..-escape / relative all refuse.

Normal-entry narrow verification: switch_core.verify_two_votes still runs the SAME
official STATE.database/task_id validate_acceptance_database strong gate under the
explicit path — valid JSON counts alone never pass, and the old failed report is not
used. The old 18 real-DB official-validator tests are preserved untouched (not re-run).

No real model/node/switch/recovery; STATE/manager/manifest/formal evidence untouched
(only the synthetic card-local fixture STATE is parameterized). PIN -B, card-new tmp.
Run: python3.14 -B fixture_tests_review_path.py.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import common
import context_adapter
import switch_core
from common import StopError
from fixture_tests import REAL_CANDIDATE, _j, _w, build_fixture

HERE = Path(__file__).resolve().parent
CARD = Path('/Users/suzhizhou/Documents/私人助理/工作区/小八架构修复-20261030/任务卡/主力接手-v1614-20261111/75R-真实切换与后置恢复工具')
EVID = CARD / 'review-path'
TMP = EVID / 'tmp'

RESULTS = []


def record(name, ok, detail=''):
    RESULTS.append({'name': name, 'ok': bool(ok), 'detail': str(detail)[:500]})
    print(('PASS ' if ok else 'FAIL ') + name + (' :: ' + str(detail)[:200] if detail else ''), flush=True)


def expect_stop(name, func, contains=None):
    try:
        func()
    except StopError as exc:
        ok = contains is None or contains in str(exc)
        record(name, ok, str(exc))
        return
    except Exception as exc:
        record(name, False, 'wrong exception %s: %s' % (type(exc).__name__, exc))
        return
    record(name, False, 'expected StopError but call succeeded')


def _set_state(ctx, **updates):
    sp = Path(ctx['acceptance']) / 'STATE.json'
    state = json.loads(sp.read_text())
    state.update(updates)
    _j(sp, state)
    return state


def _make_recovery(ctx, dirname='review-recovery1'):
    rec = Path(ctx['reports']) / dirname
    rec.mkdir(parents=True, exist_ok=True)
    _j(rec / '11-acceptance.json', {'ok': True, 'task_status': 'completed',
                                    'passed_votes': 2, 'failed_votes': 0,
                                    'distinct_agents': ['south-node', 'north-node']})
    for agent in ('south-node', 'north-node'):
        _w(rec / ('acceptance-%s.txt' % agent), 'review body\nFINAL_ACCEPTANCE: PASS\n')
    return rec / '11-acceptance.json'


def case_path_contract(base):
    ctx, paths = build_fixture(base / 'contract')
    reports = Path(ctx['reports'])
    old_runs = reports / 'review' / '11-acceptance.json'

    # P1 default compat: no explicit field -> original contract
    ev = context_adapter.review_evidence(ctx)
    record('P1_default_contract', ev['review_dir'] == reports / 'review'
           and ev['runs_path'] == old_runs and 'review_evidence_path' not in ev['state'])

    # P2 explicit positive: only the explicit file; review_dir = parent, runs_path = file
    rec_file = _make_recovery(ctx)
    _set_state(ctx, review_evidence_path=str(rec_file))
    ev = context_adapter.review_evidence(ctx)
    record('P2_explicit_positive', ev['runs_path'] == rec_file and ev['review_dir'] == rec_file.parent)
    # P3 no fallback / no guess: old report still present, explicit wins
    record('P3_no_fallback_to_old', ev['runs_path'] != old_runs and old_runs.is_file()
           and ev['runs_path'] == rec_file)

    # P4 out-of-bounds (absolute file outside ctx.reports)
    outside = Path(ctx['root']) / 'outside.json'
    _j(outside, {'ok': True})
    _set_state(ctx, review_evidence_path=str(outside))
    expect_stop('P4_out_of_bounds_refused', lambda: context_adapter.review_evidence(ctx), 'escapes ctx.reports')

    # P5 symlinked target inside reports
    link = reports / 'review-recovery1' / 'link-acceptance.json'
    os.symlink(rec_file, link)
    _set_state(ctx, review_evidence_path=str(link))
    expect_stop('P5_symlink_refused', lambda: context_adapter.review_evidence(ctx), 'missing or symlinked')

    # P6 missing file
    _set_state(ctx, review_evidence_path=str(reports / 'review-recovery1' / 'nope.json'))
    expect_stop('P6_missing_refused', lambda: context_adapter.review_evidence(ctx), 'missing or symlinked')

    # P7 ..-escape that resolves outside reports (file exists, containment fails)
    escape = Path(ctx['root']) / 'escape.json'
    _j(escape, {'ok': True})
    _set_state(ctx, review_evidence_path=str(reports / 'review' / '..' / '..' / 'escape.json'))
    expect_stop('P7_dotdot_escape_refused', lambda: context_adapter.review_evidence(ctx), 'escapes ctx.reports')

    # P8 relative path refused (must be absolute; no guessing)
    _set_state(ctx, review_evidence_path='review-recovery1/11-acceptance.json')
    expect_stop('P8_relative_refused', lambda: context_adapter.review_evidence(ctx), 'absolute')


def case_normal_entry(base):
    ctx, paths = build_fixture(base / 'normal')
    reports = Path(ctx['reports'])
    old_runs = reports / 'review' / '11-acceptance.json'
    orig_database = json.loads((Path(ctx['acceptance']) / 'STATE.json').read_text())['database']

    # N1 default entry passes the official DB gate (original contract)
    res = switch_core.verify_two_votes(ctx)
    record('N1_default_official_gate_pass', res['reverified_passed'] == ['north-node', 'south-node']
           and res['acceptance_database']['qualified_conclusions'] >= 2 and res['passed_votes'] == 2)

    # N2 explicit entry: old report present but FAILED; new qualified recovery report used.
    old_data = json.loads(old_runs.read_text())
    old_data['passed_votes'] = 1
    _j(old_runs, old_data)
    rec_file = _make_recovery(ctx)
    _set_state(ctx, review_evidence_path=str(rec_file))
    res = switch_core.verify_two_votes(ctx)
    record('N2_explicit_uses_recovery_not_old',
           res['reverified_passed'] == ['north-node', 'south-node']
           and res['acceptance_database']['qualified_conclusions'] >= 2
           and res['acceptance_database']['task_id'] == 'task-1')

    # N3 explicit + valid JSON counts but DB unavailable -> official gate still refuses
    _set_state(ctx, database=str(Path(ctx['acceptance']) / 'no-such.sqlite3'))
    expect_stop('N3_explicit_db_gate_still_enforced', lambda: switch_core.verify_two_votes(ctx),
                'official acceptance database gate')
    _set_state(ctx, database=orig_database)

    # N4 explicit + valid DB but tampered JSON counts -> count gate still refuses
    rec_data = json.loads(rec_file.read_text())
    rec_data['passed_votes'] = 1
    _j(rec_file, rec_data)
    expect_stop('N4_explicit_count_gate_still_enforced', lambda: switch_core.verify_two_votes(ctx),
                'two-vote count gate')


def main():
    TMP.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix='review-path-', dir=TMP))
    print('review-path isolation root: %s' % base, flush=True)
    case_path_contract(base)
    case_normal_entry(base)
    report = {'ok': all(r['ok'] for r in RESULTS), 'total': len(RESULTS),
              'passed': sum(r['ok'] for r in RESULTS), 'results': RESULTS,
              'isolation_root': str(base), 'real_candidate': str(REAL_CANDIDATE)}
    EVID.mkdir(parents=True, exist_ok=True)
    (EVID / 'review-path-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('SUMMARY %d/%d passed' % (report['passed'], report['total']), flush=True)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    common.guard_runtime()
    sys.exit(main())