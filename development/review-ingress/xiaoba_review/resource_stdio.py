"""Explicit user-owned JSON stdio entry; never infer vendor CLI flags or shell."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile


class JsonStdioResource:
    supports_resource_plan = True
    protocol = 'xiaoba-resource-v1'

    def __init__(self, source):
        self.kind, self.source_ref = source['kind'], source['source_ref']
        self.entry = source['node']['entry']

    def resource_identity(self):
        from .cli_identity import require_entry, implementation_digest
        require_entry(self.entry)
        return {'protocol':self.protocol,
                'implementation_sha256':implementation_digest(('resource_stdio.py','cli_identity.py'))}

    @staticmethod
    def _unsupported():
        from .resource_agy import InteractiveError
        return InteractiveError('unsupported_entry', '该入口未实现小八JSON协议；请在节点中选择正确的 Agent 类型后重新测试运行。')

    def call(self, payload):
        before=self.resource_identity()
        # Output goes to bounded reads, not memory-sized communicate buffers.
        with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as errors:
            process=subprocess.Popen([self.entry],stdin=subprocess.PIPE,stdout=output,stderr=errors,
                                     start_new_session=True)
            try:process.communicate(json.dumps(payload,ensure_ascii=False).encode(),timeout=45)
            except BaseException:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait()
                raise
            if process.returncode or output.tell()>1024*1024 or errors.tell()>1024*1024:
                raise ValueError('CLI执行失败或响应过大；不回显原始输出，不自动重试')
            output.seek(0)
            try:value=json.loads(output.read(1024*1024))
            except (ValueError,UnicodeError):raise self._unsupported() from None
        if not isinstance(value,dict) or value.get('protocol')!=self.protocol:
            raise self._unsupported()
        return value

    def test_resource(self, plan, challenge):
        value=self.call({'protocol':self.protocol,'operation':'test','resource_plan':plan,
                         'challenge':challenge,'instruction':'调用指定模型，仅返回challenge；模型和路由元数据由调用器提供。'})
        if value.get('challenge')!=challenge:raise ValueError('测试响应未绑定本次挑战')
        return value

    def run(self, card):
        value=self.call({'protocol':self.protocol,'operation':'review','card':card})
        if value.get('card_digest')!=card['card_digest']:raise ValueError('入口响应未绑定本任务')
        result = {
            'verdict': value.get('verdict'),
            'findings': value.get('findings'),
            'evidence_refs': value.get('evidence_refs'),
            'execution_evidence': value.get('execution_evidence', {})
        }
        for key in ('review_type', 'score', 'scores', 'applicability', 'duplicate_of'):
            if key in value:
                result[key] = value[key]
        return result


def json_stdio_factory(source, workspace_root=None, agy_mode='cli'):
    # agy_mode is for tests and a future explicit setting only; it is never
    # accepted from the page or an HTTP body.  The default is one CLI call per
    # step, which keeps the receipt card; 'interactive' keeps the PTY adapter.
    node=source.get('node',{})
    from .cli_identity import agent_type
    kind = agent_type(node)
    if node.get('interface')=='cli' and kind == 'agy':
        if agy_mode=='interactive':
            from .resource_agy import AgyInteractiveResource
            return AgyInteractiveResource(source, workspace_root)
        if agy_mode!='cli':raise ValueError('agy_mode只接受cli或interactive')
        from .resource_agy_cli import AgyCliResource
        return AgyCliResource(source, workspace_root)
    if node.get('interface')=='cli':
        from .resource_vendor_cli import RESOURCES
        if kind in RESOURCES:return RESOURCES[kind](source, workspace_root)
    return JsonStdioResource(source) if node.get('interface')=='cli' and node.get('entry') else None
