#!/usr/bin/env python3
"""Local review center. Verified AgentOS CLI owns review and publication.
Operational mirrors never contain unreviewed model output or user notes.
"""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

ROOT = Path('/Users/suzhizhou/Documents/小八AgentOS')
HERE = Path(__file__).resolve().parent
PENDING = {'pending_delivery_review','revision_requested','delivery_approved','pending_final_review','publication_failed','revalidation_required'}
LABELS = {'pending_delivery_review':'待初审','revision_requested':'待修改','delivery_approved':'待终审','pending_final_review':'终审发布待完成','publication_failed':'发布失败待处理','revalidation_required':'待重新验证','delivery_rejected':'初审不采用','final_rejected':'终审不采用','published':'已发布','retired':'已退役'}

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(text, encoding='utf-8'); os.chmod(temporary, 0o600)
    os.replace(temporary, path)

class Center:
    def __init__(self, root=ROOT, port=8797, config_path=None):
        self.root=Path(root); self.config_path=Path(config_path) if config_path else self.root/'config/agentos.json'
        self.config=json.loads(self.config_path.read_text())
        # Database and working directory follow the configuration, not the code location.
        runtime_dir=Path(self.config['runtime_dir'])
        self.db=runtime_dir/'agentos.sqlite3'
        self.runtime=runtime_dir/'review-center'; self.runtime.mkdir(parents=True,exist_ok=True)
        self.vault=Path(self.config['memory']['obsidian_vault'])
        self.graph=Path(self.config['memory']['knowlp_graph_dir'])
        self.url=f'http://127.0.0.1:{port}'
        self.verifier=Path.home()/'.agents/skills/xiaoba-agentos/scripts/verify-release'
        self.lock=threading.RLock(); self.csrf=secrets.token_urlsafe(32)
        self.sync_status={'ok':False,'state':'starting'}; self.last_fingerprint=None; self.binding={}
        self.journal_path=self.runtime/'submissions.sqlite3'
        with sqlite3.connect(self.journal_path) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS submissions (id TEXT PRIMARY KEY, digest TEXT NOT NULL, request TEXT NOT NULL, result TEXT, created REAL NOT NULL)')
        os.chmod(self.journal_path,0o600)

    def verify(self):
        r=subprocess.run([str(self.verifier),'--config',str(self.config_path)],capture_output=True,text=True,timeout=45)
        value=json.loads(r.stdout)
        if r.returncode or not value.get('ok'): raise RuntimeError('AgentOS 发布完整性检查未通过')
        # The page, the verification and the review action must all use this one configuration.
        if not value.get('config_path') or Path(value['config_path']).resolve()!=self.config_path.resolve():
            raise RuntimeError('发布校验使用的配置与裁决中心配置不一致')
        self.binding=value
        return value

    @contextlib.contextmanager
    def read(self):
        conn=sqlite3.connect('file:'+str(self.db)+'?mode=ro',uri=True,timeout=5); conn.row_factory=sqlite3.Row
        try: yield conn
        finally: conn.close()

    def snapshot(self):
        with self.read() as conn:
            candidates=[dict(r) for r in conn.execute('SELECT mc.*,t.prompt FROM memory_candidates mc JOIN tasks t ON t.id=mc.task_id ORDER BY mc.created_at')]
            reviews=[dict(r) for r in conn.execute('SELECT id,task_id,stage,decision,candidate_sha256,created_at FROM memory_reviews ORDER BY created_at')]
            holds=conn.execute("SELECT count(*) FROM constraint_holds WHERE status='pending'").fetchone()[0]
        known={Path(x['staged_path']).name for x in candidates}
        orphans=[p.name for p in (self.root/'.runtime/memory-outbox').glob('*.md') if p.name not in known]
        return candidates,reviews,holds,sorted(orphans)

    @staticmethod
    def title(prompt):
        first = re.split(r'[。！？\n：]', prompt.strip(), maxsplit=1)[0]
        if len(first) < 6: first = prompt.splitlines()[0]
        return first[:36]+('…' if len(first)>36 else '')

    def item(self,x):
        stage='delivery' if x['status'] in {'pending_delivery_review','revision_requested'} else 'final'
        obsidian='obsidian://open?vault='+quote(self.vault.name)+'&file='+quote('70_复利规则/待审队列')
        validation=x.get('experience_validation') or {}
        state=validation.get('state')
        label=LABELS.get(x['status'],x['status'])
        decisions=['approve','revise','reject'] if x['status'] in PENDING else []
        if x['status']=='delivery_approved' and validation.get('required'):
            label={'awaiting_trial':'待隔离验证','trial_running':'隔离验证中','trial_failed':'验证未通过','ready_for_final':'待终审 · 验证已通过'}.get(state,'待隔离验证')
            if not validation.get('ready'): decisions=['revise','reject']
        return {k:x[k] for k in ('task_id','status','evidence_sha256','updated_at','created_at')} | {
            'title':self.title(x['prompt']) or x['task_id'], 'prompt':x['prompt'],
            'status_label':label, 'stage':stage,'experience_validation':validation,
            'expected':{k:x[k] for k in ('status','evidence_sha256','updated_at')},'obsidian_url':obsidian,
            'allowed_decisions': decisions,
            'published_path':x.get('published_path')}

    def workflow_views(self, task_id=None):
        self.verify()
        request={'verifier':str(self.verifier),'config_path':str(self.config_path),'binding_sha256':self.binding['files_manifest_sha256'],'task_id':task_id}
        proc=subprocess.run([sys.executable,'-B',str(HERE/'workflow_query.py')],input=json.dumps(request),capture_output=True,text=True,timeout=55)
        value=json.loads(proc.stdout)
        if proc.returncode or not value.get('ok'): raise RuntimeError(value.get('error','经验验证状态读取失败'))
        return value.get('items',{})

    def state(self):
        candidates,reviews,holds,orphans=self.snapshot()
        views=self.workflow_views()
        for candidate in candidates: candidate['experience_validation']=views.get(candidate['task_id'])
        return {'ok':True,'items':[self.item(x) for x in candidates if x['status'] in PENDING],
                'total':len(candidates),'reviews':reviews[-30:], 'holds':holds,'orphans':orphans,
                'sync':self.sync_status,'release':self.binding.get('current_release'),
                'updated':time.strftime('%Y-%m-%d %H:%M:%S'), 'csrf':self.csrf,
                'revision':digest([(x['task_id'],x['status'],x['updated_at'],x['evidence_sha256']) for x in candidates]+reviews+[views])}

    def detail(self,task_id):
        with self.read() as conn:
            row=conn.execute('SELECT mc.*,t.prompt FROM memory_candidates mc JOIN tasks t ON t.id=mc.task_id WHERE mc.task_id=?',(task_id,)).fetchone()
            if row is None: raise ValueError('找不到候选')
            x=dict(row)
            reviews=[dict(r) for r in conn.execute('SELECT * FROM memory_reviews WHERE task_id=? ORDER BY created_at',(task_id,))]
        p=Path(x['staged_path']).resolve()
        p.relative_to((self.root/'.runtime/memory-outbox').resolve())
        if not p.exists(): p=p.with_suffix('.published')
        body=p.read_bytes()
        if len(body)>4*1024*1024: raise ValueError('候选过大，请使用本机审核入口')
        valid=hashlib.sha256(body).hexdigest()==x['evidence_sha256']
        x['experience_validation']=self.workflow_views(task_id).get(task_id)
        return self.item(x)|{'body':body.decode('utf-8'), 'hash_matches':valid,'reviews':reviews}

    def sync(self, force=False):
        with self.lock:
            candidates,reviews,holds,orphans=self.snapshot()
            metadata=[{k:x[k] for k in ('task_id','status','evidence_sha256','updated_at')} for x in candidates]
            fingerprint=digest([metadata,reviews,holds,orphans])
            if not force and fingerprint==self.last_fingerprint and self.sync_status.get('ok'): return
            try:
                binding=self.verify()
                views=self.workflow_views()
                for candidate in candidates: candidate['experience_validation']=views.get(candidate['task_id'])
                note=self.vault/'70_复利规则/待审队列.md'; parent=self.vault/'70_复利规则.md'
                stamp=time.strftime('%Y-%m-%d %H:%M:%S')
                pending=[x for x in candidates if x['status'] in PENDING]
                lines=['---','id: "review-queue"','type: "operational-view"','parent: "learning"','status: "active"','authority: false',f'last_verified: "{stamp}"','---','','# 待审队列与裁决回执','',
                       '这是 AgentOS 数据库的动态导航镜像。审核事实以数据库及完整证据哈希为准；下表不代表长期经验。',
                       f'[打开小八裁决中心]({self.url}/) · 当前活动版本 {binding["current_release"]}',
                       f'同步时间：{stamp}；待处理 {len(pending)} 条；全部候选 {len(candidates)} 条。','',
                       '新候选和审核事件发生后自动同步。候选正文与用户意见只在本机裁决中心查看。方法类初审通过后须完成隔离试验与独立双审，再由用户终审；规则和旧流程候选保留两次人工裁决。终审发布沿用正式敏感检查及索引校验。','',
                       '## 候选索引','| 候选编号 | 状态 | 完整证据哈希 |','|---|---|---|']
                for x in candidates:
                    lines.append(f'| [{x["task_id"]}]({self.url}/?candidate={x["task_id"]}) | {self.item(x)["status_label"]} | `{x["evidence_sha256"]}` |')
                lines+=['','## 裁决回执','| 回执 ID | 候选编号 | 阶段 / 决定 |','|---|---|---|']
                for x in reviews:
                    lines.append(f'| `{x["id"]}` | [{x["task_id"]}]({self.url}/?candidate={x["task_id"]}) | {x["stage"]} / {x["decision"]} |')
                lines+=['',f'待核查：格式约束暂存 {holds} 条；无数据库记录的候选文件 {len(orphans)} 个。',
                        '孤儿文件不自动补成候选，不参与裁决。','']
                lockpath=self.root/'.runtime/memory-publication.lock'
                with lockpath.open('a') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX)
                    content=parent.read_text(); link='[[70_复利规则/待审队列]]'
                    if link not in content: atomic(parent,content.rstrip()+'\n- '+link+'\n')
                    atomic(note,'\n'.join(lines))
                    script=Path(binding['release_dir'])/'agentos/scripts/build_knowlp_index.py'
                    args=[sys.executable,str(script),'--vault',str(self.vault),'--graph-dir',str(self.graph)]
                    for command in [args,args+['--verify']]:
                        result=subprocess.run(command,capture_output=True,text=True,timeout=60)
                        if result.returncode: raise RuntimeError('第二大脑索引校验失败：'+result.stderr[-600:])
                    # Independently confirm the index really contains the canonical page.
                    meta=json.loads((self.graph/'meta_index.json').read_text())
                    if '70_复利规则/待审队列.md' not in json.dumps(meta,ensure_ascii=False):
                        raise RuntimeError('索引缺少待审队列原文引用')
                self.last_fingerprint=fingerprint
                self.sync_status={'ok':True,'state':'synced','at':stamp,'pending':len(pending),'note':str(note),'index_sha256':hashlib.sha256((self.graph/'meta_index.json').read_bytes()).hexdigest()}
            except Exception as exc:
                self.sync_status={'ok':False,'state':'retry_pending','error':str(exc),'at':time.strftime('%Y-%m-%d %H:%M:%S')}
            atomic(self.runtime/'sync-status.json',json.dumps(self.sync_status,ensure_ascii=False,indent=2))

    def decide(self,request):
        batch=request.get('id'); entries=request.get('items')
        uuid.UUID(batch)
        if not isinstance(entries,list) or not 1<=len(entries)<=100: raise ValueError('裁决清单为空或过大')
        ids=set()
        for x in entries:
            uuid.UUID(x['task_id'])
            if x['task_id'] in ids: raise ValueError('同一候选不能在一次清单中出现两次')
            ids.add(x['task_id'])
            if x.get('decision') not in {'approve','revise','reject'} or x.get('stage') not in {'delivery','final'}: raise ValueError('无效裁决')
            if not isinstance(x.get('note',''),str) or len(x.get('note',''))>3500: raise ValueError('意见不得超过 3500 字')
            if x['decision']!='approve' and not x.get('note','').strip(): raise ValueError('修改或不采用需要填写意见')
            if set(x.get('expected',{}))!={'status','updated_at','evidence_sha256'}: raise ValueError('缺少完整候选版本')
        with self.lock:
            binding=self.verify(); request_hash=digest(request)
            with sqlite3.connect(self.journal_path) as conn:
                previous=conn.execute('SELECT * FROM submissions WHERE id=?',(batch,)).fetchone()
                if previous:
                    if previous[1]!=request_hash: raise ValueError('同一提交编号不能改变裁决内容')
                    if previous[3]: return json.loads(previous[3])|{'replayed':True,'sync':self.sync_status}
                else:
                    conn.execute('INSERT INTO submissions VALUES(?,?,?,?,?)',(batch,request_hash,json.dumps(request,ensure_ascii=False),None,time.time()))
            results=[]
            for x in entries:
                marker=f'[裁决中心:{batch}:{x["task_id"]}]'
                with self.read() as conn:
                    seen=conn.execute('SELECT id FROM memory_reviews WHERE task_id=? AND instr(note,?)>0',(x['task_id'],marker)).fetchone()
                if seen:
                    # Never repeat publication side effects after a lost response.
                    current=self.detail(x['task_id'])
                    results.append({'task_id':x['task_id'],'ok':False,'recorded':True,'review_id':seen['id'],'status':current['status'],'recovered':True,'error':'已找到审核记录，未重复执行；上次完整回执缺失，请核对发布与后续处理状态'})
                    continue
                try:
                    detail=self.detail(x['task_id'])
                    if detail['expected']!=x['expected'] or detail['stage']!=x['stage'] or x['decision'] not in detail['allowed_decisions'] or not detail['hash_matches']:
                        raise ValueError('候选已变化或证据不一致，请刷新后重新确认')
                    packet=dict(x);packet['note']=x.get('note','').strip()+'\n'+marker
                    action={'verifier':str(self.verifier),'config_path':str(self.config_path),'binding_sha256':binding['files_manifest_sha256'],'packet':packet}
                    proc=subprocess.run([sys.executable,str(HERE/'review_action.py')],input=json.dumps(action,ensure_ascii=False),capture_output=True,text=True,timeout=240)
                    response=json.loads(proc.stdout)
                    with self.read() as conn:
                        receipt=conn.execute('SELECT id FROM memory_reviews WHERE task_id=? AND instr(note,?)>0',(x['task_id'],marker)).fetchone()
                    current=self.detail(x['task_id'])
                    results.append({'task_id':x['task_id'],'ok':response['exit_code']==0,'recorded':bool(receipt),'review_id':receipt['id'] if receipt else None,'status':current['status'],'result':response['result']})
                except Exception as exc:
                    with self.read() as conn:
                        receipt=conn.execute('SELECT id FROM memory_reviews WHERE task_id=? AND instr(note,?)>0',(x['task_id'],marker)).fetchone()
                    results.append({'task_id':x['task_id'],'ok':False,'recorded':bool(receipt),'review_id':receipt['id'] if receipt else None,'error':str(exc)})
            self.sync(force=True)
            response={'id':batch,'ok':all(x['ok'] for x in results),'items':results,'sync':self.sync_status}
            with sqlite3.connect(self.journal_path) as conn:
                conn.execute('UPDATE submissions SET result=? WHERE id=?',(json.dumps(response,ensure_ascii=False),batch))
            return response

    def watch(self):
        # Database metadata changes trigger writes; idle polling never rewrites notes.
        while True:
            try: self.sync()
            except Exception as exc: self.sync_status={'ok':False,'state':'retry_pending','error':str(exc)}
            time.sleep(3 if self.sync_status.get('ok') else 30)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    @property
    def center(self): return self.server.center
    def respond(self,status,payload,kind='application/json; charset=utf-8'):
        body=(json.dumps(payload,ensure_ascii=False).encode() if kind.startswith('application/json') else payload)
        self.send_response(status); self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer');self.send_header('X-Frame-Options','SAMEORIGIN')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
        self.end_headers();self.wfile.write(body)
    def safe_host(self):
        return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'} and self.headers.get('Sec-Fetch-Site')!='cross-site'
    def do_GET(self):
        if not self.safe_host(): return self.respond(403,{'error':'仅允许本机同源访问'})
        path=urlparse(self.path)
        try:
            if path.path=='/': return self.respond(200,(HERE/'index.html').read_bytes(),'text/html; charset=utf-8')
            if path.path=='/app.js': return self.respond(200,(HERE/'app.js').read_bytes(),'text/javascript; charset=utf-8')
            if path.path=='/api/state': return self.respond(200,self.center.state())
            if path.path=='/api/candidate': return self.respond(200,self.center.detail(parse_qs(path.query)['id'][0]))
            return self.respond(404,{'error':'not found'})
        except Exception as exc: return self.respond(503,{'ok':False,'error':str(exc)})
    def do_POST(self):
        allowed={f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'}
        if not self.safe_host() or self.headers.get('Origin') not in allowed or not secrets.compare_digest(self.headers.get('X-Review-CSRF',''),self.center.csrf):
            return self.respond(403,{'ok':False,'error':'请从本机裁决中心确认提交'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=1024*1024 or not self.headers.get('Content-Type','').startswith('application/json'): raise ValueError('无效请求')
            data=json.loads(self.rfile.read(length))
            if self.path=='/api/decisions': return self.respond(200,self.center.decide(data))
            if self.path=='/api/sync':
                self.center.sync(force=True);return self.respond(200,self.center.sync_status)
            return self.respond(404,{'error':'not found'})
        except (ValueError,KeyError,TypeError) as exc: return self.respond(400,{'ok':False,'error':str(exc)})
        except Exception as exc: return self.respond(503,{'ok':False,'error':str(exc)})

class Server(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8797);parser.add_argument('--sync-once',action='store_true')
    # One config for every consumer: the service passes the same file the worker and the entrypoint use.
    parser.add_argument('--config',default=os.environ.get('XIAOBA_CONFIG') or None);args=parser.parse_args()
    os.umask(0o077)
    center=Center(port=args.port,config_path=args.config);center.verify();center.sync(force=True)
    if args.sync_once:
        print(json.dumps(center.sync_status,ensure_ascii=False));return
    server=Server(('127.0.0.1',args.port),Handler);server.center=center
    threading.Thread(target=center.watch,daemon=True).start()
    print(f'Review center listening on {center.url}',flush=True);server.serve_forever()
if __name__=='__main__': main()
