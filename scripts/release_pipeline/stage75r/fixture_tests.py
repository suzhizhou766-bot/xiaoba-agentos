"""Synthetic-scenario isolation verification for stage75r. NO production mutation.

Every leaf is exercised against temporary target files / databases and REAL
subprocesses (a fake switch_release.py and a fake detached monitor stand in for the
package command and the monitor service). This proves the tools actually call
commands and parse results — not a FakeRunner returning success — and that wrong
activated / readback writes / exit codes / drift / missing evidence / syntax failure
all refuse. The detached-service test asserts the production monitor PID is
unchanged. Run under the unified interpreter: python3.14 -B fixture_tests.py.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import common
import context_adapter as ca
import monitor_contract
import post_lib
import recovery
import switch_core
from common import PIN, StopError, sha

HERE = Path(__file__).resolve().parent
REAL_POINTER = HERE.parent / 'ATTEMPT-POINTER.json'
REAL_CANDIDATE = Path('/Users/suzhizhou/Documents/小八AgentOS/xiaoba-agentos/development/integrated-candidate')
FOURTEEN = switch_core.FOURTEEN_KEYS

MONITOR_PY = b'"""synthetic monitor runtime file"""\nVALUE = 1\n'
START_CMD = b'#!/bin/bash\necho synthetic-start\n'
CLOSE_CMD = b'#!/bin/bash\necho synthetic-close\n'
CONFIG_PY = (
    'from pathlib import Path\n'
    'import json\n'
    'def load(config_file, overrides=None):\n'
    '    data = json.loads(Path(config_file).read_text(encoding="utf-8"))\n'
    '    root = Path(config_file).resolve().parent\n'
    '    cfg = {"root": str(root), "port": 8799,\n'
    '           "taskcards": {"dirs": ["taskcards"], "ledger": "taskcards/ledger.json",\n'
    '                          "receipt_root": "receipts"}}\n'
    '    cfg.update(data or {})\n'
    '    if overrides:\n'
    '        cfg.update(overrides)\n'
    '    return cfg\n'
    'def resolve(loaded, item):\n'
    '    return Path(loaded["root"]) / item\n'
).encode()

FAKE_SWITCH = '''import argparse, json, os, sys
from pathlib import Path
sc = {}
if os.environ.get("FAKE_SWITCH_SCENARIO"):
    sc = json.loads(Path(os.environ["FAKE_SWITCH_SCENARIO"]).read_text())
ap = argparse.ArgumentParser()
ap.add_argument("mode"); ap.add_argument("--package"); ap.add_argument("--preflight")
ap.add_argument("--old-pid", type=int)
a = ap.parse_args()
if os.environ.get("FAKE_SWITCH_COUNT") and a.mode == os.environ.get("COUNT_MODE"):
    p = Path(os.environ["FAKE_SWITCH_COUNT"])
    modes = json.loads(p.read_text()) if p.exists() else {}
    modes[a.mode] = modes.get(a.mode, 0) + 1
    p.write_text(json.dumps(modes))
r = {"mode": a.mode, "ok": True, "writes_performed": 0}
if a.mode in ("plan", "preflight"):
    r["version"] = sc.get("version", "v1.6.15")
    if a.mode == "preflight":
        r.update(issued_at=1, expires_at=301, preflight_sha256="x")
elif a.mode == "apply":
    r.update(activated=True, old_pid=111, new_pid=222, writes_performed=5,
             completed_steps=["stage_package", "readback"])
elif a.mode == "readback":
    r.update(version=sc.get("version", "v1.6.15"), worker={"pid": sc.get("readback_pid", 222)},
             restart_verified=True, old_pid=a.old_pid)
r.update(sc.get(a.mode, {}))
print(json.dumps(r))
sys.exit(0 if r.get("ok") else 1)
'''


def _w(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode('utf-8')
    path.write_bytes(data)
    return path


def _j(path, value):
    return _w(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def _acceptance_db(path, manifest_sha):
    """Real acceptance SQLite database (modeled on the candidate's own
    tests/test_release.py) so the official validate_acceptance_database gate has
    genuine task/run/judge/pool facts. Two independent pool nodes pass."""
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
    snapshot = json.dumps({'source_manifest_sha256': manifest_sha,
                           'review_context': {'work_task_id': 'work-1', 'release_acceptance': True}})
    conn.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?)',
                 ('task-1', 'completed', 'production', '本回答：小八 AgentOS 双 Agent 裁判\n\n有效结论',
                  json.dumps({'kind': 'agentos_multi_adjudicated', 'minimum_successful_agents': 2}), snapshot))
    conn.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?)',
                 ('work-1', 'completed', 'production', '工作结果', '{}',
                  json.dumps({'prompt': '工作', 'needs_review': False, 'review_context': {'needs_review': False}})))
    for index, name in enumerate(('south-node', 'north-node')):
        conn.execute('INSERT INTO agent_runs VALUES (?,?,?,?,?,?)',
                     ('task-1', name, 'completed', 'FINAL_ACCEPTANCE: PASS\n审查正文', float(index), None))
    conn.execute('INSERT INTO memory_candidates VALUES (?,?,?)', ('task-1', 'pending_delivery_review', 'a' * 64))
    conn.execute('INSERT INTO events VALUES (?,?,?,?)',
                 ('task-1', 'judge.decided', json.dumps({'winner': 'south-node', 'successful_agents': 2}), 1))
    for node_id, entry, model in (('south-node', 'cli:codex', 'alpha'), ('north-node', 'cli:claude', 'beta')):
        conn.execute('INSERT INTO ri_sources VALUES (?,?)', (node_id, json.dumps({
            'reviewer_id': node_id, 'kind': 'review', 'source_ref': 'agent-entry:' + node_id,
            'node': {'enabled': True, 'removed': False, 'interface': 'cli', 'entry': entry,
                     'model': model, 'agent_type': entry.split(':')[-1], 'test': {'status': 'passed'}}},
            ensure_ascii=False)))
    conn.commit()
    conn.close()
    return path


def build_fixture(root, *, bad_syntax=False):
    root = Path(root)
    version = 'v1.6.15'
    round_name = 'v1615-r1'
    work = root / 'work'
    repo = root / 'repo'
    source = root / 'source'
    candidate = REAL_CANDIDATE
    acceptance = root / 'acceptance'
    reports = root / 'reports'
    live = root / 'live'
    live_monitor = root / 'live-monitor'
    package = root / 'releases' / 'v1615-r1-attempt2' / version
    tail = 'monitor'
    repo_monitor = repo / tail
    source_monitor = source / tail
    reports_step4 = reports / 'round1b-attempt2'

    py_bytes = b'def broken(:\n' if bad_syntax else MONITOR_PY
    names = {'xiaoba_monitor.py': py_bytes, '启动监看中心.command': START_CMD,
             '关闭监看中心.command': CLOSE_CMD}
    for d in (repo_monitor, source_monitor, live_monitor):
        for name, data in names.items():
            _w(d / name, data)
    _w(live_monitor / 'monitor_core/config.py', CONFIG_PY)
    _j(live_monitor / 'monitor.json', {'port': 8799})
    _w(source_monitor / 'monitor_core/config.py', CONFIG_PY)
    _j(source_monitor / 'monitor.json', {'port': 8799})

    syntax = [{'file': name, 'path': str(repo_monitor / name),
               'kind': 'python' if name.endswith('.py') else 'shell',
               'sha256': sha(repo_monitor / name)} for name in names]

    manifest_bytes = (candidate / 'SOURCE_MANIFEST.json').read_bytes()
    manifest_sha = sha(candidate / 'SOURCE_MANIFEST.json')

    _w(package / 'agentos/SOURCE_MANIFEST.json', manifest_bytes)
    _w(package / 'agentos/scripts/switch_release.py', FAKE_SWITCH)
    _w(package / 'agentos/scripts/create_manifest.py', 'print("{}")\n')
    _j(package / 'RELEASE.json', {'version': version, 'status': 'verified_immutable_release',
                                  'final_publish_requires_human': True, 'tests': {'result': 'passed'},
                                  'agentos_source_manifest_sha256': manifest_sha,
                                  'release_tier': {'tier': 'full'}})
    # genuine per-file manifest from the FROZEN candidate's own generator, so the
    # trusted per-file integrity gate has real hashes to check (not a fake 'a'*64).
    _w(package / 'FILES.sha256', switch_core._candidate_module(candidate).manifest(package))
    files_sha = sha(package / 'FILES.sha256')

    _j(reports_step4 / 'STEP4-5-result.json', {
        'ok': True, 'production_switch': False, 'step4_passed': True, 'step5_passed': True,
        'fourteen_items': {k: True for k in FOURTEEN}, 'cleanup': {'ok': True},
        'real_workspace_guard': {'ok': True},
        'package': {'path': str(package), 'files_sha256': files_sha,
                    'status': 'verified_immutable_release', 'release_tier': {'tier': 'full'}}})

    # --- shared 75S baseline evidence: four genuine unittest log sets + freeze chain ---
    base_commit = 'b' * 40
    suites = {'core': (10, 0), 'release': (5, 1), 'switch': (3, 0), 'content': (2, 0)}
    expected_suites = {s: {'tests': t, 'skipped': k} for s, (t, k) in suites.items()}
    total_tests = sum(t for t, _ in suites.values())
    runner_sha = sha(HERE.parent / 'driver75p' / 'baseline_runner.py')
    baseline_logs = {}
    freeze_test_logs = {}
    for suite, (tests, skipped) in suites.items():
        fname = 'baseline-%s.log' % suite
        ok_line = 'OK (skipped=%d)' % skipped if skipped else 'OK'
        result_line = json.dumps({'suite': suite, 'tests': tests, 'skipped': skipped,
                                  'ok': True, 'errors': 0, 'failures': 0}, ensure_ascii=False)
        log_path = _w(reports / 'prepare' / fname,
                      'Ran %d tests in 0.05s\n\n%s\n%s\n' % (tests, ok_line, result_line))
        log_sha = sha(log_path)
        baseline_logs[fname] = {'suite': suite, 'tests': tests, 'skipped': skipped, 'ok': True,
                                'exit_code': 0, 'interpreter': str(PIN), 'runner_sha256': runner_sha,
                                'sha256': log_sha}
        freeze_test_logs[fname] = log_sha
    _j(work / 'PREPARATION.json', {'base': base_commit, 'source_manifest_sha256': manifest_sha,
                                   'baseline_logs': baseline_logs, 'tests': total_tests})
    _j(work / 'BASE_READY.json', {'base': base_commit, 'round': round_name,
                                  'preparation_sha256': sha(work / 'PREPARATION.json')})
    _j(acceptance / 'FREEZE.json', {'base': base_commit, 'production_switch': False,
                                    'source_manifest_sha256': manifest_sha, 'tests': total_tests,
                                    'test_logs': freeze_test_logs})
    freeze_sha = sha(acceptance / 'FREEZE.json')
    diff_name = '314b9a0..BASE.diff'
    _w(acceptance / diff_name, 'synthetic diff\n')
    diff_sha = sha(acceptance / diff_name)
    acceptance_db = _acceptance_db(acceptance / 'acceptance.sqlite3', manifest_sha)
    _j(acceptance / 'STATE.json', {'acceptance_passed': True, 'freeze_sha256': freeze_sha,
                                   'source_manifest_sha256': manifest_sha,
                                   'database': str(acceptance_db), 'task_id': 'task-1',
                                   'prompt': str(acceptance / 'prompt.txt'),
                                   'prompt_sha256': 'p' * 64, 'diff': str(acceptance / diff_name),
                                   'diff_sha256': diff_sha, 'tasks': {'review': 'task-1'}})
    review_dir = reports / 'review'
    _j(review_dir / '11-acceptance.json', {'ok': True, 'task_status': 'completed',
                                           'passed_votes': 2, 'failed_votes': 0,
                                           'distinct_agents': ['south-node', 'north-node']})
    for agent in ('south-node', 'north-node'):
        _w(review_dir / ('acceptance-%s.txt' % agent),
           'review body\nFINAL_ACCEPTANCE: PASS\n')

    live_db = live / '.runtime/agentos.sqlite3'
    live_db.parent.mkdir(parents=True, exist_ok=True)
    _w(live / 'SOURCE_MANIFEST.json', manifest_bytes)
    with sqlite3.connect(live_db) as conn:
        conn.execute('PRAGMA user_version=7')
        conn.execute('CREATE TABLE ri_sources (id TEXT PRIMARY KEY, data TEXT)')
        conn.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, attempts INTEGER, last_error TEXT)')
        rows = [
            ('node-cli', {'reviewer_id': 'node-cli', 'kind': 'cli',
                          'node': {'version': 1, 'revision': 3, 'enabled': True, 'removed': False,
                                   'name': 'cli', 'agent': 'cli', 'interface': 'cli', 'entry': 'x',
                                   'model': 'm', 'reasoning_effort': 'high'}}),
            ('node-api', {'reviewer_id': 'node-api', 'kind': 'api',
                          'node': {'version': 1, 'revision': 1, 'enabled': True, 'removed': False,
                                   'name': 'api', 'agent': 'api', 'interface': 'api', 'entry': None,
                                   'model': 'm2', 'reasoning_effort': None}}),
            ('node-removed', {'reviewer_id': 'node-removed', 'kind': 'cli',
                              'node': {'version': 1, 'revision': 0, 'enabled': False, 'removed': True,
                                       'name': 'gone', 'agent': 'gone', 'interface': 'cli',
                                       'entry': None, 'model': None}}),
            ('node-legacy', {'reviewer_id': 'node-legacy', 'kind': 'api', 'node': None}),
        ]
        for row_id, data in rows:
            conn.execute('INSERT INTO ri_sources VALUES (?,?)', (row_id, json.dumps(data)))

    real_pointer = json.loads(REAL_POINTER.read_text())
    pointer = {'prepare_tool': real_pointer['prepare_tool'],
               'prepare_tool_sha256': sha(real_pointer['prepare_tool']),
               'migration_checks': real_pointer['migration_checks'],
               'migration_checks_sha256': sha(real_pointer['migration_checks']),
               'record_path': str(work / 'attempt-record.json')}
    _j(work / 'ATTEMPT-POINTER.json', pointer)
    _j(work / 'migration-spec.json', {'schema_version': 7, 'new_indexes': {}, 'db': str(live_db)})

    tool_a = package / 'agentos/scripts/switch_release.py'
    tool_b = package / 'agentos/scripts/create_manifest.py'
    _j(reports / 'final75/final-tool-hashes.json',
       {str(tool_a.relative_to(root)): sha(tool_a)})
    _j(reports / 'final75/step4-tool-hashes.json',
       {str(tool_b.relative_to(root)): sha(tool_b)})

    record = {'round': round_name, 'version': version, 'attempt': 2,
              'acceptance_step4_dir': str(acceptance / 'step4-attempt2'),
              'reports_step4_dir': str(reports_step4), 'releases_package_dir': str(package),
              'step4_helpers_dir': str(work / 'round1b'), 'syntax': syntax,
              'record_path': str(work / 'attempt-record.json')}
    _j(work / 'attempt-record.json', record)
    assigned = {'round': round_name, 'version': version, 'base': 'b' * 40,
                'diff_base': 'c' * 40, 'source_manifest_sha256': manifest_sha}
    parameters = {'round': round_name, 'version': version, 'release_root': str(root),
                  'post_files': [str(repo_monitor / n) for n in names],
                  'diff': {'repo': str(repo), 'base': 'b' * 40, 'diff_base': 'c' * 40}}
    state = {'round': round_name, 'version': version, 'current_attempt': 2,
             'previous_package': str(root / 'previous'),
             'expected_suites': expected_suites,
             'tool_manifests': [str(reports / 'final75/final-tool-hashes.json'),
                                str(reports / 'final75/step4-tool-hashes.json')]}
    ctx = {'root': root, 'work': work, 'live': live, 'repo': repo, 'source': source,
           'candidate': candidate, 'acceptance': acceptance, 'reports': reports,
           'previous_package': root / 'previous', 'round': round_name, 'version': version,
           'record': record, 'assigned': assigned, 'parameters': parameters, 'state': state,
           'live_monitor_dir': str(live_monitor)}
    paths = {'package': package, 'live_db': live_db, 'live_monitor': live_monitor,
             'source_monitor': source_monitor, 'repo_monitor': repo_monitor,
             'review_dir': review_dir, 'reports_step4': reports_step4, 'work': work}
    return ctx, paths


def _scenario(paths, root, scenario):
    sc = _j(root / 'scenario.json', scenario)
    env = common.clean_env()
    env['FAKE_SWITCH_SCENARIO'] = str(sc)
    return env


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


def main(out_name='isolation', tmp_root=None):
    base = Path(tempfile.mkdtemp(prefix='stage75r-isolation-',
                                 dir=str(tmp_root) if tmp_root else '/private/tmp'))
    print('isolation root: %s' % base, flush=True)
    try:
        run_all(base)
    finally:
        report = {'ok': all(r['ok'] for r in RESULTS), 'total': len(RESULTS),
                  'passed': sum(r['ok'] for r in RESULTS), 'results': RESULTS,
                  'isolation_root': str(base)}
        out = HERE.parent.parent.parent / 'reports/v1615-r1/stage75r' / out_name
        out.mkdir(parents=True, exist_ok=True)
        (out / 'isolation-report.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('SUMMARY %d/%d passed' % (report['passed'], report['total']), flush=True)
    return 0 if all(r['ok'] for r in RESULTS) else 1


def run_all(base):
    # ---- happy-path fixture ----
    root = base / 'happy'
    ctx, paths = build_fixture(root)

    # roster semantics
    nodes = ca.roster(paths['live_db'])
    record('roster_id_equals_reviewer_id', set(nodes) == {'node-cli', 'node-api', 'node-removed', 'node-legacy'})
    record('roster_removed_preserved', nodes['node-removed']['node']['removed'] is True)
    record('roster_legacy_preserved', nodes['node-legacy']['legacy'] is True)
    bad_db = root / 'bad-roster.sqlite3'
    with sqlite3.connect(bad_db) as conn:
        conn.execute('CREATE TABLE ri_sources (id TEXT, data TEXT)')
        conn.execute('INSERT INTO ri_sources VALUES (?,?)',
                     ('id-x', json.dumps({'reviewer_id': 'different'})))
    expect_stop('roster_id_mismatch_refused', lambda: ca.roster(bad_db))

    # migration via pointer-bound r5, schema 7 zero indexes
    mig = ca.run_migration_check(ctx, paths['live_db'])
    record('migration_r5_schema7_zero_indexes', mig.get('ok') and mig['declared_indexes'] == 0,
           mig.get('checks_module'))
    record('migration_loads_r5_not_r4', 'migration_checks-r5.py' in mig['checks_module'])
    wrong_db = root / 'wrong-schema.sqlite3'
    with sqlite3.connect(wrong_db) as conn:
        conn.execute('PRAGMA user_version=6')
    spec_path = paths['work'] / 'migration-spec.json'
    saved_spec = spec_path.read_text()
    _j(spec_path, {'schema_version': 7, 'new_indexes': {}, 'db': str(wrong_db)})
    try:
        ca.run_migration_check(ctx, wrong_db)
        record('migration_wrong_schema_refused', False, 'no error')
    except (StopError, RuntimeError, SystemExit) as exc:
        record('migration_wrong_schema_refused', True, str(exc))
    _w(spec_path, saved_spec)
    tampered = json.loads((paths['work'] / 'ATTEMPT-POINTER.json').read_text())
    tampered['migration_checks_sha256'] = '0' * 64
    _j(paths['work'] / 'ATTEMPT-POINTER.json', tampered)
    expect_stop('migration_pointer_drift_refused', lambda: ca.bound_helpers(ctx))
    _j(paths['work'] / 'ATTEMPT-POINTER.json',
       {**tampered, 'migration_checks_sha256': sha(tampered['migration_checks'])})

    # monitor count computed, not fixed 32
    cmp0 = monitor_contract.compare(paths['source_monitor'], paths['live_monitor'])
    expected_count = len(monitor_contract.runtime_files(paths['source_monitor']))
    record('monitor_count_computed', cmp0['expected_file_count'] == expected_count
           and cmp0['expected_file_count'] != 32, 'count=%d' % expected_count)
    record('monitor_initial_match', cmp0['ok'])

    # syntax gate happy
    checks = switch_core.syntax_gate(ctx)
    record('syntax_gate_three_files', len(checks) == 3 and all(c['ok'] for c in checks))
    expect_stop('syntax_unknown_suffix_refused',
                lambda: post_lib.syntax_check('x.txt', b'data', paths['live_monitor'] / 'x.txt'))
    bad_shell = root / 'bad.command'
    _w(bad_shell, b'#!/bin/bash\nif [ ; then\n')
    expect_stop('syntax_bad_shell_refused',
                lambda: post_lib.syntax_check('bad.command', bad_shell.read_bytes(), bad_shell))

    # full gate_check happy
    gates = switch_core.gate_check(ctx)
    record('gate_check_happy', gates['votes']['reverified_passed'] == ['north-node', 'south-node']
           and gates['votes']['acceptance_database']['qualified_conclusions'] >= 2
           and gates['migration']['ok'])

    # two-vote gate failures
    def mutate_runs(mut):
        p = paths['review_dir'] / '11-acceptance.json'
        data = json.loads(p.read_text())
        mut(data)
        _j(p, data)
    orig_runs = (paths['review_dir'] / '11-acceptance.json').read_text()
    mutate_runs(lambda d: d.update(passed_votes=1))
    expect_stop('two_votes_lt2_refused', lambda: switch_core.verify_two_votes(ctx))
    _w(paths['review_dir'] / '11-acceptance.json', orig_runs)
    mutate_runs(lambda d: d.update(failed_votes=1))
    expect_stop('two_votes_failed_vote_refused', lambda: switch_core.verify_two_votes(ctx))
    _w(paths['review_dir'] / '11-acceptance.json', orig_runs)
    mutate_runs(lambda d: d.update(distinct_agents=['south-node']))
    expect_stop('two_votes_single_agent_refused', lambda: switch_core.verify_two_votes(ctx))
    _w(paths['review_dir'] / '11-acceptance.json', orig_runs)
    _w(paths['review_dir'] / 'acceptance-north-node.txt', 'no marker here\n')
    expect_stop('two_votes_reverify_fail_refused', lambda: switch_core.verify_two_votes(ctx))
    _w(paths['review_dir'] / 'acceptance-north-node.txt', 'review body\nFINAL_ACCEPTANCE: PASS\n')

    # core switch happy (real subprocess against fake switch_release)
    switch_dir = root / 'reports-switch'
    count_file = root / 'switch-count.json'
    env = _scenario(paths, root, {})
    env['FAKE_SWITCH_COUNT'] = str(count_file)
    env['COUNT_MODE'] = 'apply'
    summary = switch_core.core_switch(ctx, switch_dir, env=env)
    record('core_switch_happy', summary['ok'] and summary['activated']
           and summary['new_pid'] == 222 and summary['old_pid'] == 111)
    record('core_switch_real_subprocess_logs',
           (switch_dir / 'plan.json').is_file() and (switch_dir / 'apply.json').is_file()
           and (switch_dir / 'readback.json').is_file())
    record('core_switch_single_marker', (switch_dir / 'SWITCH.started').is_file())
    expect_stop('core_switch_marker_blocks_reapply',
                lambda: switch_core.core_switch(ctx, switch_dir, env=env))

    # recovery after core success runs ONLY post (apply count must stay 1).
    # run_smoke_tests=False is maintenance sync-only: an explicit PARTIAL state,
    # never ok/completed, and supplementable later (card r2 point 2).
    post_dir = root / 'reports-post'
    rep = recovery.recover(ctx, switch_dir, post_dir, env=common.clean_env(), run_smoke_tests=False)
    counts = json.loads(count_file.read_text())
    record('recovery_only_post_no_reapply', counts.get('apply') == 1,
           'apply_calls=%s' % counts.get('apply'))
    record('recovery_no_smoke_is_partial',
           rep['ok'] is False and rep['completed'] is False
           and rep['status'] == 'partial_sync_only' and rep['sync_completed'] is True
           and rep['smoke_pending'] is True and rep['smoke_attempted'] is False and rep['smoke'] is None)
    record('recovery_synced_three', len(rep['sync']) == 3)
    record('recovery_runtime_compare_ok',
           (post_dir / 'runtime-compare.json').is_file()
           and json.loads((post_dir / 'runtime-compare.json').read_text())['ok'])
    rep2 = recovery.post_switch(ctx, switch_dir, post_dir, run_smoke_tests=False)
    record('post_partial_resync_idempotent',
           rep2['ok'] is False and rep2['status'] == 'partial_sync_only'
           and all(r['status'] == 'already_synced' for r in rep2['sync']))
    record('post_partial_no_smoke_attempt_recorded',
           not (switch_dir / 'smoke-attempts.json').exists())

    # missing core evidence cannot recover
    expect_stop('recover_missing_core_refused',
                lambda: recovery.recognize_core_switch(ctx, root / 'no-such-switch'))

    # wrong activated / readback writes / version / exit-code must fail (fresh dirs)
    def core_with_scenario(name, scenario):
        d = root / name
        e = _scenario(paths, root, scenario)
        expect_stop(name + '_refused', lambda: switch_core.core_switch(ctx, d, env=e))
    core_with_scenario('wrong-activated', {'apply': {'ok': False, 'activated': False}})
    core_with_scenario('readback-writes', {'readback': {'writes_performed': 1}})
    core_with_scenario('readback-wrong-version', {'readback': {'version': 'v0.0.0'}})
    core_with_scenario('readback-wrong-pid', {'readback': {'worker': {'pid': 999}}})
    core_with_scenario('apply-exit-fail', {'apply': {'ok': False}})
    core_with_scenario('plan-writes', {'plan': {'writes_performed': 2}})

    # live manifest drift after switch must fail readback gate
    drift_dir = root / 'manifest-drift'
    e = _scenario(paths, root, {})
    saved_manifest = (paths['live'] if 'live' in paths else ctx['live']) / 'SOURCE_MANIFEST.json'
    original = saved_manifest.read_bytes()
    _w(saved_manifest, b'tampered\n')
    expect_stop('live_manifest_drift_refused', lambda: switch_core.core_switch(ctx, drift_dir, env=e))
    _w(saved_manifest, original)

    # three-file syntax failure refuses BEFORE apply (matching record hash, bad python)
    bad_root = base / 'bad-syntax'
    bctx, bpaths = build_fixture(bad_root, bad_syntax=True)
    bswitch = bad_root / 'reports-switch'
    expect_stop('syntax_failure_pre_apply_refused',
                lambda: switch_core.core_switch(bctx, bswitch, env=_scenario(bpaths, bad_root, {})))
    record('syntax_failure_no_marker', not (bswitch / 'SWITCH.started').exists())

    # atomic sync: drift refusal + mode preservation + failed-staging retention
    target = bpaths['live_monitor'] / 'xiaoba_monitor.py'
    before = sha(target)
    expect_stop('atomic_sync_drift_refused',
                lambda: post_lib.atomic_sync(target, b'new', '0' * 64, bad_root / 'sync-out'))
    record('atomic_sync_drift_zero_write', sha(target) == before)
    good_target = paths['live_monitor'] / 'xiaoba_monitor.py'
    good_before = sha(good_target)
    os.chmod(good_target, 0o755)
    rec = post_lib.atomic_sync(good_target, MONITOR_PY, good_before, root / 'sync-out')
    record('atomic_sync_mode_preserved', (good_target.stat().st_mode & 0o777) == 0o755
           and rec['after_sha256'] == sha_bytes_eq(MONITOR_PY))
    # failed-staging retention: force os.replace to fail after staging
    retain_target = paths['live_monitor'] / '启动监看中心.command'
    retain_before = sha(retain_target)
    real_replace = post_lib.os.replace
    calls = {'n': 0}
    def boom(*a, **k):
        calls['n'] += 1
        if calls['n'] == 1:
            raise OSError('simulated replace failure')
        return real_replace(*a, **k)
    post_lib.os.replace = boom
    out_dir = root / 'retain-out'
    try:
        post_lib.atomic_sync(retain_target, START_CMD, retain_before, out_dir)
        record('atomic_sync_failed_staging_retained', False, 'no error raised')
    except OSError:
        pending = out_dir / '_待删'
        retained = pending.is_dir() and any(pending.iterdir()) and (pending / '说明.md').is_file()
        record('atomic_sync_failed_staging_retained', retained and sha(retain_target) == retain_before)
    finally:
        post_lib.os.replace = real_replace

    # taskcards proof before/after equality
    expected_tc, _, _ = post_lib.resolved_taskcards(paths['live_monitor'])
    proof = post_lib.taskcards_proof(paths['live_monitor'], expected_tc)
    record('taskcards_proof_equal', proof['ok'])
    expect_stop('taskcards_proof_drift_refused',
                lambda: post_lib.taskcards_proof(paths['live_monitor'],
                                                 {'dirs': ['/wrong'], 'ledger': '/wrong',
                                                  'receipt_root': '/wrong'}))

    # -O optimization rejected before any write
    q = subprocess.run([str(PIN), '-O', '-B', '-c',
                        'import sys; sys.path.insert(0, %r); import common; common.guard_runtime()'
                        % str(HERE)], capture_output=True, text=True)
    record('optimize_O_rejected', q.returncode != 0 and 'optimization' in (q.stderr + q.stdout),
           q.stderr.strip()[:120])
    q2 = subprocess.run([str(PIN), '-O', '-B', str(HERE / 'cli.py'), 'plan'],
                        capture_output=True, text=True)
    record('cli_optimize_O_rejected', q2.returncode != 0, q2.stderr.strip()[:120])

    # detached temp service must not kill production monitor
    detached_test(base)

    # synthetic smoke: fake HTTP monitor + fake start/close + fake xiaoba entry
    smoke_test(base)


def smoke_test(base):
    import post_smoke
    root = base / 'smoke'
    monitor = root / 'monitor'
    monitor.mkdir(parents=True)
    _w(monitor / 'monitor_core/config.py', CONFIG_PY)

    free = subprocess.run([str(PIN), '-B', '-c',
                           'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); '
                           'print(s.getsockname()[1]); s.close()'], capture_output=True, text=True)
    port = int(free.stdout.strip())
    _j(monitor / 'monitor.json', {'port': port})

    roster_nodes = [
        {'node_id': 'n-cli', 'version': 1, 'revision': 2, 'enabled': True, 'removed': False,
         'name': 'cli', 'agent': 'cli', 'interface': 'cli', 'entry': 'x', 'model': 'm',
         'reasoning_effort': 'high', 'no_progress_seconds': 1800, 'tmux_session': None,
         'working_directory': None, 'context_isolated': False, 'kind': 'cli', 'legacy': False,
         'readiness': {'ready': True}, 'agent_type': 'cli'},
        {'node_id': 'n-api', 'version': 1, 'revision': 1, 'enabled': True, 'removed': False,
         'name': 'api', 'agent': 'api', 'interface': 'api', 'entry': None, 'model': 'm2',
         'reasoning_effort': None, 'no_progress_seconds': 1800, 'tmux_session': None,
         'working_directory': None, 'context_isolated': False, 'kind': 'api', 'legacy': False,
         'readiness': {'ready': True}, 'agent_type': 'api'},
        {'node_id': 'n-tmux', 'version': 1, 'revision': 0, 'enabled': True, 'removed': False,
         'name': 'tmux', 'agent': 'tmux', 'interface': 'tmux', 'entry': None, 'model': None,
         'reasoning_effort': None, 'no_progress_seconds': 1800, 'tmux_session': 's',
         'working_directory': None, 'context_isolated': False, 'kind': 'tmux', 'legacy': False,
         'readiness': {'ready': True}, 'agent_type': 'tmux'},
    ]
    _j(monitor / 'roster.json', roster_nodes)

    server = '''import argparse, json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int); a = ap.parse_args()
here = Path(__file__).resolve().parent
nodes = json.loads((here / "roster.json").read_text())
class H(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        elif self.path == "/api/agent-pool/state":
            self._send({"ok": True, "nodes": nodes})
        elif self.path == "/":
            html = '<html><head><meta name="xiaoba-page-token" content="tok123"></head></html>'
            self.send_response(200); self.end_headers(); self.wfile.write(html.encode())
        else:
            self.send_response(404); self.end_headers()
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0)); self.rfile.read(length)
        if self.path == "/api/agent-pool/test":
            self._send({"ok": True, "result": {"status": "passed", "reason": "synthetic"}})
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", a.port), H).serve_forever()
'''
    server_path = _w(monitor / 'fake_server.py', server)
    (monitor / 'run').mkdir(exist_ok=True)
    (monitor / 'logs').mkdir(exist_ok=True)

    start_sh = _w(monitor / '启动监看中心.command',
                  '#!/bin/bash\nDIR="$(cd "$(dirname "$0")" && pwd)"\n'
                  'nohup "%s" -B "$DIR/fake_server.py" --port %d >> "$DIR/logs/s.log" 2>&1 &\n'
                  'echo $! > "$DIR/run/supervisor.pid"\n'
                  'for _ in $(seq 1 40); do /usr/bin/curl -fsS --max-time 2 '
                  '"http://127.0.0.1:%d/healthz" >/dev/null 2>&1 && break; sleep 0.3; done\n'
                  % (PIN, port, port))
    close_sh = _w(monitor / '关闭监看中心.command',
                  '#!/bin/bash\nDIR="$(cd "$(dirname "$0")" && pwd)"\n'
                  'PID="$(cat "$DIR/run/supervisor.pid" 2>/dev/null)"\n'
                  '[ -n "$PID" ] && kill "$PID" 2>/dev/null\n'
                  'for _ in $(seq 1 30); do kill -0 "$PID" 2>/dev/null || break; sleep 0.2; done\n')

    live_db = root / 'live.sqlite3'
    with sqlite3.connect(live_db) as conn:
        conn.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, attempts INTEGER, last_error TEXT)')
    xiaoba = _w(root / 'xiaoba-entry.py',
                'import sys, json, sqlite3\n'
                'task_id = "synthetic-task-1"\n'
                'with sqlite3.connect(%r) as c:\n'
                '    c.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?,?)",\n'
                '              (task_id, "completed", 1, None))\n'
                'print(json.dumps({"task_id": task_id}))\n' % str(live_db))
    xiaoba_sh = _w(root / 'xiaoba', '#!/bin/bash\nexec "%s" -B "%s" "$@"\n' % (PIN, xiaoba))
    os.chmod(xiaoba_sh, 0o755)

    env = common.clean_env()
    env['XIAOBA_MONITOR_PORT'] = str(port)
    env['XIAOBA_MONITOR_NO_BROWSER'] = '1'
    base_url = 'http://127.0.0.1:%d' % port

    proc = subprocess.Popen([str(PIN), '-B', str(server_path), '--port', str(port)],
                            cwd=str(monitor), env=env)
    (monitor / 'run/supervisor.pid').write_text(str(proc.pid))
    try:
        for _ in range(40):
            try:
                with urllib.request.urlopen(base_url + '/healthz', timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                time.sleep(0.3)
        out = root / 'smoke-out'
        report = post_smoke.run_smoke(base_url=base_url, monitor_dir=monitor, live_db=live_db,
                                      neutral_cwd=root, env=env, output=out,
                                      round_name='v1615-r1', xiaoba_entry=xiaoba_sh)
        record('smoke_ok', report['ok'])
        record('smoke_roster_baseline_api', report['config_baseline']['source'] == 'api')
        record('smoke_config_match', not report['config_mismatch'])
        record('smoke_retest_two_passed',
               sum(1 for t in report['node_tests'] if not t['skipped'] and t['status'] == 'passed') == 2)
        record('smoke_tmux_skipped',
               any(t['skipped'] for t in report['node_tests']))
        record('smoke_default_task_completed',
               report['ordinary_task']['status'] == 'completed')
        record('smoke_no_retry_no_resubmit',
               report['automatic_retry'] is False and report['resubmission'] is False)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        pid_file = monitor / 'run/supervisor.pid'
        if pid_file.is_file():
            try:
                os.kill(int(pid_file.read_text()), 15)
            except (ProcessLookupError, ValueError, OSError):
                pass


def sha_bytes_eq(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def detached_test(base):
    prod_port = 8799
    prod_pid = None
    try:
        r = subprocess.run(['/usr/sbin/lsof', '-nP', '-tiTCP:%d' % prod_port, '-sTCP:LISTEN'],
                           capture_output=True, text=True)
        pids = [int(x) for x in r.stdout.split()]
        prod_pid = pids[0] if pids else None
    except Exception:
        prod_pid = None
    record('production_monitor_observed', prod_pid is not None, 'pid=%s' % prod_pid)

    root = base / 'detached'
    monitor = root / 'monitor'
    monitor.mkdir(parents=True)
    repo_monitor = Path('/Users/suzhizhou/Documents/小八AgentOS/xiaoba-agentos/'
                        'monitor')
    for name in ('启动监看中心.command', '关闭监看中心.command'):
        shutil.copyfile(repo_monitor / name, monitor / name)
    _w(monitor / 'monitor_core/config.py', CONFIG_PY)
    _j(monitor / 'monitor.json', {'port': prod_port})

    free = subprocess.run([str(PIN), '-B', '-c',
                           'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); '
                           'print(s.getsockname()[1]); s.close()'], capture_output=True, text=True)
    temp_port = int(free.stdout.strip())

    fake_monitor = '''import argparse, os, sys, json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
ap = argparse.ArgumentParser()
ap.add_argument("--resolve-port", action="store_true")
ap.add_argument("--supervise", action="store_true")
ap.add_argument("--detach", action="store_true")
ap.add_argument("--port", type=int)
a = ap.parse_args()
here = Path(__file__).resolve().parent
cfg = json.loads((here / "monitor.json").read_text())
port = a.port if a.port is not None else cfg.get("port", 8799)
if a.resolve_port:
    print(port); sys.exit(0)
if a.supervise and a.detach:
    if os.fork() > 0: os._exit(0)
    os.setsid()
    if os.fork() > 0: os._exit(0)
    (here / "run").mkdir(exist_ok=True)
    (here / "run/supervisor.pid").write_text(str(os.getpid()))
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a): pass
    HTTPServer(("127.0.0.1", port), H).serve_forever()
'''
    _w(monitor / 'xiaoba_monitor.py', fake_monitor)

    lsof_wrapper = '''import os, sys
allowed = os.environ["ALLOWED_PORT"]
for arg in sys.argv[1:]:
    if "TCP:" in arg:
        p = arg.split("TCP:")[1].split()[0]
        if p != allowed:
            sys.exit(1)
os.execv("/usr/sbin/lsof", ["/usr/sbin/lsof"] + sys.argv[1:])
'''
    wrapper = _w(root / 'lsof-wrapper.py', lsof_wrapper)
    wrapper_sh = _w(root / 'lsof-wrapper.sh',
                    '#!/bin/bash\nexec "%s" -B "%s" "$@"\n' % (PIN, wrapper))
    os.chmod(wrapper_sh, 0o755)

    env = common.clean_env()
    env['XIAOBA_MONITOR_PORT'] = str(temp_port)
    env['XIAOBA_MONITOR_NO_BROWSER'] = '1'
    env['XIAOBA_LSOF'] = str(wrapper_sh)
    env['ALLOWED_PORT'] = str(temp_port)

    start_log = root / 'start.log'
    with start_log.open('x') as h:
        qs = subprocess.run(['/bin/bash', str(monitor / '启动监看中心.command')],
                            cwd=str(monitor), env=env, stdout=h, stderr=subprocess.STDOUT, timeout=60)
    record('detached_start_exit0', qs.returncode == 0, start_log.read_text()[:120])
    healthy = False
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/healthz' % temp_port, timeout=5) as r:
            healthy = r.status == 200
    except OSError:
        healthy = False
    record('detached_temp_service_listens', healthy, 'port=%d' % temp_port)
    pid_file = monitor / 'run/supervisor.pid'
    temp_pid = int(pid_file.read_text()) if pid_file.is_file() else None
    record('detached_pid_file_written', temp_pid is not None, 'pid=%s' % temp_pid)

    close_log = root / 'close.log'
    with close_log.open('x') as h:
        qc = subprocess.run(['/bin/bash', str(monitor / '关闭监看中心.command')],
                            cwd=str(monitor), env=env, stdout=h, stderr=subprocess.STDOUT, timeout=60)
    record('detached_close_exit0', qc.returncode == 0, close_log.read_text()[:120])
    time.sleep(1)
    temp_gone = True
    try:
        os.kill(temp_pid, 0)
        temp_gone = False
    except (ProcessLookupError, TypeError):
        temp_gone = True
    record('detached_temp_service_stopped', temp_gone)

    if prod_pid is not None:
        alive = True
        try:
            os.kill(prod_pid, 0)
        except ProcessLookupError:
            alive = False
        record('production_monitor_unchanged', alive, 'pid=%s still alive' % prod_pid)
    else:
        record('production_monitor_unchanged', True, 'no production monitor running; nothing to kill')


if __name__ == '__main__':
    common.guard_runtime()
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else 'isolation',
                          sys.argv[2] if len(sys.argv) > 2 else None))