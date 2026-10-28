"""Explicit candidate host entry for the user's actual workday-start message."""
import argparse
import json
import re
import urllib.request
from urllib.parse import urlsplit


def start(url,message_id,kinds,selections):
    parsed=urlsplit(url)
    if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port or parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('开工入口只接受已知本机监看服务的根地址')
    url=url.rstrip('/')
    html=urllib.request.urlopen(url+'/',timeout=10).read().decode()
    token=re.search(r'data-page-token="([^"]+)"',html)
    if not token:raise ValueError('本机服务没有提供当前页面令牌，未发送开工任务')
    payload={'user_message':'上班了小八助理','message_id':message_id,'required_kinds':kinds,'selections':selections}
    request=urllib.request.Request(url+'/api/agent-pool/workday-start',json.dumps(payload).encode(),
        headers={'Content-Type':'application/json','Origin':url,'X-Xiaoba-Token':token[1]},method='POST')
    return json.load(urllib.request.urlopen(request,timeout=300))


def main():
    parser=argparse.ArgumentParser(description='候选开工链路；只能在真实用户开工口令后调用，不部署生产入口')
    parser.add_argument('--url',required=True)
    parser.add_argument('--user-message',choices=['上班了小八助理'],required=True)
    parser.add_argument('--message-id',required=True)
    parser.add_argument('--kind',choices=['dsh','peer'],action='append',required=True)
    parser.add_argument('--select',action='append',default=[],help='本次计划已知的槽位=节点ID；只补问缺项')
    args=parser.parse_args();selections={}
    for value in args.select:
        kind,sep,node=value.partition('=')
        if not sep or kind not in args.kind or not node or kind in selections:parser.error('节点选择格式应为当前槽位=节点ID')
        selections[kind]=node
    result=start(args.url,args.message_id,args.kind,selections)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result.get('result',{}).get('route')=='ready' else 1


if __name__=='__main__':raise SystemExit(main())
