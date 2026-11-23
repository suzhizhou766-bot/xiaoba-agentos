"""75R r2 point-4 isolation: UI four-asset install + narrow output-key verification.

Proves, against the FROZEN candidate's REAL product functions (no fake shapes):
  * install_ui_assets reuses review_ui_install.run verbatim. The four source-bound
    assets install atomically; a same-hash target is left byte-for-byte untouched
    (no overwrite, inode/mtime preserved); a drifted/missing/symlinked target refuses.
  * the success output keys equal the official run() shape exactly, and the record
    equals a direct official run() call (narrow verification, not a custom shape).
  * validate_acceptance_db's official-sourced keys/values equal the real
    validate_acceptance_database return, and its derived counts recompose from the
    official conclusions/pool_nodes/execution (alignment with 75S STATE/review).
  * the CLI ui-install mode/flags parse correctly (read-only default; --apply gated).

NO production mutation: card-local tmp project/assistant roots, real candidate read-only.
Run: python3.14 -B fixture_tests_r2_ui.py.
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import common
import switch_core
from common import StopError, sha, sha_bytes
from fixture_tests import REAL_CANDIDATE, build_fixture

HERE = Path(__file__).resolve().parent
CARD = Path('/Users/suzhizhou/Documents/私人助理/工作区/小八架构修复-20261030/任务卡/主力接手-v1614-20261111/75R-真实切换与后置恢复工具')
TMP = CARD / 'r2' / 'tmp'
REPORT_DIR = HERE.parent.parent.parent / 'reports/v1615-r1/stage75r/r2'
ASSETS_SRC = REAL_CANDIDATE / 'src/xiaoba_agentos/review_ui_assets'

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


def build_ui_fixture(root, *, changed=False, skip_asset=None, symlink_asset=None):
    root = Path(root)
    manifest = json.loads((ASSETS_SRC / 'manifest.json').read_text())
    project, assistant, runtime = root / 'project', root / 'assistant', root / 'runtime'
    runtime.mkdir(parents=True)
    roots = {'project': project, 'assistant': assistant}
    assets = ASSETS_SRC
    if changed:
        assets = root / 'assets'
        assets.mkdir()
        new_files = []
        for entry in manifest['files']:
            asset_bytes = (ASSETS_SRC / entry['asset']).read_bytes()
            (assets / entry['asset']).write_bytes(asset_bytes)
            old_bytes = ('OLD-PREIMAGE-' + entry['asset']).encode()
            target = roots[entry['base']] / entry['relative']
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry['asset'] != skip_asset:
                target.write_bytes(old_bytes)
            new_files.append({**entry, 'before_sha256': sha_bytes(old_bytes), 'after_sha256': sha_bytes(asset_bytes)})
        manifest = {'version': 1, 'files': new_files}
        (assets / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    else:
        for entry in manifest['files']:
            target = roots[entry['base']] / entry['relative']
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry['asset'] == skip_asset:
                continue
            if entry['asset'] == symlink_asset:
                outside = _w_outside(root, entry['asset'])
                os.symlink(outside, target)
            else:
                target.write_bytes((ASSETS_SRC / entry['asset']).read_bytes())
    return {'assets': assets, 'project': project, 'assistant': assistant, 'runtime': runtime,
            'manifest': manifest, 'roots': roots}


def _w_outside(root, name):
    p = root / ('outside-' + name)
    p.write_bytes(b'outside target bytes\n')
    return p


def _ctx():
    return {'candidate': str(REAL_CANDIDATE), 'live': str(TMP)}


def _install(fx, *, apply):
    return switch_core.install_ui_assets(_ctx(), apply=apply, project_root=fx['project'],
                                         assistant_root=fx['assistant'], runtime_dir=fx['runtime'],
                                         assets=fx['assets'])


def _targets(fx):
    return {e['asset']: fx['roots'][e['base']] / e['relative'] for e in fx['manifest']['files']}


def _stat(path):
    st = path.stat()
    return (st.st_ino, st.st_mtime_ns, path.read_bytes())


def case_ui(base):
    # U0: the real frozen assets match their manifest after-hashes
    manifest = json.loads((ASSETS_SRC / 'manifest.json').read_text())
    record('U0_real_assets_match_manifest',
           len(manifest['files']) == 4
           and all(sha_bytes((ASSETS_SRC / e['asset']).read_bytes()) == e['after_sha256'] for e in manifest['files']))

    # U1: same-hash -> no overwrite (inode/mtime/bytes preserved)
    fx = build_ui_fixture(base / 'ui-same')
    targets = _targets(fx)
    before = {a: _stat(p) for a, p in targets.items()}
    res = _install(fx, apply=True)
    after = {a: _stat(p) for a, p in targets.items()}
    record('U1_same_hash_no_overwrite',
           res['ok'] is True and res['up_to_date'] is True and res['applied'] is True
           and res['restart_monitor_required'] is False
           and all(f['changed'] is False for f in res['files']) and before == after)

    # U2: narrow keys == official run() shape, and record == direct official call
    fx = build_ui_fixture(base / 'ui-narrow')
    mine = _install(fx, apply=True)
    ui = switch_core._ui_install_module(REAL_CANDIDATE)
    config = SimpleNamespace(project_dir=fx['project'], runtime_dir=fx['runtime'],
                             memory={'obsidian_vault': str(fx['assistant'] / '第二大脑')})
    official = ui.run(config, apply=True, assets=fx['assets'])
    record('U2_narrow_keys_match_official',
           set(mine) == {'ok', 'applied', 'up_to_date', 'restart_monitor_required', 'files'}
           and all(set(f) == {'path', 'changed', 'after_sha256'} for f in mine['files'])
           and mine == official)

    # U3: read-only prepare writes nothing
    fx = build_ui_fixture(base / 'ui-readonly')
    targets = _targets(fx)
    before = {a: _stat(p) for a, p in targets.items()}
    res = _install(fx, apply=False)
    record('U3_readonly_prepare_no_write',
           res['applied'] is False and res['up_to_date'] is True
           and before == {a: _stat(p) for a, p in targets.items()})

    # U4: real install (before != after) writes the after bytes, restart required
    fx = build_ui_fixture(base / 'ui-install', changed=True)
    targets = _targets(fx)
    res = _install(fx, apply=True)
    after_bytes = {a: p.read_bytes() for a, p in targets.items()}
    expected = {e['asset']: (fx['assets'] / e['asset']).read_bytes() for e in fx['manifest']['files']}
    record('U4_real_install_writes_after',
           res['ok'] is True and res['restart_monitor_required'] is True
           and all(f['changed'] is True for f in res['files']) and after_bytes == expected)

    # U5: changed but read-only -> up_to_date False, targets stay at old preimage
    fx = build_ui_fixture(base / 'ui-install-readonly', changed=True)
    targets = _targets(fx)
    before = {a: p.read_bytes() for a, p in targets.items()}
    res = _install(fx, apply=False)
    record('U5_changed_readonly_no_write',
           res['applied'] is False and res['up_to_date'] is False
           and all(f['changed'] is True for f in res['files'])
           and before == {a: p.read_bytes() for a, p in targets.items()})

    # U6: drifted target (third hash) refuses
    fx = build_ui_fixture(base / 'ui-drift')
    victim = next(iter(_targets(fx).values()))
    victim.write_bytes(b'drifted third-hash bytes\n')
    expect_stop('U6_drift_target_refused', lambda: _install(fx, apply=True))

    # U7: symlinked target refuses
    fx = build_ui_fixture(base / 'ui-symlink', symlink_asset='server.py')
    expect_stop('U7_symlink_target_refused', lambda: _install(fx, apply=True))

    # U8: missing target refuses (old=None not in {before, after})
    fx = build_ui_fixture(base / 'ui-missing', skip_asset='review_action.py')
    expect_stop('U8_missing_target_refused', lambda: _install(fx, apply=True))


def case_acceptance_narrow(base):
    # U9: validate_acceptance_db official-sourced keys/values == real product return;
    # derived counts recompose from official conclusions/pool_nodes/execution.
    ctx, paths = build_fixture(base / 'acceptance-narrow')
    state = json.loads((ctx['acceptance'] / 'STATE.json').read_text())
    module = switch_core._candidate_module(ctx['candidate'])
    official = module.validate_acceptance_database(Path(state['database']), state['task_id'], Path(ctx['candidate']))
    gate = switch_core.validate_acceptance_db(ctx, state)
    sourced = ['task_id', 'successful_agents', 'minimum_successful_agents', 'source_manifest_sha256',
               'frozen_prompt_sha256', 'database_record_sha256', 'candidate_status']
    passed = [c for c in official['conclusions'] if c.get('passed') is True]
    by_id = {str(n['node_id']): n for n in official['pool_nodes']}
    pool = sum(1 for c in passed if c.get('source') != 'host')
    host = sum(1 for c in passed if c.get('source') == 'host')
    keys = [tuple(by_id[str(c['node_id'])]['independence']) for c in passed if c.get('source') != 'host']
    record('U9_acceptance_db_narrow_keys',
           all(k in official for k in sourced)
           and all(gate[k] == official[k] for k in sourced)
           and gate['qualified_conclusions'] == len(passed)
           and gate['pool_votes'] == pool and gate['host_votes'] == host
           and gate['independence_keys'] == [list(k) for k in keys])


def case_cli_flags(base):
    import cli
    parser = cli.build_parser()
    a = parser.parse_args(['ui-install'])
    b = parser.parse_args(['ui-install', '--apply', '--allow-real'])
    record('U10_cli_ui_install_flags',
           a.command == 'ui-install' and a.apply is False and a.allow_real is False
           and b.command == 'ui-install' and b.apply is True and b.allow_real is True)


def main():
    TMP.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix='r2-ui-', dir=TMP))
    print('r2 UI/narrow isolation root: %s' % base, flush=True)
    case_ui(base)
    case_acceptance_narrow(base)
    case_cli_flags(base)
    report = {'ok': all(r['ok'] for r in RESULTS), 'total': len(RESULTS),
              'passed': sum(r['ok'] for r in RESULTS), 'results': RESULTS,
              'isolation_root': str(base), 'real_candidate': str(REAL_CANDIDATE)}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / 'ui-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('SUMMARY %d/%d passed' % (report['passed'], report['total']), flush=True)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    common.guard_runtime()
    sys.exit(main())