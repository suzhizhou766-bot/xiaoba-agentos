"""Run the verified release CLI, adding an atomic browser-snapshot guard.
No state transitions or publication logic are reimplemented here.
"""
from __future__ import annotations
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys


def execute(packet, config_path, source):
    sys.path.insert(0, str(source))
    from xiaoba_agentos import cli
    from xiaoba_agentos.store import EventStore
    expected = packet['expected']

    class GuardedStore(EventStore):
        def initialize(self):
            if not self.schema_status()['ok']:
                raise RuntimeError('数据库版本需要维护，请先处理迁移')

        def review_memory(self, task_id, stage, decision, note, candidate_sha256):
            # Hold SQLite's write lock through comparison and the native review.
            with super().connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT * FROM memory_candidates WHERE task_id=?', (task_id,)).fetchone()
                if not row or any(row[k] != expected[k] for k in ('status', 'evidence_sha256', 'updated_at')):
                    raise ValueError('候选已变化，请刷新并重新裁决')
                if candidate_sha256 != expected['evidence_sha256']:
                    raise ValueError('正文已变化，请重新核实证据')
                p = Path(row['staged_path'])
                if not p.exists():
                    p = p.with_suffix('.published')
                if hashlib.sha256(p.read_bytes()).hexdigest() != expected['evidence_sha256']:
                    raise ValueError('候选文件与证据哈希不一致')
                original_connect = self.connect
                @contextlib.contextmanager
                def guarded_connection():
                    yield conn
                self.connect = guarded_connection
                try:
                    return super().review_memory(task_id, stage, decision, note, candidate_sha256)
                finally:
                    self.connect = original_connect

    cli.EventStore = GuardedStore
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(['--config', str(config_path), 'review', packet['task_id'],
                         '--stage', packet['stage'], '--decision', packet['decision'], '--note', packet['note']])
    return {'exit_code': code, 'result': json.loads(output.getvalue())}


def main():
    request = json.load(sys.stdin)
    config_path = Path(request['config_path'])
    verified = subprocess.run([request['verifier'], '--config', str(config_path)],
                              capture_output=True, text=True, timeout=45)
    binding = json.loads(verified.stdout)
    if verified.returncode or not binding.get('ok'):
        raise RuntimeError('当前发布校验未通过')
    # Execute on the configuration the review center showed, and only if the
    # release was verified against that same configuration.
    if not binding.get('config_path') or Path(binding['config_path']).resolve() != config_path.resolve():
        raise RuntimeError('发布校验使用的配置与裁决中心配置不一致')
    if binding['files_manifest_sha256'] != request['binding_sha256']:
        raise RuntimeError('活动版本已经变化，请刷新裁决中心')
    result = execute(request['packet'], config_path, Path(binding['release_dir'])/'agentos/src')
    print(json.dumps(result, ensure_ascii=False))

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'exit_code': 1, 'result': {'status': 'blocked', 'error': str(exc)}}, ensure_ascii=False))
        sys.exit(1)
