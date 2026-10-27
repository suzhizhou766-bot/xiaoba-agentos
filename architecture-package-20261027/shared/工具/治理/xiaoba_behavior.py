#!/usr/bin/env python3
"""Agent-neutral behavior and strategy loop.

This store is deliberately separate from the authority brain. It learns reusable
procedures from observed executions without treating a single success as a rule.
No example domain is embedded here; callers provide domain-specific objects.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("XIAOBA_ASSISTANT_ROOT", Path.home() / "Documents/私人助理")).resolve()
STORE = Path(os.environ.get("XIAOBA_BEHAVIOR_DIR", ROOT / ".runtime/learning/behavior")).resolve()
EVENTS = STORE / "events.jsonl"
BEHAVIORS = STORE / "behaviors.json"
LOCK = STORE / ".lock"
SCHEMA_VERSION = 1


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def key_for(*values):
    return hashlib.sha256(canonical(values).encode()).hexdigest()[:32]


@contextmanager
def locked():
    STORE.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as fh:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        try: yield
        finally:
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass


def load_events():
    if not EVENTS.exists(): return []
    out=[]
    for line in EVENTS.read_text(encoding="utf-8").splitlines():
        try:
            x=json.loads(line)
            if isinstance(x,dict): out.append(x)
        except json.JSONDecodeError: continue
    return out


def append_event(event):
    with locked():
        old=load_events()
        if any(x.get("idempotency_key")==event["idempotency_key"] for x in old):
            return {"ok":True,"duplicate":True,"event":next(x for x in old if x.get("idempotency_key")==event["idempotency_key"])}
        with EVENTS.open("a",encoding="utf-8") as fh:
            fh.write(canonical(event)+"\n"); fh.flush(); os.fsync(fh.fileno())
    return {"ok":True,"duplicate":False,"event":event}


def load_behaviors():
    if not BEHAVIORS.exists(): return {}
    try: return json.loads(BEHAVIORS.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): return {}


def save_behaviors(data):
    tmp=BEHAVIORS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    os.replace(tmp,BEHAVIORS)


def similarity(goal, context, item):
    g=set(str(goal).lower().split())
    ig=set(str(item.get("goal","")).lower().split())
    overlap=len(g & ig)/max(1,len(g|ig))
    ctx=item.get("context",{})
    same=sum(context.get(k)==ctx.get(k) for k in set(context)|set(ctx) if context.get(k) is not None and ctx.get(k) is not None)
    denom=max(1,len(set(context)|set(ctx)))
    return overlap*0.7 + same/denom*0.3


def recommend(args):
    data=load_behaviors(); rows=[]
    for bid,item in data.items():
        score=similarity(args.goal,args.context,item)
        if score >= args.min_score:
            success=item.get("success_count",0); failure=item.get("failure_count",0)
            reliability=(success+1)/(success+failure+2)
            rows.append({"behavior_id":bid,"score":round(score*reliability,4),"match":round(score,4),"reliability":round(reliability,4),"behavior":item})
    rows.sort(key=lambda x:x["score"],reverse=True)
    return {"ok":True,"goal":args.goal,"recommendations":rows[:args.limit],"count":len(rows)}


def observe(args):
    outcome=args.outcome
    action={"type":args.action_type,"parameters":args.parameters or {}}
    context=args.context or {}
    behavior_id=key_for(args.goal,context,action)
    with locked():
        data=load_behaviors(); item=data.get(behavior_id,{"schema_version":SCHEMA_VERSION,"behavior_id":behavior_id,"goal":args.goal,"context":context,"action":action,"expected_outcome":args.expected_outcome,"success_count":0,"failure_count":0,"total_cost":0.0,"observations":[]})
        success=outcome == "success"
        item["success_count"] += int(success); item["failure_count"] += int(not success)
        item["total_cost"] += float(args.cost)
        item["last_observed_at"] = now(); item["observations"].append({"outcome":outcome,"observed_result":args.observed_result,"evidence":args.evidence,"failure_reason":args.failure_reason,"at":now()})
        item["observations"] = item["observations"][-50:]
        item["reuse_level"] = "validated" if item["success_count"] >= 1 and item["success_count"] >= item["failure_count"] else "experimental"
        data[behavior_id]=item; save_behaviors(data)
    event={"schema_version":SCHEMA_VERSION,"event_id":"obs_"+uuid.uuid4().hex,"idempotency_key":"observation:"+key_for(behavior_id,args.observed_result,args.evidence,outcome),"event_type":"behavior.observed","behavior_id":behavior_id,"goal":args.goal,"outcome":outcome,"evidence":args.evidence,"created_at":now()}
    return {"ok":True,"behavior_id":behavior_id,"behavior":item,"event":append_event(event)}


def main():
    ap=argparse.ArgumentParser(description="行为记忆与策略适配")
    sub=ap.add_subparsers(dest="cmd",required=True)
    r=sub.add_parser("recommend"); r.add_argument("--goal",required=True); r.add_argument("--context-json",default="{}"); r.add_argument("--min-score",type=float,default=0.15); r.add_argument("--limit",type=int,default=5)
    o=sub.add_parser("observe"); o.add_argument("--goal",required=True); o.add_argument("--action-type",required=True); o.add_argument("--parameters-json",default="{}"); o.add_argument("--context-json",default="{}"); o.add_argument("--expected-outcome",required=True); o.add_argument("--observed-result",required=True); o.add_argument("--outcome",choices=["success","failure","partial"],required=True); o.add_argument("--evidence",action="append",default=[]); o.add_argument("--failure-reason",default=None); o.add_argument("--cost",type=float,default=0)
    s=sub.add_parser("status")
    args=ap.parse_args()
    try:
        if args.cmd=="recommend": args.context=json.loads(args.context_json); result=recommend(args)
        elif args.cmd=="observe": args.context=json.loads(args.context_json); args.parameters=json.loads(args.parameters_json); result=observe(args)
        else:
            data=load_behaviors(); result={"ok":True,"behaviors":len(data),"store":str(STORE),"validated":sum(v.get("reuse_level")=="validated" for v in data.values())}
        print(json.dumps(result,ensure_ascii=False,indent=2)); return 0 if result.get("ok") else 1
    except (ValueError,OSError,json.JSONDecodeError) as e:
        print(json.dumps({"ok":False,"error":str(e)},ensure_ascii=False)); return 1
if __name__=="__main__": raise SystemExit(main())
