"""Post-switch smoke leaf: restart via the synced start/close scripts, verify the
whole roster against the captured pre-restart baseline, retest every enabled
non-tmux node, then submit one release smoke task with default routing and verify
audit-only settlement.

Ported by purpose from the accepted v1.6.14 post_switch_smoke.py (e5f672af…).
Differences required by the 75R card:
  * base_url / monitor_dir / live_db / xiaoba_entry / neutral_cwd are PARAMETERS,
    never a hardcoded 8799 or production path; the caller resolves them from the
    live config + environment (XIAOBA_MONITOR_PORT) exactly like the start script.
  * expectations come from the ACTUAL pre-restart roster (API node_id first,
    read-only ri_sources reviewer_id fallback preserving removed/legacy), never a
    fixed reviewer/model/effort; model & reasoning_effort stay opaque data.
  * every refusal is an explicit StopError, not an assert (safe under -O too).
This leaf only verifies; it never edits nodes, retries a retest, or resubmits.
"""
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
import urllib.request
from contextlib import closing
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from common import StopError, atomic_dump, sha
from context_adapter import roster

CONFIG_FIELDS = ('version', 'revision', 'enabled', 'removed', 'name', 'agent', 'interface', 'entry',
                 'model', 'reasoning_effort', 'no_progress_seconds', 'tmux_session',
                 'working_directory', 'context_isolated')


def node_config(node, kind=None, legacy=False):
    config = {k: node.get(k) for k in CONFIG_FIELDS}
    config['kind'] = node.get('kind') if kind is None else kind
    config['legacy'] = legacy
    return config


def resolve_base_url(monitor_dir, env):
    """Mirror the start script port precedence: XIAOBA_MONITOR_PORT > monitor.json.
    Uses the LIVE config loader so the resolved port is the one the script binds."""
    from post_lib import _load_live_config
    module, _ = _load_live_config(monitor_dir)
    overrides = None
    if env.get('XIAOBA_MONITOR_PORT'):
        overrides = {'port': int(env['XIAOBA_MONITOR_PORT'])}
    config = module.load(Path(monitor_dir) / 'monitor.json', overrides)
    port = config['port']
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise StopError('STOP: resolved monitor port invalid: %r' % port)
    return 'http://127.0.0.1:%d' % port


