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
        path = Path(self.entry).resolve(strict=True)
        if not path.is_file() or not os.access(path,os.X_OK):
            raise ValueError('CLI入口必须是可执行文件')
        with path.open('rb') as stream:
            checksum=hashlib.file_digest(stream,'sha256').hexdigest()
        return {'protocol':self.protocol,'entry':str(path),'sha256':checksum}

    def call(self, payload):
        before=self.resource_identity()
        # Output goes to bounded reads, not memory-sized communicate buffers.
        with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as errors:
            process=subprocess.Popen([before['entry']],stdin=subprocess.PIPE,stdout=output,stderr=errors,
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
            except (ValueError,UnicodeError):raise ValueError('该CLI未实现小八JSON入口协议，请使用对应调用适配器') from None
        if before!=self.resource_identity():raise ValueError('调用期间入口已变化')
        if not isinstance(value,dict) or value.get('protocol')!=self.protocol:
            raise ValueError('入口响应协议不符')
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
        for key in ('review_type', 'score', 'applicability', 'duplicate_of'):
            if key in value:
                result[key] = value[key]
        return result


def json_stdio_factory(source, workspace_root=None, agy_mode='cli'):
    # agy_mode is for tests and a future explicit setting only; it is never
    # accepted from the page or an HTTP body.  The default is one CLI call per
    # step, which keeps the receipt card; 'interactive' keeps the PTY adapter.
    node=source.get('node',{})
    if node.get('interface')=='cli' and Path(node.get('entry','')).name in {'agy','antigravity'}:
        if agy_mode=='interactive':
            from .resource_agy import AgyInteractiveResource
            return AgyInteractiveResource(source, workspace_root)
        if agy_mode!='cli':raise ValueError('agy_mode只接受cli或interactive')
        from .resource_agy_cli import AgyCliResource
        return AgyCliResource(source, workspace_root)
    return JsonStdioResource(source) if node.get('interface')=='cli' and node.get('entry') else None
