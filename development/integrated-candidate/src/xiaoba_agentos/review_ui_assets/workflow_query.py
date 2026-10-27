"""Read-only query through the same verified release/config as adjudication."""
import json
from pathlib import Path
import subprocess
import sys

def main():
    request=json.load(sys.stdin)
    result=subprocess.run([request['verifier'],'--config',request['config_path']],capture_output=True,text=True,timeout=45)
    binding=json.loads(result.stdout)
    if result.returncode or not binding.get('ok') or binding.get('files_manifest_sha256')!=request['binding_sha256']:
        raise RuntimeError('活动版本已经变化或完整性检查失败，请刷新')
    if Path(binding['config_path']).resolve()!=Path(request['config_path']).resolve():
        raise RuntimeError('验证状态配置不一致')
    source=Path(binding['release_dir'])/'agentos/src'
    if not (source/'xiaoba_agentos/experience_workflow.py').is_file():
        return {'ok':True,'supported':False,'items':{}}
    sys.path.insert(0,str(source))
    from xiaoba_agentos.config import AppConfig
    from xiaoba_agentos.store import EventStore
    from xiaoba_agentos.experience_workflow import trial_summary
    config=AppConfig.load(Path(request['config_path']))
    store=EventStore(config.database_path)
    with store.read_only() as conn:
        if request.get('task_id'):
            rows=conn.execute('SELECT * FROM memory_candidates WHERE task_id=?',(request['task_id'],)).fetchall()
        else:
            rows=conn.execute("SELECT * FROM memory_candidates WHERE status IN ('pending_delivery_review','revision_requested','delivery_approved','pending_final_review','publication_failed','revalidation_required')").fetchall()
        items={row['task_id']:trial_summary(conn,row) for row in rows}
    return {'ok':True,'supported':True,'items':items}

if __name__=='__main__':
    try: print(json.dumps(main(),ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False));sys.exit(1)
