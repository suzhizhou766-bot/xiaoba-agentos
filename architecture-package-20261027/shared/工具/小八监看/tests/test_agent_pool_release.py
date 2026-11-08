"""Release integration tests use real HTTP processes and temporary databases."""
import hashlib
import json
import os
from pathlib import Path
import re
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

def fixture(root, mode):
    release = root / 'releases/vtest'
    project = release / 'agentos'
    shutil.copytree(CORE / 'src', project / 'src', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    if mode != 'old':
        shutil.copytree(INGRESS, project / 'src/xiaoba_review', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    if mode == 'broken':
        (project / 'src/xiaoba_review/__init__.py').write_text('raise ImportError("fixture failure")\n')
    for name in ('README.md', 'pyproject.toml', '.gitignore', 'CAPABILITY.json'):
        shutil.copy2(CORE / name, project / name)
    entries = {p.relative_to(project).as_posix(): sha(p) for p in project.rglob('*') if p.is_file()}
    (project / 'SOURCE_MANIFEST.json').write_text(json.dumps({'algorithm':'sha256','files':entries}))
    (release / 'RELEASE.json').write_text(json.dumps({'version':'vtest','status':'verified_immutable_release',
        'agentos_source_manifest_sha256':sha(project / 'SOURCE_MANIFEST.json')}))
    (release / 'FILES.sha256').write_text(''.join(sha(p)+'  ./'+p.relative_to(release).as_posix()+'\n'
        for p in sorted(release.rglob('*')) if p.is_file()))
    index = root / 'releases/index.json'
    index.write_text(json.dumps({'current_release':'vtest','releases':[{'version':'vtest','path':'vtest',
        'status':'verified_immutable','files_manifest_sha256':sha(release / 'FILES.sha256')}]}))
    if mode == 'tampered':
        (project / 'src/xiaoba_review/__init__.py').write_text('raise RuntimeError("unverified execution")')
    raw = {'schema_version':2, 'runtime_dir':str(root / 'runtime'),'project_dir':str(project),
        'release':{'index_path':str(index),'expected_version':'vtest'},'agents':[],
        'worker':{k:1 for k in ('poll_seconds','lease_seconds','max_attempts','parallelism','minimum_successful_agents')},
        'integrations':{'node_command':'fixture-only'},
        'skills':{'root_dir':'skills','index_path':'skills/index.json','max_selected':1},
        'knowledge':{'index_path':'knowledge.json','allowed_roots':[str(root)]},
        'memory':{'obsidian_vault':str(root / 'vault'),'publish_enabled':False}}
    config = root / 'agentos.json';config.write_text(json.dumps(raw))
    page = root / 'page.html'
    page.write_bytes((MONITOR / 'xiaoba-monitor.html').read_bytes())
    return project, config, page

class ReleaseAgentProcessTest(unittest.TestCase):
    def run_case(self, mode):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project, config, page = fixture(root, mode)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            spec=root/'process.json'
            # 服务进程的 PATH 里可能没有 tmux；用户在场通道的可执行文件由 monitor.json
            # 的 agent_pool.tmux_path 指定，这里用一个临时夹具证明它真的生效。
            fake_tmux=root/'fake-tmux'
            fake_tmux.write_text('#!/bin/sh\nexit 0\n')
            fake_tmux.chmod(0o700)
            spec.write_text(json.dumps({'port':port,'project':str(project),'config':str(config),'page':str(page),'root':str(root),'tmux':str(fake_tmux)}))
            child_env=dict(os.environ,PYTHONPATH=str(MONITOR))
            child_env.pop('PYTHONDONTWRITEBYTECODE',None)
            proc=subprocess.Popen([PYTHON,str(Path(__file__).resolve()),'serve',str(spec)],
                cwd=str(MONITOR),stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                env=child_env)
            try:
                base='http://127.0.0.1:%d'%port
                for _ in range(150):
                    if proc.poll() is not None:self.fail(proc.communicate()[1].decode())
                    try:
                        with urllib.request.urlopen(base+'/healthz',timeout=.3) as response:self.assertTrue(json.load(response)['ok'])
                        break
                    except OSError:time.sleep(.1)
                else:self.fail('monitor did not start')
                def get(path):
                    with urllib.request.urlopen(base+path,timeout=5) as response:return response.read()
                body=get('/')
                health=json.loads(get('/api/modules'))['modules']
                loader=next(m for m in health if m['id']=='agent-pool-loader')
                if mode=='new':
                    self.assertEqual('ok',loader['status'],loader)
                    self.assertIn(b'/agent-pool.js',body)
                    self.assertIn(b'/open-review-native.js',body)
                    self.assertTrue(get('/open-review-native.js'))
                    self.assertEqual(str((root/'workspaces').resolve()),json.loads(get('/fixture-collector'))['workspace'])
                    # agent_pool.tmux_path 生效后，详情里不再出现"未指定"的说明。
                    self.assertEqual(str(fake_tmux),json.loads(get('/fixture-collector'))['tmux'])
                    self.assertNotIn('tmux_path',loader['detail'])
                    self.assertIn(b'id="apDrawer"',body)
                    self.assertIn('后台 Agent'.encode(),body)
                    token=re.search(rb'name="xiaoba-page-token" content="([^"]+)"',body)[1].decode()
                    node={'node_id':'fixture','name':'Fixture','agent':'fixture','kind':'peer',
                          'interface':'web','entry':'https://example.invalid','model':'fixture-model'}
                    request=urllib.request.Request(base+'/api/agent-pool/nodes',json.dumps(node).encode(),
                        {'Origin':base,'X-Xiaoba-Token':token,'Content-Type':'application/json'})
                    with urllib.request.urlopen(request) as response:self.assertTrue(json.load(response)['ok'])
                    state=json.loads(get('/api/agent-pool/state'))
                    self.assertEqual('fixture',state['nodes'][0]['node_id'])
                    self.assertTrue((root/'runtime/agentos.sqlite3').is_file())
                    time.sleep(2.5)
                    self.assertTrue(json.loads(get('/healthz'))['ok'])
                    self.assertGreaterEqual(json.loads(get('/fixture-collector'))['calls'], 1)
                    self.assertEqual([],list(project.rglob('__pycache__')))
                else:
                    # 旧版本是正常状态，总状态不能变黄；坏包和篡改才告警。
                    self.assertEqual('ok' if mode=='old' else 'warn',loader['status'])
                    self.assertNotIn(b'/agent-pool.js',body)
                    self.assertEqual(get('/fixture-original'),body)
                    self.assertFalse((root/'runtime/agentos.sqlite3').exists())
                    self.assertFalse(any(m['id']=='open-review' for m in health))
                    if mode=='old':self.assertIn('未包含',loader['detail'])
            finally:
                proc.terminate()
                try:proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:proc.kill();proc.communicate()
    def test_active_release_mounts_and_adds_node(self):self.run_case('new')
    def test_old_release_preserves_page_bytes(self):self.run_case('old')
    def test_import_failure_keeps_system_alive(self):self.run_case('broken')
    def test_tampered_release_never_imported(self):self.run_case('tampered')

if __name__=='__main__' and len(sys.argv)>1 and sys.argv[1]=='serve':
    import signal
    import threading
    import xiaoba_monitor
    from monitor_core import config, logs
    from monitor_core.modules.system import SystemModule
    from monitor_core.web import Route
    data=json.loads(Path(sys.argv[2]).read_text())
    settings=config.load(Path(data['root'])/'absent.json',{'port':data['port'],
        'agentos_project':data['project'],'agentos_config':data['config'],'page_file':data['page'],
        'agent_pool':{'workspace_root':data['root']+'/workspaces','tmux_path':data['tmux']}})
    settings['root']=data['root']
    xiaoba_monitor.MODULES=[SystemModule]
    app=xiaoba_monitor.build_app(settings,logs.setup(None))
    counter={'calls':0}
    extension=app.ctx.modules.get('open-review')
    if extension:
        counter['workspace']=str(extension.store.workspace_root)
        try:
            from xiaoba_review import resource_tmux
            counter['tmux']=resource_tmux._EXECUTABLE
        except ImportError:
            counter['tmux']=None
        collect=extension.pool.tmux.collect
        def counted_collect():
            counter['calls']+=1
            return collect()
        extension.pool.tmux.collect=counted_collect
    app.routes[('GET','/fixture-collector')]=Route('GET','/fixture-collector',lambda req:(200,counter),'read')
    app.routes[('GET','/fixture-original')]=Route('GET','/fixture-original',app.ctx.modules['system'].index,'page')
    server=app.server();app.start()
    signal.signal(signal.SIGTERM,lambda *_:threading.Thread(target=server.shutdown,daemon=True).start())
    try:server.serve_forever()
    finally:app.stop();server.server_close()
