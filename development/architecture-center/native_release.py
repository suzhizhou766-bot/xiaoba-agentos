"""Fixed, host-configured bridge to the accepted native switch command.

No executable path or shell command is accepted from the webpage or task file.
The native command still verifies package/reviews/fingerprints/TTL/readback.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys


class NativeRelease:
    def __init__(self, script, expected_sha256, python=None):
        self.script=Path(script).resolve();self.expected=expected_sha256
        self.python=str(Path(python or sys.executable).resolve())
        self.verify()

    def verify(self):
        if not self.script.is_file() or hashlib.sha256(self.script.read_bytes()).hexdigest()!=self.expected:
            raise ValueError('本机固定发布入口指纹不符')

    def call(self,mode,package,ticket=None):
        self.verify()
        argv=[self.python,'-B',str(self.script),mode,'--package',str(package)]
        if ticket is not None:argv+=['--preflight',str(ticket)]
        # Never retry an interrupted native operation. No shell interpolation.
        try:proc=subprocess.run(argv,capture_output=True,text=True,timeout=1800,check=False)
        except subprocess.TimeoutExpired:
            return {'ok':False,'error':'原生发版入口未在期限内返回，暂停并等待人工回读','requires_manual_readback':True}
        try:result=json.loads(proc.stdout)
        except (ValueError,TypeError):
            return {'ok':False,'error':'原生发布入口未返回有效 JSON；未虚报成功','returncode':proc.returncode,'requires_manual_readback':mode=='apply'}
        if not isinstance(result,dict):return {'ok':False,'error':'原生发布回执格式不正确'}
        if proc.returncode!=0:result['ok']=False
        # Native apply's successful receipt includes its actual readback.
        if mode=='apply' and result.get('ok') and (not result.get('activated') or not result.get('read_back',{}).get('ok')):
            result.update(ok=False,error='未收到实际切换和回读通过的证据，不能报告已发布',requires_manual_readback=True)
        return result

    def preflight(self,package):return self.call('preflight',package)
    def apply(self,package,ticket):return self.call('apply',package,ticket)
