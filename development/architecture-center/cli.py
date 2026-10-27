#!/usr/bin/env python3
"""Agent-neutral update handoff. Final publishing exists only in monitor UI.

JSON actions arrive through stdin; no arbitrary shell commands or active writes.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,required=True)
    parser.add_argument('--repository',type=Path,default=Path(__file__).resolve().parents[2])
    parser.add_argument('--catalog',type=Path,default=Path(__file__).with_name('MODULES.json'))
    args=parser.parse_args()
    spec=importlib.util.spec_from_file_location('architecture_center_core',Path(__file__).with_name('core.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    center=module.Center(args.repository,args.workspace,args.catalog)
    request=json.load(sys.stdin);action=request.pop('action')
    operations={'list':lambda:center.state(),'read':lambda:center.get(**request),
        'claim':lambda:center.claim(**request),'update':lambda:center.agent_update(**request),'validate':lambda:center.validate(**request),
        'register_package':lambda:center.register_package(**request)}
    if action not in operations:raise ValueError('Agent 入口仅允许读取、认领、候选写入与交接；不提供正式发布动作')
    print(json.dumps({'ok':True,'result':operations[action]()},ensure_ascii=False,indent=2))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False));sys.exit(1)
