"""卡 41 反例：CLI 提交 --material 后 run_once 端到端，材料进节点工作区。

真实流程用 CLI 提交（不手写 snapshot）；节点经真实 AgentPool 探针上线，
派发走真实 PoolCliAdapter / CliWorklogResource，假 agy 只读派工卡列出的
材料副本并写回执（健康探针回挑战原文）。
"""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from xiaoba_agentos.cli import main
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.store import EventStore

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'review-ingress'))

FAKE_AGY = r'''#!__PYTHON__
import json,os,re,sys
from pathlib import Path
message=json.loads(sys.stdin.readline())
prompt=message['message']['content']
root=Path(__file__).parent
with (root/'calls').open('a') as out:
    out.write(json.dumps({'event':'start','cwd':os.getcwd(),'prompt':prompt})+'\n')
cardpath=Path(json.loads(prompt.split('XIAOBA_TASK_CARD=',1)[1]))
card=cardpath.read_text()
logpath=Path(re.search(r'^工作回执文件: (.+)$',card,re.M)[1])
taskid=re.search(r'^任务ID: (.+)$',card,re.M)[1]
step=re.search(r'^步骤码: (.+)$',card,re.M)[1]
nonce=re.search(r'^本轮标识: (.+)$',card,re.M)[1]
context=json.loads(card.split('## 材料\n',1)[1].split('\n\n## 范围与回执',1)[0])
staged=[]
if '## 材料副本（只读）' in card:
    for line in card.split('## 材料副本（只读）\n',1)[1].splitlines():
        if line.startswith('- '):staged.append(line[2:].strip())
if 'challenge' in context:
    body=context['challenge']
else:
    assert staged, 'TEST fake cli got no staged material copy'
    body='\n'.join('MATERIAL:'+Path(p).read_text().strip() for p in staged)
header=card.split('先写以下文件头：\n\n',1)[1].split('\n文件已存在时',1)[0]
if not logpath.exists():
    logpath.write_text(header)
meta='任务ID: '+taskid+'\n步骤码: '+step+'\n状态: completed\n'
with logpath.open('a') as out:
    out.write('\n<!-- task:'+nonce+' -->\n'+meta+'---\n'+body+'\n')
    out.write('<!-- done:'+nonce+' -->\n')
print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'TEST'}}))
'''


class Card41SubmitMaterialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        from test_card37 import _isolated_config
        self.config = _isolated_config(self.root, [])
        self.store = EventStore(self.config.database_path)
        self.store.initialize()
        self.enterContext(patch('xiaoba_agentos.orchestrator.release_binding_status',
                                return_value={'ok': True}))
        self.entry = self.root / 'agy'
        self.entry.write_text(FAKE_AGY.replace('__PYTHON__', sys.executable))
        self.entry.chmod(0o700)
        self.material = self.root / 'ingress-task-card.json'
        self.material.write_text(json.dumps(
            {'assignment_id': 'ASSIGN-C41', 'review_request_id': 'REQ-C41',
             'scope': 'TEST 材料内容：接入口任务卡正文'}, ensure_ascii=False))
        # 真实节点上线：AgentPool 登记 + test_run 健康探针写真实资格证据；
        # 工作区根目录与 orchestrator 的 workspace_root 一致，身份摘要才能对上。
        from xiaoba_review.store import ReviewStore
        from xiaoba_review.backends import BackendRegistry
        from xiaoba_review.agent_pool import AgentPool
        from xiaoba_review.resource_stdio import json_stdio_factory
        self.workspaces = Path(self.config.runtime_dir) / 'agent-workspaces'
        self.workspaces.mkdir(parents=True, exist_ok=True)
        # 显式绑定工作区根目录（同 run.py 的绑定语义），readiness 身份才可核对。
        review_store = ReviewStore(self.store, self.config.runtime_dir / 'memory-outbox',
            workspace_root=self.workspaces)
        backends = BackendRegistry(review_store, resource_factory=lambda source:
            json_stdio_factory(source, self.workspaces, agy_mode='cli'))
        self.addCleanup(backends.close)
        pool = AgentPool(review_store, backends)
        for node_id, model in (('c41-node-a', 'alpha'), ('c41-node-b', 'beta'), ('c41-node-c', 'gamma')):
            source = pool.add({'node_id': node_id, 'name': node_id, 'agent': 'custom',
                'kind': 'peer', 'interface': 'cli', 'entry': str(self.entry), 'model': model})
            pool.change(node_id, source['node']['revision'], 'toggle', True)
            revision = next(v for v in pool.state()['nodes'] if v['node_id'] == node_id)['revision']
            result = pool.runtime.test_run(node_id, revision, 'TEST-c41-probe-' + node_id)
            self.assertEqual('passed', result['status'], result)

    def test_submit_material_reaches_node_workspace_and_receipt_is_readable(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = main(['--config', str(self.config.path), 'submit',
                         'TEST 材料端到端', '--memory-kind', 'audit',
                         '--material', str(self.material)])
        self.assertEqual(0, code)
        task_id = json.loads(stream.getvalue())['task_id']
        snapshot = json.loads(self.store.get_task(task_id)['execution_snapshot_json'])
        self.assertEqual(snapshot['material_files'], [{
            'path': str(self.material.resolve()),
            'sha256': hashlib.sha256(self.material.read_bytes()).hexdigest()}])
        for _ in range(6):
            if self.store.get_task(task_id)['status'] == 'completed':
                break
            Orchestrator(self.config, self.store).run_once()
        copies = [p for p in self.workspaces.rglob('材料/*') if p.is_file()]
        self.assertTrue(copies, '材料没有进节点工作区')
        self.assertTrue(all(p.read_text() == self.material.read_text() for p in copies))
        receipts = list(self.workspaces.rglob('执行回执-*.json'))
        material_receipts = [p for p in receipts if 'MATERIAL:' in p.read_text()]
        self.assertTrue(material_receipts, '节点没有写出读到材料的执行回执')
        self.assertIn('接入口任务卡正文', material_receipts[-1].read_text())
        self.assertEqual('completed', self.store.get_task(task_id)['status'])


if __name__ == '__main__':
    unittest.main()
