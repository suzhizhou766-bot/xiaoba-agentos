"""75R r2.1 isolation: pre-call exclusive smoke claim (minimal, two time points).

The attempt ledger is only written AFTER run_smoke returns, so before this fix two
concurrent posts could both read an empty ledger and repeat node retest/submit, and a
SIGKILL mid-smoke left an empty ledger that a different out_dir could re-enter. This
suite proves the stable switch_dir-anchored O_EXCL claim closes that window:

  (K1) maintenance no-smoke claims NOTHING; the first real smoke claims successfully
       (identity + original out_dir persisted) and completes; the claim is retained.
  (K2) an existing claim with NO ledger result ("call interrupted", original process
       exited) refuses a new post/recover even under a DIFFERENT out_dir, and the real
       run_smoke is never entered (call count stays 0); maintenance is still allowed.
  (K3) two real concurrent posts on the SAME stable switch_dir: exactly one enters
       run_smoke, the other refuses; one attempt recorded.

run_smoke is STUBBED (counting) — no server, no model, no production. Core evidence is
real (switch_core.core_switch against the fake switch_release). Card-local tmp only.
Run: python3.14 -B fixture_tests_r2_claim.py.
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import common
import recovery
import switch_core
from common import StopError
from fixture_tests import REAL_CANDIDATE, _scenario, build_fixture

HERE = Path(__file__).resolve().parent
CARD = Path('/Users/suzhizhou/Documents/私人助理/工作区/小八架构修复-20261030/任务卡/主力接手-v1614-20261111/75R-真实切换与后置恢复工具')
TMP = CARD / 'r2' / 'tmp'
REPORT_DIR = HERE.parent.parent.parent / 'reports/v1615-r1/stage75r/r2'

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


class SmokeStub:
    """Counting stand-in for post_smoke.run_smoke; never touches a server/model."""

    def __init__(self, delay=0.0):
        self.calls = []
        self.delay = delay

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            time.sleep(self.delay)
        return {'ok': True, 'restart': {'stop': True, 'start': True},
                'node_tests': [], 'ordinary_task': {'status': 'completed'}}


def _core(root):
    ctx, paths = build_fixture(root)
    switch_dir = root / 'switch'
    count_file = root / 'switch-count.json'
    env = _scenario(paths, root, {})
    env['FAKE_SWITCH_COUNT'] = str(count_file)
    env['COUNT_MODE'] = 'apply'
    summary = switch_core.core_switch(ctx, switch_dir, env=env)
    record('core_activated_%s' % root.name, summary['ok'] and summary['activated'])
    return ctx, switch_dir


def _patched(stub):
    orig = recovery.run_smoke
    recovery.run_smoke = stub
    return orig


def case_first_claim(base):
    root = base / 'first-claim'
    ctx, switch_dir = _core(root)
    partial = recovery.post_switch(ctx, switch_dir, root / 'partial',
                                   env=common.clean_env(), run_smoke_tests=False)
    record('K1_maintenance_partial', partial['status'] == 'partial_sync_only'
           and partial['smoke_pending'] is True and partial['smoke_attempted'] is False)
    record('K1_maintenance_no_claim',
           not (switch_dir / 'smoke-claim.json').exists()
           and not (switch_dir / 'smoke-attempts.json').exists())
    stub = SmokeStub()
    orig = _patched(stub)
    try:
        out1 = root / 'smoke-out'
        rep = recovery.post_switch(ctx, switch_dir, out1, env=common.clean_env(), run_smoke_tests=True)
    finally:
        recovery.run_smoke = orig
    record('K1_first_smoke_completes', rep['ok'] is True and rep['completed'] is True
           and rep['status'] == 'completed')
    claim = json.loads((switch_dir / 'smoke-claim.json').read_text())
    record('K1_claim_identity', claim['out_dir'] == str(out1) and claim['pid'] == os.getpid()
           and claim['round'] == ctx['round'] and claim['version'] == ctx['version']
           and claim['switch_dir'] == str(switch_dir))
    record('K1_run_smoke_called_once', len(stub.calls) == 1, 'calls=%d' % len(stub.calls))
    ledger = json.loads((switch_dir / 'smoke-attempts.json').read_text())
    record('K1_one_attempt_recorded', len(ledger['attempts']) == 1 and ledger['attempts'][0]['ok'] is True)
    record('K1_claim_retained_after_success', (switch_dir / 'smoke-claim.json').is_file())


def case_interrupted_claim(base):
    root = base / 'interrupted'
    ctx, switch_dir = _core(root)
    # Simulate a prior process that claimed then was SIGKILLed before any ledger write:
    # claim present (different, now-exited pid + original out_dir), ledger still empty.
    prior_out = root / 'original-out'
    prior_out.mkdir(parents=True, exist_ok=True)
    claim_path = switch_dir / 'smoke-claim.json'
    identity = {'round': ctx['round'], 'version': ctx['version'], 'switch_dir': str(switch_dir),
                'out_dir': str(prior_out), 'pid': 999999, 'claimed_at': time.time()}
    fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(identity, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    record('K2_interrupted_state', claim_path.is_file()
           and not (switch_dir / 'smoke-attempts.json').exists())
    stub = SmokeStub()
    orig = _patched(stub)
    try:
        expect_stop('K2_new_outdir_post_refused',
                    lambda: recovery.post_switch(ctx, switch_dir, root / 'launder-1',
                                                 env=common.clean_env(), run_smoke_tests=True))
        expect_stop('K2_other_outdir_recover_refused',
                    lambda: recovery.recover(ctx, switch_dir, root / 'launder-2',
                                             env=common.clean_env(), run_smoke_tests=True))
        maint = recovery.post_switch(ctx, switch_dir, root / 'maint',
                                     env=common.clean_env(), run_smoke_tests=False)
    finally:
        recovery.run_smoke = orig
    record('K2_run_smoke_never_entered', len(stub.calls) == 0, 'calls=%d' % len(stub.calls))
    record('K2_ledger_still_empty', not (switch_dir / 'smoke-attempts.json').exists())
    record('K2_claim_not_deleted', claim_path.is_file())
    record('K2_maintenance_still_partial', maint['status'] == 'partial_sync_only'
           and maint['smoke_pending'] is True)


def case_concurrent_claim(base):
    root = base / 'concurrent'
    ctx, switch_dir = _core(root)
    # Single-threaded maintenance pre-sync so both workers see already_synced (no
    # shared live writes during the race); maintenance does not claim.
    recovery.post_switch(ctx, switch_dir, root / 'pre-sync',
                         env=common.clean_env(), run_smoke_tests=False)
    stub = SmokeStub(delay=0.4)
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(name):
        barrier.wait()
        try:
            rep = recovery.post_switch(ctx, switch_dir, root / name,
                                       env=common.clean_env(), run_smoke_tests=True)
            outcomes.append(('ok', name, rep))
        except StopError as exc:
            outcomes.append(('stop', name, str(exc)))
        except Exception as exc:
            outcomes.append(('error', name, '%s: %s' % (type(exc).__name__, exc)))

    orig = _patched(stub)
    try:
        threads = [threading.Thread(target=worker, args=('outA',)),
                   threading.Thread(target=worker, args=('outB',))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        recovery.run_smoke = orig
    oks = [o for o in outcomes if o[0] == 'ok']
    stops = [o for o in outcomes if o[0] == 'stop']
    errors = [o for o in outcomes if o[0] == 'error']
    record('K3_exactly_one_enters_smoke', len(stub.calls) == 1, 'calls=%d' % len(stub.calls))
    record('K3_one_completes_one_refused', len(oks) == 1 and len(stops) == 1 and not errors,
           'ok=%d stop=%d err=%d' % (len(oks), len(stops), len(errors)))
    ledger_path = switch_dir / 'smoke-attempts.json'
    record('K3_one_attempt_recorded', ledger_path.is_file()
           and len(json.loads(ledger_path.read_text())['attempts']) == 1)
    record('K3_claim_present', (switch_dir / 'smoke-claim.json').is_file())


def main():
    TMP.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix='r2-claim-', dir=TMP))
    print('r2.1 claim isolation root: %s' % base, flush=True)
    case_first_claim(base)
    case_interrupted_claim(base)
    case_concurrent_claim(base)
    report = {'ok': all(r['ok'] for r in RESULTS), 'total': len(RESULTS),
              'passed': sum(r['ok'] for r in RESULTS), 'results': RESULTS,
              'isolation_root': str(base), 'real_candidate': str(REAL_CANDIDATE)}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / 'claim-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('SUMMARY %d/%d passed' % (report['passed'], report['total']), flush=True)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    common.guard_runtime()
    sys.exit(main())