def load_lifecycle(path):
    """Load the verified monitor implementation without starting its entry point."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise StopError('STOP: monitor lifecycle module is missing or symlinked: %s' % path)
    spec = importlib.util.spec_from_file_location('_stage75r_monitor_lifecycle', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_restart_before_claim(monitor_dir, base_url, env):
    """Resolve known restart blockers before taking the one-use smoke claim."""
    monitor_dir = Path(monitor_dir)
    module_path = monitor_dir / 'monitor_core/lifecycle.py'
    if not module_path.exists() and not module_path.is_symlink():
        return
    lifecycle = load_lifecycle(module_path)
    run_dir = monitor_dir / 'run'
    port = urlsplit(base_url).port
    claim = lifecycle.read_claim(run_dir)
    if claim is not None:
        kind, detail = lifecycle.classify_claim(claim, port, monitor_dir)
        if kind not in (lifecycle.KIND_MATCH, lifecycle.KIND_STALE):
            raise StopError('STOP: monitor claim is unresolved before smoke: %s' % detail)
    else:
        kind = lifecycle.KIND_STALE
        legacy = lifecycle.inspect_legacy_pid_file(run_dir, monitor_dir, port)
        if legacy['status'] != 'none':
            raise StopError('STOP: complete the legacy monitor handoff before smoke: %s' % legacy['status'])
    probe = lifecycle.probe_port_status(port, lsof_bin=env.get('XIAOBA_LSOF'))
    if probe['status'] not in (lifecycle.PORT_FREE, lifecycle.PORT_BUSY):
        raise StopError('STOP: cannot verify monitor port before smoke: %s' % probe['detail'])
    if kind == lifecycle.KIND_STALE and probe['status'] != lifecycle.PORT_FREE:
        raise StopError('STOP: a listener has no verified monitor claim before smoke')


def read_close_report(log):
    with Path(log).open(encoding='utf-8') as stream:
        try:
            value = json.loads(stream.readline())
        except ValueError as exc:
            raise StopError('STOP: close script did not output a monitor close report') from exc
    if not isinstance(value, dict) or value.get('schema') != 'monitor_close_report_v1':
        raise StopError('STOP: unsupported monitor close report')
    return value


def baseline_nodes(base_url, live_db):
    """Pre-restart node config: live API first (node_id), read-only ri_sources
    fallback (reviewer_id == SQL id). Removed/legacy semantics preserved."""
    try:
        with urllib.request.urlopen(base_url + '/api/agent-pool/state', timeout=5) as response:
            state = json.loads(response.read())
        if state.get('ok') is not True:
            raise StopError('STOP: agent-pool state not ok before restart')
        nodes = {v['node_id']: node_config(v, legacy=bool(v.get('legacy'))) for v in state['nodes']}
        return 'api', nodes, state
    except (OSError, ValueError):
        nodes = {}
        for row_id, entry in roster(live_db).items():
            nodes[row_id] = node_config(entry['node'], kind=entry['kind'], legacy=entry['legacy'])
        return 'database', nodes, {'ok': True, 'source': 'ri_sources_read_only', 'node_count': len(nodes)}


class _Token(HTMLParser):
    value = None

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == 'meta' and attrs.get('name') == 'xiaoba-page-token':
            self.value = attrs.get('content')


def _verify_audit_settlement(db, task_id, snapshot_json, provenance_json, result):
    snapshot = json.loads(snapshot_json or '{}')
    if (snapshot.get('task_purpose') != 'release_smoke'
            or snapshot.get('experience_policy', {}).get('kind') != 'audit'):
        raise StopError('STOP: the task did not freeze release_smoke with audit policy')

    provenance = json.loads(provenance_json or '{}')
    audits = db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='audit.settled'",
                        (task_id,)).fetchall()
    result_sha = hashlib.sha256(result.encode()).hexdigest() if isinstance(result, str) else None
    if (not result_sha or provenance.get('memory_disposition') != 'audit_only'
            or provenance.get('result_sha256') != result_sha or len(audits) != 1
            or json.loads(audits[0][0]).get('result_sha256') != result_sha):
        raise StopError('STOP: expected one audit-only settlement bound to the smoke task result')

    candidate = db.execute('SELECT 1 FROM memory_candidates WHERE task_id=?', (task_id,)).fetchone()
    candidate_event = db.execute(
        "SELECT 1 FROM events WHERE task_id=? "
        "AND event_type IN ('memory.candidate_staged','memory.value_unscored','memory.value_assessed') LIMIT 1",
        (task_id,)).fetchone()
    if candidate or candidate_event:
        raise StopError('STOP: release smoke unexpectedly produced experience candidate evidence')
    return {'memory_disposition': 'audit_only', 'audit_settlements': 1, 'result_sha256': result_sha,
            'memory_candidate_count': 0}


def run_smoke(*, base_url, monitor_dir, live_db, neutral_cwd, env, output, round_name, xiaoba_entry):
    monitor_dir = Path(monitor_dir)
    live_db = Path(live_db)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    report = {'ok': False, 'base_url': base_url, 'restart': {}, 'config_baseline': None,
              'config_verified': [], 'effort_verified': [], 'config_mismatch': {},
              'disabled_cli_blockers': [], 'node_tests': [], 'ordinary_task': None,
              'automatic_retry': False, 'resubmission': False}
    report_path = output / 'post-switch-smoke.json'

    def save():
        atomic_dump(report_path, report, overwrite=True)

    def get(route):
        with urllib.request.urlopen(base_url + route, timeout=5) as response:
            return response.read()

    try:
        baseline_source, expected, snapshot = baseline_nodes(base_url, live_db)
        atomic_dump(output / 'agent-pool-before-restart.json', snapshot)
        report['config_baseline'] = {'source': baseline_source, 'nodes': len(expected),
                                     'snapshot': 'agent-pool-before-restart.json',
                                     'captured_before_restart': True}
        save()

        for label, name in (('stop', '关闭监看中心.command'), ('start', '启动监看中心.command')):
            script = monitor_dir / name
            if script.is_symlink() or not script.is_file():
                raise StopError('STOP: synced monitor script missing or symlinked: %s' % script)
            log = output / ('monitor-%s.log' % label)
            if log.exists():
                raise StopError('STOP: restart log already exists; preserve evidence: %s' % log)
            with log.open('x', encoding='utf-8') as handle:
                q = subprocess.run(['/bin/bash', str(script)], cwd=str(monitor_dir), env=env,
                                   stdout=handle, stderr=subprocess.STDOUT, timeout=120)
            report['restart'][label] = {'exit_code': q.returncode, 'script': str(script)}
            if label == 'stop' and (monitor_dir / 'monitor_core/lifecycle.py').is_file():
                closed = read_close_report(log)
                report['restart'][label]['close_report'] = closed
                if (closed.get('closed_ok') is not True
                        or (closed.get('port_probe') or {}).get('status') != 'free'):
                    save()
                    raise StopError('STOP: monitor close report did not confirm shutdown')
            save()
            if q.returncode != 0:
                raise StopError('STOP: monitor %s script failed (exit %d)' % (label, q.returncode))
            if label == 'stop':
                closed = False
                try:
                    get('/healthz')
                except OSError:
                    closed = True
                report['restart']['closed_confirmed'] = closed
                save()
                if not closed:
                    raise StopError('STOP: old monitor still answers after close script')

        probe = subprocess.run(
            [sys.executable, '-B', '-c',
             'import urllib.request; r=urllib.request.urlopen(%r, timeout=5); print(r.status)'
             % (base_url + '/healthz')],
            capture_output=True, text=True, timeout=30, env=env)
        report['restart']['healthz_new_process'] = {'exit': probe.returncode,
                                                    'stdout': probe.stdout.strip(),
                                                    'stderr': probe.stderr.strip()}
        save()
        if probe.returncode != 0 or probe.stdout.strip() != '200':
            raise StopError('STOP: monitor healthz not 200 in a new process')

        deadline = time.monotonic() + 180
        token = None
        state = None
        while True:
            try:
                state = json.loads(get('/api/agent-pool/state'))
                if state.get('ok') is not True:
                    raise ValueError('state not ok')
                parser = _Token()
                parser.feed(get('/').decode())
                if not parser.value:
                    raise ValueError('no page token')
                token = parser.value
                break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise StopError('STOP: restarted monitor / agent pool not ready')
                time.sleep(2)
        report['restart']['ok'] = True
        save()

        untestable_cli = [v['node_id'] for v in state['nodes']
                          if v.get('interface') == 'cli' and not v.get('removed') and not v.get('enabled')]
        report['disabled_cli_blockers'] = untestable_cli
        save()
        if untestable_cli:
            raise StopError('STOP: disabled CLI nodes require user decision: %s' % untestable_cli)

        fresh = json.loads(get('/api/agent-pool/state'))
        if fresh.get('ok') is not True:
            raise StopError('STOP: agent-pool state not ok after restart')
        actual = {v['node_id']: v for v in fresh['nodes']}
        mismatch = {}
        if set(actual) != set(expected):
            mismatch['roster'] = {'expected': sorted(expected), 'actual': sorted(actual)}
        for node_id, config in expected.items():
            if node_id not in actual:
                continue
            observed = node_config(actual[node_id], legacy=bool(actual[node_id].get('legacy')))
            diff = {k: {'expected': config[k], 'actual': observed[k]}
                    for k in config if config[k] != observed[k]}
            if diff:
                mismatch[node_id] = diff
        report['config_mismatch'] = mismatch
        save()
        if mismatch:
            raise StopError('STOP: roster changed across restart: %s' % json.dumps(mismatch, ensure_ascii=False))

        for node_id, config in expected.items():
            node = actual[node_id]
            report['config_verified'].append({
                'node_id': node_id, 'kind': config['kind'], 'interface': config['interface'],
                'entry': config['entry'], 'model': config['model'],
                'reasoning_effort': config['reasoning_effort'], 'enabled': config['enabled'],
                'removed': config['removed'], 'revision': config['revision'], 'edited': False})
            if config['reasoning_effort'] is not None:
                report['effort_verified'].append({
                    'node_id': node_id, 'agent_type': node.get('agent_type'), 'model': config['model'],
                    'reasoning_effort': config['reasoning_effort'], 'edited': False})
        save()

        state = json.loads(get('/api/agent-pool/state'))
        enabled = [v['node_id'] for v in state['nodes'] if v.get('enabled') and not v.get('removed')]
        for node_id in enabled:
            fresh = json.loads(get('/api/agent-pool/state'))
            node = next(v for v in fresh['nodes'] if v['node_id'] == node_id)
            if not (node.get('enabled') and not node.get('removed')):
                raise StopError('STOP: enabled roster changed during retest')
            if node.get('interface') == 'tmux':
                report['node_tests'].append({'node_id': node_id, 'skipped': True,
                                             'reason': 'tmux attended node'})
                save()
                continue
            test_id = '%s-retest-%s' % (round_name, hashlib.sha256(node_id.encode()).hexdigest()[:20])
            record = {'node_id': node_id, 'skipped': False, 'status': 'started',
                      'client_timeout_seconds': 1200, 'test_id': test_id}
            report['node_tests'].append(record)
            save()
            request = urllib.request.Request(
                base_url + '/api/agent-pool/test',
                json.dumps({'node_id': node_id, 'expected_revision': node['revision'],
                            'test_id': test_id}).encode(),
                headers={'Content-Type': 'application/json', 'Origin': base_url,
                         'X-Xiaoba-Token': token})
            with urllib.request.urlopen(request, timeout=1200) as response:
                value = json.loads(response.read())
            result = value.get('result') or {}
            record.update(status=result.get('status'), reason=result.get('reason'),
                          http_ok=value.get('ok'))
            save()
            if not (value.get('ok') and result.get('status') == 'passed'):
                raise StopError('STOP: node retest failed; never substitute or retry: %s' % node_id)

        latest = json.loads(get('/api/agent-pool/state'))
        if set(enabled) != {v['node_id'] for v in latest['nodes'] if v.get('enabled') and not v.get('removed')}:
            raise StopError('STOP: enabled set changed after retests')
        required_cli = {v['node_id'] for v in latest['nodes'] if v.get('interface') == 'cli' and not v.get('removed')}
        tested_cli = {v['node_id'] for v in report['node_tests'] if not v.get('skipped') and v.get('status') == 'passed'}
        if not required_cli <= tested_cli:
            raise StopError('STOP: not every CLI node passed retest')
        if not any(not v['skipped'] for v in report['node_tests']):
            raise StopError('STOP: no enabled non-tmux node was tested')
        for node in latest['nodes']:
            if node['node_id'] in enabled and node.get('interface') != 'tmux':
                if not node['readiness']['ready']:
                    raise StopError('STOP: node not ready after retest: %s' % node['node_id'])

        prompt = '请用一句话说明清晰的检查清单如何帮助完成工作。只回答正文。'
        command = [str(xiaoba_entry), 'submit', prompt, '--task-purpose', 'release_smoke']
        submit_log = output / 'ordinary-task-submit.json'
        if submit_log.exists():
            raise StopError('STOP: ordinary task submit log exists; never resubmit')
        with submit_log.open('x', encoding='utf-8') as handle:
            q = subprocess.run(command, cwd=str(neutral_cwd), env=env,
                               stdout=handle, stderr=subprocess.STDOUT)
        if q.returncode != 0:
            raise StopError('STOP: release smoke submit failed; never resubmit')
        submitted = json.loads(submit_log.read_text(encoding='utf-8'))
        task_id = submitted['task_id']
        report['ordinary_task'] = {'task_id': task_id, 'argv': command, 'status': 'submitted',
                                   'default_routing': True, 'task_purpose': 'release_smoke'}
        save()
        while True:
            with closing(sqlite3.connect(live_db.as_uri() + '?mode=ro', uri=True)) as db:
                row = db.execute(
                    'SELECT status,attempts,last_error,execution_snapshot_json,provenance_json,result '
                    'FROM tasks WHERE id=?', (task_id,)).fetchone()
                if not row:
                    raise StopError('STOP: submitted task not found')
                status, attempts, last_error, snapshot_json, provenance_json, result = row
                report['ordinary_task'].update(status=status, attempts=attempts, last_error=last_error,
                                               checked_at=time.time())
                if status == 'completed':
                    report['ordinary_task'].update(
                        _verify_audit_settlement(db, task_id, snapshot_json, provenance_json, result))
                    break
            save()
            if status in {'failed', 'frozen_paused', 'resume_ready'}:
                raise StopError('STOP: release smoke task failed/paused; do not resume or substitute')
            time.sleep(5)
        report['ok'] = True
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        report['error'] = str(exc)
        raise
    finally:
        save()
    return report
