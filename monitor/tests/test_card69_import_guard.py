"""卡 69：台账模块导入失败不能拦住 Agent 池挂载（卡 66 第 9 项的真反例）。

修复前 `configure` 的 ImportError 会冒到 attach 的外层兜底，整个 Agent 池不注册、
页面没有 /agent-pool.js；修复后只告警，模块照常挂载。
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

CORE = Path('/Users/suzhizhou/Documents/小八AgentOS/xiaoba-agentos/development/integrated-candidate')
INGRESS = CORE.parent / 'review-ingress/xiaoba_review'
PYTHON = '/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14'
MONITOR = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def release_fixture(root):
    """A verified release whose xiaoba_agentos.workday_ledger fails at import."""
    release = root / 'releases/vtest'
    project = release / 'agentos'
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc')
    shutil.copytree(CORE / 'src', project / 'src', ignore=ignore)
    shutil.copytree(INGRESS, project / 'src/xiaoba_review', ignore=ignore)
    for name in ('README.md', 'pyproject.toml', '.gitignore', 'CAPABILITY.json'):
        shutil.copy2(CORE / name, project / name)
    (project / 'src/xiaoba_agentos/workday_ledger.py').write_text(
        "raise ImportError('TEST workday ledger')\n", encoding='utf-8')
    entries = {p.relative_to(project).as_posix(): sha(p) for p in project.rglob('*') if p.is_file()}
    (project / 'SOURCE_MANIFEST.json').write_text(json.dumps({'algorithm': 'sha256', 'files': entries}))
    (release / 'RELEASE.json').write_text(json.dumps({
        'version': 'vtest', 'status': 'verified_immutable_release',
        'agentos_source_manifest_sha256': sha(project / 'SOURCE_MANIFEST.json')}))
    (release / 'FILES.sha256').write_text(''.join(
        sha(p) + '  ./' + p.relative_to(release).as_posix() + '\n'
        for p in sorted(release.rglob('*')) if p.is_file()))
    index = root / 'releases/index.json'
    index.write_text(json.dumps({'current_release': 'vtest', 'releases': [{
        'version': 'vtest', 'path': 'vtest', 'status': 'verified_immutable',
        'files_manifest_sha256': sha(release / 'FILES.sha256')}]}))
    raw = {'schema_version': 2, 'runtime_dir': str(root / 'runtime'), 'project_dir': str(project),
           'release': {'index_path': str(index), 'expected_version': 'vtest'}, 'agents': [],
           'worker': {k: 1 for k in ('poll_seconds', 'lease_seconds', 'max_attempts', 'parallelism',
                                     'minimum_successful_agents')},
           'integrations': {'node_command': 'fixture-only'},
           'skills': {'root_dir': 'skills', 'index_path': 'skills/index.json', 'max_selected': 1},
           'knowledge': {'index_path': 'knowledge.json', 'allowed_roots': [str(root)]},
           'memory': {'obsidian_vault': str(root / 'vault'), 'publish_enabled': False}}
    config = root / 'agentos.json'
    config.write_text(json.dumps(raw))
    page = root / 'page.html'
    page.write_bytes((MONITOR / 'xiaoba-monitor.html').read_bytes())
    return project, config, page


class LedgerImportGuardTests(unittest.TestCase):
    def test_broken_ledger_module_still_mounts_the_agent_pool(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp).resolve()
            project, config, page = release_fixture(root)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            spec = root / 'process.json'
            spec.write_text(json.dumps({'port': port, 'project': str(project), 'config': str(config),
                                        'page': str(page), 'root': str(root)}))
            copy = root / 'monitor-copy'
            shutil.copytree(MONITOR, copy, ignore=shutil.ignore_patterns(
                '__pycache__', '*.pyc', 'node_modules', 'run', 'logs', '小八3D导演台'))
            env = dict(os.environ, PYTHONPATH=str(copy))
            env.pop('PYTHONDONTWRITEBYTECODE', None)
            proc = subprocess.Popen([PYTHON, str(copy / 'tests' / Path(__file__).name), 'serve', str(spec)],
                                    cwd=str(copy), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            try:
                base = 'http://127.0.0.1:%d' % port
                for _ in range(200):
                    if proc.poll() is not None:
                        self.fail(proc.communicate()[1].decode())
                    try:
                        with urllib.request.urlopen(base + '/healthz', timeout=.3) as response:
                            self.assertTrue(json.load(response)['ok'])
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    self.fail('monitor did not start')
                modules = json.loads(urllib.request.urlopen(base + '/api/modules', timeout=5).read())['modules']
                loader = next(m for m in modules if m['id'] == 'agent-pool-loader')
                self.assertEqual('ok', loader['status'], loader)
                self.assertIn('已加载活动版本', loader['detail'])
                body = urllib.request.urlopen(base + '/', timeout=5).read()
                self.assertIn(b'/agent-pool.js', body)
                self.assertIn(b'id="apDrawer"', body)
            finally:
                proc.terminate()
                try:
                    proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()


if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == 'serve':
    import signal
    import threading
    import xiaoba_monitor
    from monitor_core import config as cfg, logs
    from monitor_core.modules.system import SystemModule
    data = json.loads(Path(sys.argv[2]).read_text())
    settings = cfg.load(Path(data['root']) / 'absent.json', {
        'port': data['port'], 'agentos_project': data['project'],
        'agentos_config': data['config'], 'page_file': data['page']})
    settings['root'] = data['root']
    xiaoba_monitor.MODULES = [SystemModule]
    app = xiaoba_monitor.build_app(settings, logs.setup(None))
    server = app.server()
    app.start()
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever()
    finally:
        app.stop()
        server.server_close()
elif __name__ == '__main__':
    unittest.main()
