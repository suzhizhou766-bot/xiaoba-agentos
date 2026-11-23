"""75R r2 point-3 isolation: package per-file integrity + shared baseline gate.

Proves the write-before gates refuse BEFORE any SWITCH marker / apply:
  * package per-file integrity reuses the FROZEN candidate's own switch_release
    engine (files/safe/relative/require + official manifest-set comparison). Tampering
    a package script's bytes while FILES.sha256 is unchanged is caught (the package
    never proves itself); unlisted/missing/symlinked files refuse.
  * the shared 75S validate_baseline_evidence gate (four genuine log sets +
    source/BASE/runner-hash/freeze-chain) refuses log drift, runner-hash mismatch,
    freeze-chain mismatch and dynamic suite-count mismatch.
  * a tampered package or baseline yields ZERO marker and ZERO apply via core_switch.

NO production mutation: card-local tmp, fake switch_release subprocess. The real
candidate is read-only. Run: python3.14 -B fixture_tests_r2_package.py.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import common
import switch_core
from common import PIN, StopError, sha
from fixture_tests import REAL_CANDIDATE, _j, _w, build_fixture

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


def _switch_env(root):
    scenario = _j(root / 'scenario.json', {})
    count_file = root / 'switch-count.json'
    env = common.clean_env()
    env['FAKE_SWITCH_SCENARIO'] = str(scenario)
    env['FAKE_SWITCH_COUNT'] = str(count_file)
    env['COUNT_MODE'] = 'apply'
    return env, count_file


def _no_marker_no_apply(switch_dir, count_file):
    return (not (switch_dir / 'SWITCH.started').exists()
            and (not count_file.exists() or json.loads(count_file.read_text()).get('apply', 0) == 0))


def case_package(base):
    # P0 control: untampered fixture activates (proves negatives are due to tamper)
    root = base / 'pkg-happy'
    ctx, paths = build_fixture(root)
    env, count_file = _switch_env(root)
    summary = switch_core.core_switch(ctx, root / 'switch', env=env)
    record('P0_core_switch_happy_activates', summary['ok'] and summary['activated']
           and json.loads(count_file.read_text()).get('apply') == 1)

    root = base / 'pkg-integrity'
    ctx, paths = build_fixture(root)
    integ = switch_core.verify_package_integrity(ctx)
    record('P1_package_integrity_happy', integ['ok'] is True and integ['package_files_verified'] == 4,
           'files_verified=%s' % integ['package_files_verified'])

    # P2/P3: script bytes changed, FILES.sha256 unchanged -> refuse, zero marker/apply
    root = base / 'pkg-tamper'
    ctx, paths = build_fixture(root)
    script = paths['package'] / 'agentos/scripts/switch_release.py'
    files_before = sha(paths['package'] / 'FILES.sha256')
    _w(script, script.read_bytes() + b'\n# tampered bytes, manifest untouched\n')
    record('P2_files_manifest_unchanged', sha(paths['package'] / 'FILES.sha256') == files_before)
    expect_stop('P2_script_tamper_integrity_refused', lambda: switch_core.verify_package_integrity(ctx))
    env, count_file = _switch_env(root)
    switch_dir = root / 'switch'
    expect_stop('P3_script_tamper_core_refused', lambda: switch_core.core_switch(ctx, switch_dir, env=env))
    record('P3_zero_marker_zero_apply', _no_marker_no_apply(switch_dir, count_file))

    # P4: unlisted extra file -> manifest-set mismatch
    root = base / 'pkg-unlisted'
    ctx, paths = build_fixture(root)
    _w(paths['package'] / 'agentos/scripts/extra.py', 'print("unlisted")\n')
    expect_stop('P4_unlisted_file_refused', lambda: switch_core.verify_package_integrity(ctx))

    # P5: listed file missing (manifest lists a ghost) -> manifest-set mismatch
    root = base / 'pkg-missing'
    ctx, paths = build_fixture(root)
    manifest = paths['package'] / 'FILES.sha256'
    _w(manifest, manifest.read_text() + ('0' * 64) + '  ./agentos/scripts/ghost.py\n')
    expect_stop('P5_missing_listed_file_refused', lambda: switch_core.verify_package_integrity(ctx))

    # P6: symlinked package file -> candidate safe() refuses
    root = base / 'pkg-symlink'
    ctx, paths = build_fixture(root)
    target = _w(root / 'outside.py', 'print("outside")\n')
    link = paths['package'] / 'agentos/scripts/link.py'
    os.symlink(target, link)
    manifest = paths['package'] / 'FILES.sha256'
    _w(manifest, manifest.read_text() + sha(target) + '  ./agentos/scripts/link.py\n')
    expect_stop('P6_symlinked_package_file_refused', lambda: switch_core.verify_package_integrity(ctx))


def case_baseline(base):
    root = base / 'base-happy'
    ctx, paths = build_fixture(root)
    baseline = switch_core.validate_baseline(ctx)
    record('P7_baseline_happy', baseline['ok'] is True and baseline['total_tests'] == 20
           and len(baseline['suites']) == 4, 'suites=%s' % sorted(baseline['suites']))

    # P8: baseline log bytes drift -> sha mismatch
    root = base / 'base-log-drift'
    ctx, paths = build_fixture(root)
    log = ctx['reports'] / 'prepare' / 'baseline-core.log'
    _w(log, log.read_text() + 'tampered\n')
    expect_stop('P8_baseline_log_drift_refused', lambda: switch_core.validate_baseline(ctx))

    # P9: runner hash mismatch (keep BASE_READY binding consistent to isolate the check)
    root = base / 'base-runner'
    ctx, paths = build_fixture(root)
    prep_path = paths['work'] / 'PREPARATION.json'
    prep = json.loads(prep_path.read_text())
    first = next(iter(prep['baseline_logs']))
    prep['baseline_logs'][first]['runner_sha256'] = '0' * 64
    _j(prep_path, prep)
    _j(paths['work'] / 'BASE_READY.json',
       {'base': 'b' * 40, 'round': 'v1615-r1', 'preparation_sha256': sha(prep_path)})
    expect_stop('P9_baseline_runner_hash_refused', lambda: switch_core.validate_baseline(ctx))

    # P10: freeze-chain mismatch (FREEZE.test_logs != PREPARATION sha)
    root = base / 'base-freeze'
    ctx, paths = build_fixture(root)
    freeze_path = ctx['acceptance'] / 'FREEZE.json'
    freeze = json.loads(freeze_path.read_text())
    first = next(iter(freeze['test_logs']))
    freeze['test_logs'][first] = '0' * 64
    _j(freeze_path, freeze)
    state_path = ctx['acceptance'] / 'STATE.json'
    state_data = json.loads(state_path.read_text())
    state_data['freeze_sha256'] = sha(freeze_path)
    _j(state_path, state_data)
    expect_stop('P10_baseline_freeze_chain_refused', lambda: switch_core.validate_baseline(ctx))

    # P11: dynamic suite-count mismatch (manager expectation != recorded)
    root = base / 'base-suite-count'
    ctx, paths = build_fixture(root)
    ctx['state']['expected_suites']['core']['tests'] = 999
    expect_stop('P11_baseline_suite_count_refused', lambda: switch_core.validate_baseline(ctx))

    # P12: baseline tamper -> zero marker / zero apply via core_switch
    root = base / 'base-tamper-core'
    ctx, paths = build_fixture(root)
    log = ctx['reports'] / 'prepare' / 'baseline-core.log'
    _w(log, log.read_text() + 'tampered\n')
    env, count_file = _switch_env(root)
    switch_dir = root / 'switch'
    expect_stop('P12_baseline_tamper_core_refused', lambda: switch_core.core_switch(ctx, switch_dir, env=env))
    record('P12_zero_marker_zero_apply', _no_marker_no_apply(switch_dir, count_file))


def main():
    TMP.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix='r2-package-', dir=TMP))
    print('r2 package/baseline isolation root: %s' % base, flush=True)
    case_package(base)
    case_baseline(base)
    report = {'ok': all(r['ok'] for r in RESULTS), 'total': len(RESULTS),
              'passed': sum(r['ok'] for r in RESULTS), 'results': RESULTS,
              'isolation_root': str(base), 'real_candidate': str(REAL_CANDIDATE)}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / 'package-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('SUMMARY %d/%d passed' % (report['passed'], report['total']), flush=True)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    common.guard_runtime()
    sys.exit(main())