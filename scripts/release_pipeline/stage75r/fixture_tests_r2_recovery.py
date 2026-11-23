"""75R r2 point-2 isolation: continuous post-switch recovery semantics.

Drives the REAL recovery.post_switch / recover against a functional fake monitor
(real HTTP server started/stopped by the synced .command scripts) and a fake xiaoba
entry, with real core-switch evidence produced by switch_core.core_switch (fake
switch_release subprocess, apply calls counted). Proves:

  (A) no-smoke -> explicit PARTIAL (ok/completed False, sync_completed True,
      smoke_pending True); a later default recover supplements ONE real smoke and
      only then completes; core apply is NEVER repeated (apply count stays 1).
  (B) a smoke that fails mid-way (node retest failed) is recorded in a ledger
      anchored to the core-switch dir; retrying — even under a DIFFERENT out_dir —
      refuses to repeat the retest (server test-call count stays 1), original kept.
  (C) a failed ordinary task is never resubmitted (xiaoba call count stays 1) across
      an out_dir change.

NO production mutation: temp port, card-local tmp, fake entry. The production
monitor is never touched. Run: python3.14 -B fixture_tests_r2_recovery.py.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import common
import recovery
import switch_core
from common import PIN, StopError, sha
from fixture_tests import CONFIG_PY, MONITOR_PY, REAL_CANDIDATE, _j, _w, build_fixture

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


def _free_port():
    q = subprocess.run([str(PIN), '-B', '-c',
                        'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); '
                        'print(s.getsockname()[1]); s.close()'], capture_output=True, text=True)
    return int(q.stdout.strip())


ROSTER = [
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

SERVER = '''import argparse, json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int); a = ap.parse_args()
here = Path(__file__).resolve().parent
nodes = json.loads((here / "roster.json").read_text())
behavior = json.loads((here / "behavior.json").read_text())
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
            cf = Path(behavior["test_count_file"])
            n = int(cf.read_text()) if cf.exists() else 0
            cf.write_text(str(n + 1))
            self._send({"ok": True, "result": {"status": behavior.get("retest_status", "passed"),
                                               "reason": "synthetic"}})
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", a.port), H).serve_forever()
'''


def build_functional_fixture(root, *, retest_status='passed', task_status='completed'):
    """build_fixture (real candidate + acceptance DB) then swap in a functional
    monitor: source/repo carry the real start/close scripts, live carries echo
    scripts so the authorized sync installs the functional ones. fake_server.py /
    config.py / roster.json / monitor.json are identical in source and live."""
    root = Path(root)
    ctx, paths = build_fixture(root)
    port = _free_port()
    source_monitor = paths['source_monitor']
    repo_monitor = paths['repo_monitor']
    live_monitor = paths['live_monitor']
    test_count = root / 'test-count.txt'
    submit_count = root / 'submit-count.txt'

    for d in (source_monitor, live_monitor):
        _w(d / 'fake_server.py', SERVER)
        _j(d / 'roster.json', ROSTER)
        _j(d / 'monitor.json', {'port': port})
        _w(d / 'monitor_core/config.py', CONFIG_PY)
    _j(live_monitor / 'behavior.json', {'retest_status': retest_status,
                                        'test_count_file': str(test_count)})
    (live_monitor / 'run').mkdir(exist_ok=True)
    (live_monitor / 'logs').mkdir(exist_ok=True)

    start_sh = ('#!/bin/bash\nDIR="$(cd "$(dirname "$0")" && pwd)"\n'
                'nohup "%s" -B "$DIR/fake_server.py" --port %d >> "$DIR/logs/s.log" 2>&1 &\n'
                'echo $! > "$DIR/run/supervisor.pid"\n'
                'for _ in $(seq 1 40); do /usr/bin/curl -fsS --max-time 2 '
                '"http://127.0.0.1:%d/healthz" >/dev/null 2>&1 && break; sleep 0.3; done\n'
                % (PIN, port, port))
    close_sh = ('#!/bin/bash\nDIR="$(cd "$(dirname "$0")" && pwd)"\n'
                'PID="$(cat "$DIR/run/supervisor.pid" 2>/dev/null)"\n'
                '[ -n "$PID" ] && kill "$PID" 2>/dev/null\n'
                'for _ in $(seq 1 30); do kill -0 "$PID" 2>/dev/null || break; sleep 0.2; done\n')
    # source + repo: functional scripts; live: echo scripts (sync installs functional)
    for d in (source_monitor, repo_monitor):
        _w(d / '启动监看中心.command', start_sh)
        _w(d / '关闭监看中心.command', close_sh)
        _w(d / 'xiaoba_monitor.py', MONITOR_PY)
    _w(live_monitor / '启动监看中心.command', '#!/bin/bash\necho old-start\n')
    _w(live_monitor / '关闭监看中心.command', '#!/bin/bash\necho old-close\n')
    _w(live_monitor / 'xiaoba_monitor.py', MONITOR_PY)

    # rebind the attempt record syntax hashes to the functional source files
    names = ['xiaoba_monitor.py', '启动监看中心.command', '关闭监看中心.command']
    syntax = [{'file': n, 'path': str(repo_monitor / n),
               'kind': 'python' if n.endswith('.py') else 'shell',
               'sha256': sha(repo_monitor / n)} for n in names]
    ctx['record']['syntax'] = syntax
    _j(Path(ctx['record']['record_path']), ctx['record'])

    # fake xiaoba entry: counts submits, inserts a task with the requested status
    live_db = paths['live_db']
    xiaoba_py = _w(root / 'xiaoba-entry.py',
                   'import json, sqlite3\n'
                   'from pathlib import Path\n'
                   'cf = Path(%r)\n'
                   'n = int(cf.read_text()) if cf.exists() else 0\n'
                   'cf.write_text(str(n + 1))\n'
                   'task_id = "synthetic-task-1"\n'
                   'with sqlite3.connect(%r) as c:\n'
                   '    c.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?,?)",\n'
                   '              (task_id, %r, 1, None))\n'
                   'print(json.dumps({"task_id": task_id}))\n'
                   % (str(submit_count), str(live_db), task_status))
    xiaoba_sh = _w(root / 'xiaoba', '#!/bin/bash\nexec "%s" -B "%s" "$@"\n' % (PIN, xiaoba_py))
    os.chmod(xiaoba_sh, 0o755)

    # start the initial fake server so run_smoke can capture a pre-restart baseline
    env = common.clean_env()
    env['XIAOBA_MONITOR_PORT'] = str(port)
    env['XIAOBA_MONITOR_NO_BROWSER'] = '1'
    proc = subprocess.Popen([str(PIN), '-B', str(live_monitor / 'fake_server.py'), '--port', str(port)],
                            cwd=str(live_monitor), env=env)
    (live_monitor / 'run/supervisor.pid').write_text(str(proc.pid))
    base_url = 'http://127.0.0.1:%d' % port
    for _ in range(40):
        try:
            with urllib.request.urlopen(base_url + '/healthz', timeout=2) as r:
                if r.status == 200:
                    break
        except OSError:
            time.sleep(0.3)
    return {'ctx': ctx, 'paths': paths, 'port': port, 'proc': proc, 'env': env,
            'base_url': base_url, 'xiaoba': xiaoba_sh, 'test_count': test_count,
            'submit_count': submit_count, 'live_monitor': live_monitor}


def _core_switch(fx, switch_dir):
    root = switch_dir.parent
    scenario = _j(root / 'scenario.json', {})
    count_file = root / 'switch-count.json'
    env = common.clean_env()
    env['FAKE_SWITCH_SCENARIO'] = str(scenario)
    env['FAKE_SWITCH_COUNT'] = str(count_file)
    env['COUNT_MODE'] = 'apply'
    summary = switch_core.core_switch(fx['ctx'], switch_dir, env=env)
    return summary, count_file


def _stop_server(fx):
    pid_file = fx['live_monitor'] / 'run/supervisor.pid'
    if pid_file.is_file():
        try:
            os.kill(int(pid_file.read_text()), 15)
        except (ProcessLookupError, ValueError, OSError):
            pass
    try:
        fx['proc'].terminate()
        fx['proc'].wait(timeout=5)
    except Exception:
        pass


def _count(path):
    return int(path.read_text()) if path.is_file() else 0


def case_complete(base):
    root = base / 'complete'
    fx = build_functional_fixture(root, retest_status='passed', task_status='completed')
    try:
        switch_dir = root / 'switch'
        summary, count_file = _core_switch(fx, switch_dir)
        record('A_core_switch_activated', summary['ok'] and summary['activated'])
        post_dir = root / 'post-partial'
        partial = recovery.post_switch(fx['ctx'], switch_dir, post_dir, env=fx['env'],
                                       run_smoke_tests=False, xiaoba_entry=fx['xiaoba'])
        record('A_no_smoke_partial', partial['ok'] is False and partial['completed'] is False
               and partial['status'] == 'partial_sync_only' and partial['smoke_pending'] is True
               and partial['sync_completed'] is True and partial['smoke'] is None)
        record('A_partial_no_smoke_attempt', not (switch_dir / 'smoke-attempts.json').exists())
        # supplement ONE real smoke via default recover; core never re-applied
        done_dir = root / 'post-complete'
        rep = recovery.recover(fx['ctx'], switch_dir, done_dir, env=fx['env'],
                               run_smoke_tests=True, xiaoba_entry=fx['xiaoba'])
        counts = json.loads(count_file.read_text())
        record('A_supplement_completes', rep['ok'] is True and rep['completed'] is True
               and rep['status'] == 'completed' and rep['smoke_pending'] is False
               and rep['smoke']['ok'] is True)
        record('A_core_not_reapplied', counts.get('apply') == 1, 'apply_calls=%s' % counts.get('apply'))
        record('A_smoke_retest_and_task', _count(fx['test_count']) == 2 and _count(fx['submit_count']) == 1
               and rep['smoke']['ordinary_task']['status'] == 'completed')
        ledger = json.loads((switch_dir / 'smoke-attempts.json').read_text())
        record('A_one_smoke_attempt_recorded', len(ledger['attempts']) == 1 and ledger['attempts'][0]['ok'] is True)
        expect_stop('A_completed_post_refused',
                    lambda: recovery.post_switch(fx['ctx'], switch_dir, done_dir, env=fx['env'],
                                                 run_smoke_tests=True, xiaoba_entry=fx['xiaoba']))
    finally:
        _stop_server(fx)


def case_node_failure(base):
    root = base / 'node-fail'
    fx = build_functional_fixture(root, retest_status='failed', task_status='completed')
    try:
        switch_dir = root / 'switch'
        _core_switch(fx, switch_dir)
        post_dir = root / 'post-fail'
        expect_stop('B_smoke_node_failure_refused',
                    lambda: recovery.post_switch(fx['ctx'], switch_dir, post_dir, env=fx['env'],
                                                 run_smoke_tests=True, xiaoba_entry=fx['xiaoba']))
        record('B_failure_recorded', (switch_dir / 'smoke-attempts.json').is_file()
               and (post_dir / 'smoke/post-switch-smoke.json').is_file())
        done = json.loads((post_dir / 'post-switch-result.json').read_text())
        record('B_failed_not_complete', done['ok'] is False and done['completed'] is False
               and done['status'] == 'smoke_failed' and done['smoke_attempted'] is True)
        record('B_one_retest_no_submit', _count(fx['test_count']) == 1 and _count(fx['submit_count']) == 0)
        # retry SAME out_dir: ledger refuses, no repeat
        expect_stop('B_retry_same_outdir_refused',
                    lambda: recovery.post_switch(fx['ctx'], switch_dir, post_dir, env=fx['env'],
                                                 run_smoke_tests=True, xiaoba_entry=fx['xiaoba']))
        # retry DIFFERENT out_dir: still refused (ledger anchored to switch_dir)
        other_dir = root / 'post-launder'
        expect_stop('B_retry_other_outdir_refused',
                    lambda: recovery.post_switch(fx['ctx'], switch_dir, other_dir, env=fx['env'],
                                                 run_smoke_tests=True, xiaoba_entry=fx['xiaoba']))
        record('B_no_launder_no_repeat', _count(fx['test_count']) == 1 and _count(fx['submit_count']) == 0)
    finally:
        _stop_server(fx)


def case_task_failure(base):
    root = base / 'task-fail'
    fx = build_functional_fixture(root, retest_status='passed', task_status='failed')
    try:
        switch_dir = root / 'switch'
        _core_switch(fx, switch_dir)
        post_dir = root / 'post-fail'
        expect_stop('C_smoke_task_failure_refused',
                    lambda: recovery.post_switch(fx['ctx'], switch_dir, post_dir, env=fx['env'],
                                                 run_smoke_tests=True, xiaoba_entry=fx['xiaoba']))
        record('C_two_retests_one_submit', _count(fx['test_count']) == 2 and _count(fx['submit_count']) == 1)
        other_dir = root / 'post-launder'
        expect_stop('C_retry_other_outdir_refused',
                    lambda: recovery.post_switch(fx['ctx'], switch_dir, other_dir, env=fx['env'],
                                                 run_smoke_tests=True, xiaoba_entry=fx['xiaoba']))
        record('C_no_resubmit', _count(fx['submit_count']) == 1)
        ledger = json.loads((switch_dir / 'smoke-attempts.json').read_text())
        record('C_failed_task_recorded', ledger['attempts'][-1]['ordinary_task']['status'] == 'failed'
               and ledger['attempts'][-1]['ok'] is False)
    finally:
        _stop_server(fx)


def main():
    TMP.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix='r2-recovery-', dir=TMP))
    print('r2 recovery isolation root: %s' % base, flush=True)
    case_complete(base)
    case_node_failure(base)
    case_task_failure(base)
    report = {'ok': all(r['ok'] for r in RESULTS), 'total': len(RESULTS),
              'passed': sum(r['ok'] for r in RESULTS), 'results': RESULTS,
              'isolation_root': str(base), 'real_candidate': str(REAL_CANDIDATE)}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / 'recovery-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('SUMMARY %d/%d passed' % (report['passed'], report['total']), flush=True)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    common.guard_runtime()
    sys.exit(main())