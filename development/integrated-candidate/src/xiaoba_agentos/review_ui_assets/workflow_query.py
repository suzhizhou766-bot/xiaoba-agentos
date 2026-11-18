"""Read-only query through the same verified release/config as adjudication."""
import json
import math
import sqlite3
from pathlib import Path
import subprocess
import sys


def assessment_projection(conn, task_id):
    """卡 07 C07-03：只读透出价值分与暂停依据，按真实动态节点身份展示。

    不再把结论写死成 dsh_verdict / peer_verdict 两个固定槽：
      - memory.value_assessed.scores 的键就是实际审查节点 id，分数与结论按节点展示；
      - memory.validation_paused 用 reasons/failure_class/needs_info 表达暂停，不伪造通过。
    读不到或坏事件时 available=False，绝不缓存成假健康。
    """
    out = {"available": False, "value_assessed": None, "validation_paused": None, "problems": []}
    readable = []
    for event_type in ("memory.value_assessed", "memory.validation_paused"):
        try:
            row = conn.execute(
                "SELECT payload_json FROM events WHERE task_id=? AND event_type=? "
                "ORDER BY sequence DESC LIMIT 1", (task_id, event_type)).fetchone()
            readable.append(True)
            if row is None:
                continue
            value = json.loads(row["payload_json"])
            if not isinstance(value, dict):
                raise ValueError("事件不是对象")
            if event_type == "memory.value_assessed":
                if not isinstance(value.get("scores"), dict):
                    raise ValueError("scores 不是对象")
                reviewers = []
                for name, score in value["scores"].items():
                    if (not isinstance(name, str) or not name.strip() or type(score) not in (int, float)
                            or not math.isfinite(score) or not 0 <= score <= 10):
                        out["problems"].append("memory.value_assessed 含无效节点分数，已隔离")
                        continue
                    reviewers.append({"reviewer": name, "score": score})
                out["value_assessed"] = {
                    "version": value.get("version"),
                    "reviewers": sorted(reviewers, key=lambda r: r["reviewer"]),
                    "average": value.get("average"), "spread": value.get("spread"),
                    "samples": value.get("samples"), "decision": value.get("decision"),
                    "reason": value.get("reason"), "candidate_sha256": value.get("candidate_sha256"),
                    "human_approval": value.get("human_approval"),
                }
            else:
                out["validation_paused"] = {
                    "round": value.get("round"), "reason": value.get("pause_reason"),
                    "failure_class": value.get("failure_class"), "needs_info": bool(value.get("needs_info")),
                    "legacy_verdicts": {k: value[k] for k in ("dsh_verdict", "peer_verdict") if k in value},
                }
        except sqlite3.Error as exc:
            readable.append(False)
            out["problems"].append(event_type + " 读取失败：" + type(exc).__name__)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            out["problems"].append(event_type + " 结构无效，已隔离：" + type(exc).__name__)
    out["available"] = any(readable)
    return out


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
    if request.get('delivery_query'):
        from xiaoba_agentos.delivery import pending_tasks, detail_tx
        if request.get('delivery_task_id'):
            with store.read_only() as conn:
                delivery = detail_tx(conn, request['delivery_task_id'], request.get('delivery_version'))
        else:
            delivery = pending_tasks(store)
        return {'ok': True, 'delivery': delivery}
    with store.read_only() as conn:
        if request.get('task_id'):
            rows=conn.execute('SELECT * FROM memory_candidates WHERE task_id=?',(request['task_id'],)).fetchall()
        else:
            rows=conn.execute("SELECT * FROM memory_candidates WHERE status IN ('pending_delivery_review','revision_requested','delivery_approved','pending_final_review','publication_failed','revalidation_required')").fetchall()
        items={}
        for row in rows:
            summary = trial_summary(conn, row)
            # 卡07 C07-03：附加价值分/暂停投影，按动态节点身份展示，不覆盖旧 experience_validation。
            summary["assessment"] = assessment_projection(conn, row['task_id'])
            items[row['task_id']] = summary
    from xiaoba_agentos.formal_review import items as formal_items
    items['_formal'] = formal_items(store, include_closed=True)
    return {'ok':True,'supported':True,'items':items}

if __name__=='__main__':
    try: print(json.dumps(main(),ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False));sys.exit(1)
