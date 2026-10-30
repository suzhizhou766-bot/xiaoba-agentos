"""Module proposals, scoped agent handoff and guarded release coordination.

The database is authoritative. 待发布 is an editable projection for handoff;
unregistered or changed files cannot silently inherit validation or preflight.
No operation here writes to active module sources or approves experience.
"""
from contextlib import contextmanager
import ast
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import subprocess
import time
import uuid


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def safe(root, relative):
    root=Path(root)
    rel=PurePosixPath(relative)
    if not relative or rel.is_absolute() or any(p in {'','..','.'} for p in rel.parts) or '\\' in relative:
        raise ValueError('不安全的相对路径')
    current=root
    if root.is_symlink():raise ValueError('工作根目录不能是软链接')
    for part in rel.parts:
        current=current/part
        if current.is_symlink():raise ValueError('候选路径不允许软链接')
    current.resolve().relative_to(root.resolve())
    return current


def atomic(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with open(temp,'xb') as output:output.write(data)
        os.chmod(temp,0o600);os.replace(temp,path)
    finally:
        if temp.exists():temp.unlink()


class Conflict(ValueError):pass


class Center:
    UNKNOWN='release_unknown'
    RECOVERY={'applied','not_applied'}

    def __init__(self, repository, workspace, catalog, driver=None):
        self.repository=Path(repository).resolve();self.workspace=Path(workspace).resolve()
        self.pending=self.workspace/'待发布';self.pending.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.catalog=json.loads(Path(catalog).read_text())
        if self.catalog.get('schema_version')!=1:raise ValueError('模块登记版本不支持')
        self.modules={m['id']:m for m in self.catalog['modules']}
        self.driver=driver;self.database=self.workspace/'upgrade-center.sqlite3'
        with self.tx() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS changes(id TEXT PRIMARY KEY,data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,change_id TEXT,type TEXT,actor TEXT,data TEXT,at REAL,previous TEXT,hash TEXT);
              CREATE TABLE IF NOT EXISTS publishes(id TEXT PRIMARY KEY,change_id TEXT,request_hash TEXT,state TEXT,result TEXT);
              CREATE UNIQUE INDEX IF NOT EXISTS one_running_publish ON publishes(state) WHERE state='running';
            ''')
        os.chmod(self.database,0o600)
        self._recover_interrupted()

    def _recover_interrupted(self):
        """A publish that was still 'running' at startup has an unknown outcome; never retry it."""
        try:
            with self.tx() as db:
                for row in db.execute('SELECT id,data FROM changes ORDER BY rowid').fetchall():
                    change=json.loads(row['data'])
                    if change.get('state')!='publishing':continue
                    change['state']=self.UNKNOWN
                    change['release_unknown']={'reason':'interrupted_publish','detected_at':time.time(),
                        'note':'进程在正式发布中途退出，无法确认是否已生效；须人工核对后再恢复'}
                    change.pop('preflight',None);change.pop('confirmation',None)
                    self._event(db,change,'release_unknown','monitor-user',{'reason':'重启时发现遗留的发布中记录，结果未知'})
                    self._save(db,change)
                    db.execute("UPDATE publishes SET state=? WHERE change_id=? AND state='running'",(self.UNKNOWN,change['id']))
        except Conflict:
            # Audit chain is broken: no writes at all, and the stale 'running' row keeps publish blocked.
            return

    def _unknown(self,db):
        return [row['id'] for row in db.execute('SELECT id,data FROM changes ORDER BY rowid')
                if json.loads(row['data']).get('state')==self.UNKNOWN]

    def _guard_unknown(self,db):
        blocked=self._unknown(db)
        if blocked:raise Conflict('存在发布结果未知的候选，需先人工核对并恢复：'+'、'.join(blocked))

    @contextmanager
    def tx(self):
        db=sqlite3.connect(self.database,timeout=30,isolation_level=None);db.row_factory=sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE');yield db;db.commit()
        except BaseException:db.rollback();raise
        finally:db.close()

    def _load(self,db,cid):
        if not isinstance(cid,str) or not re.fullmatch('[0-9a-f-]{36}',cid):raise ValueError('无效改动 ID')
        row=db.execute('SELECT data FROM changes WHERE id=?',(cid,)).fetchone()
        if row is None:raise ValueError('找不到待发布改动')
        return json.loads(row['data'])

    def _event(self,db,change,kind,actor,payload):
        self._verify_chain(db)
        last=db.execute('SELECT hash FROM events ORDER BY sequence DESC LIMIT 1').fetchone()
        prev=last['hash'] if last else ''
        stamp=time.time();data=encoded(payload).decode()
        digest=sha(encoded([prev,change['id'],kind,actor,payload,stamp]))
        db.execute('INSERT INTO events(change_id,type,actor,data,at,previous,hash) VALUES(?,?,?,?,?,?,?)',
                   (change['id'],kind,actor,data,stamp,prev,digest))

    def _verify_chain(self,db):
        previous=''
        for row in db.execute('SELECT * FROM events ORDER BY sequence'):
            payload=json.loads(row['data'])
            expected=sha(encoded([previous,row['change_id'],row['type'],row['actor'],payload,row['at']]))
            if row['previous']!=previous or row['hash']!=expected:raise Conflict('升级审计链损坏，停止写入')
            previous=row['hash']
        return previous

    def _folder(self,change):return safe(self.pending,change['id'])

    def _save(self,db,change):
        change['updated_at']=time.time();change['revision']+=1
        db.execute('INSERT OR REPLACE INTO changes VALUES (?,?)',(change['id'],encoded(change).decode()))
        folder=self._folder(change);folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        # File projections contain no approval challenge or process authority.
        public={k:v for k,v in change.items() if k not in {'preflight','confirmation'}}
        atomic(safe(folder,'change.json'),encoded(public)+b'\n')
        permission={'change_id':change['id'],'module_id':change['module_id'],
            'allowed':['read','claim','renew','write_staged_files','checkpoint','validate','register_package','complete'],
            'cli':str(Path(__file__).with_name('cli.py')),'workspace':str(self.workspace),
            'write_root':str(folder/'files'),'editable_prefixes':self.modules[change['module_id']]['editable_prefixes'],
            'excluded_module_spec_roots':[str(PurePosixPath(m['spec_path']).parent)+'/' for mid,m in self.modules.items() if mid!=change['module_id']],
            'forbidden':['edit_active_sources','edit_credentials','publish_without_user_confirmation'],
            'authorization':'local owner filesystem / monitor session; actor labels are audit metadata, not authentication'}
        atomic(safe(folder,'ACCESS.json'),encoded(permission)+b'\n')
        handoff=f"# 升级接续任务 {change['id']}\n\n模块：{change['module_id']}\n目标：{change['goal']}\n状态：{change['state']}\n基线：{change['base_version']} / {change['base_source']}\n最近接续：{change.get('checkpoint','尚未认领')}\n下一步：{change.get('next_step','认领、实施、验证并准备正式包')}\n\n读取 ACCESS.json 中的 cli 与 workspace，用 JSON stdin 入口读取权威状态并认领接续。协议见源码同目录 HANDOFF-PROTOCOL.md。\n只在 ACCESS.json 允许的候选路径写入；经本中心命令更新清单。不得直接改活动源码或凭据。正式发版由用户在监看中心对具体预检结果确认。\n"
        atomic(safe(folder,'HANDOFF.md'),handoff.encode())
        history=[dict(row) for row in db.execute('SELECT * FROM events WHERE change_id=? ORDER BY sequence',(change['id'],))]
        atomic(safe(folder,'events.jsonl'),b''.join(encoded(r)+b'\n' for r in history))

    def module(self,mid):
        if mid not in self.modules:raise ValueError('未知模块')
        m=self.modules[mid];path=safe(self.repository,m['spec_path'])
        data=path.read_bytes()
        return {**m,'body':data.decode(),'sha256':sha(data)}

    def _target(self,change,path):
        rel=PurePosixPath(path)
        if any(mid!=change['module_id'] and path.startswith(str(PurePosixPath(m['spec_path']).parent)+'/') for mid,m in self.modules.items()):
            raise ValueError('其它模块规范需要独立提案，不能借上层目录越界写入')
        if any(part.startswith('.') for part in rel.parts) or rel.name.lower() in {'auth.json','keypool.json'}:
            raise ValueError('不允许编辑凭据、隐藏目录或认证文件')
        if not any(path.startswith(prefix) for prefix in self.modules[change['module_id']]['editable_prefixes']):
            raise ValueError('超出本模块候选写入范围')
        if rel.suffix.lower() not in {'.md','.py','.js','.css','.html','.json','.toml','.txt','.sh','.mjs'}:
            raise ValueError('不支持的候选文件类型')
        safe(self.repository,path)
        return safe(self._folder(change),'files/'+path)

    def _write(self,change,path,body):
        if not isinstance(body,str) or len(body.encode())>1024*1024:raise ValueError('候选文件必须是 1 MiB 内的文本')
        target=self._target(change,path);baseline=safe(self.repository,path)
        before=baseline.read_bytes() if baseline.is_file() else b''
        old=change['files'].get(path)
        if old and old['before_sha256']!=sha(before):raise Conflict('基线源码已变化，先重新盘点')
        atomic(target,body.encode())
        change['files'][path]={'before_sha256':sha(before),'after_sha256':sha(body.encode()),'bytes':len(body.encode())}
        diff=''.join(difflib.unified_diff(before.decode().splitlines(True),body.splitlines(True),fromfile='a/'+path,tofile='b/'+path))
        atomic(safe(self._folder(change),'diffs/'+sha(path.encode())+'.patch'),diff.encode())
        change.pop('validation',None);change.pop('package',None);change.pop('preflight',None);change.pop('confirmation',None)

    def create(self,module_id,body,expected_sha256,goal,base_version,base_source,target_version=''):
        module=self.module(module_id)
        if module['sha256']!=expected_sha256:raise Conflict('模块规范已变化，请先重新读取')
        if not all(isinstance(x,str) and x.strip() for x in (goal,base_version,base_source)):raise ValueError('需要改动目标、基线版本及源码记录')
        if target_version and not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+',target_version):raise ValueError('目标版本格式必须为 vX.Y.Z')
        change={'id':str(uuid.uuid4()),'module_id':module_id,'goal':goal,'base_version':base_version,'target_version':target_version,
                'base_source':base_source,'created_at':time.time(),'revision':0,'state':'awaiting_agent','files':{},'lease':None}
        with self.tx() as db:
            self._write(change,module['spec_path'],body)
            self._event(db,change,'created','monitor-user',{'module':module_id,'goal':goal})
            self._save(db,change)
        return self.get(change['id'])

    def _revision(self,change,expected):
        if expected!=change['revision']:raise Conflict('候选版本已变化，请刷新后重试')
        if change['state']==self.UNKNOWN:raise Conflict('发布结果未知，须先人工核对并恢复：'+change['id'])
        if change['state'] in {'publishing','published'}:raise Conflict('正式发版中或已发布的候选不可修改')

    def _lease(self,change,actor,lease_id):
        lease=change.get('lease')
        if not lease or lease['id']!=lease_id or lease['actor']!=actor or lease['expires_at']<time.time():
            raise Conflict('任务执行权不存在或已过期')

    def claim(self,cid,actor,expected_revision):
        if not isinstance(actor,str) or not actor.strip():raise ValueError('需要接续者标识')
        with self.tx() as db:
            change=self._load(db,cid);self._revision(change,expected_revision)
            if change.get('lease') and change['lease']['expires_at']>=time.time():raise Conflict('已有 Agent 持有本任务执行权')
            change['lease']={'id':str(uuid.uuid4()),'actor':actor,'expires_at':time.time()+900}
            change['state']='claimed'
            self._event(db,change,'claimed',actor,{'lease_id':change['lease']['id']});self._save(db,change)
        return self.get(cid)

    def agent_update(self,cid,actor,lease_id,expected_revision,operation,**data):
        with self.tx() as db:
            change=self._load(db,cid);self._revision(change,expected_revision);self._lease(change,actor,lease_id)
            if operation=='write':
                self._write(change,data['path'],data['body']);change['state']='claimed'
            elif operation=='checkpoint':
                if not data.get('summary') or not data.get('next_step'):raise ValueError('需要进度与下一步')
                change['checkpoint']=data['summary'];change['next_step']=data['next_step']
            elif operation=='renew':change['lease']['expires_at']=time.time()+900
            elif operation=='complete':
                if not data.get('summary'):raise ValueError('需要交接总结')
                change['checkpoint']=data['summary'];change['lease']=None;change['state']='awaiting_validation'
            else:raise ValueError('未知接续动作')
            self._event(db,change,operation,actor,{k:v for k,v in data.items() if k!='body'});self._save(db,change)
        return self.get(cid)

    def _integrity(self,change):
        folder=self._folder(change);expected=set(change['files'])
        if any(p.is_symlink() for p in (folder/'files').rglob('*')):raise Conflict('候选目录不允许软链接')
        actual={p.relative_to(folder/'files').as_posix() for p in (folder/'files').rglob('*') if p.is_file()}
        if actual!=expected:raise Conflict('候选目录出现未登记或缺失文件')
        for path,item in change['files'].items():
            proposed=self._target(change,path)
            baseline=safe(self.repository,path)
            if sha(proposed.read_bytes())!=item['after_sha256']:raise Conflict('候选文件与清单不一致：'+path)
            if sha(baseline.read_bytes() if baseline.is_file() else b'')!=item['before_sha256']:raise Conflict('活动开发基线漂移：'+path)
        return sha(encoded({'module_id':change['module_id'],'files':change['files'],
                            'base_version':change['base_version'],'base_source':change['base_source']}))

    def validate(self,cid,expected_revision):
        with self.tx() as db:
            change=self._load(db,cid);self._revision(change,expected_revision)
            if change.get('lease'):raise Conflict('Agent 尚未交接完成，不能冻结验证')
            digest=self._integrity(change);checks=[]
            for path in change['files']:
                target=self._target(change,path);body=target.read_text();ok=True;detail='文本及清单检查通过'
                try:
                    suffix=target.suffix.lower()
                    if not body.strip():raise ValueError('内容为空')
                    if suffix=='.py':ast.parse(body)
                    if suffix=='.json':json.loads(body)
                    if suffix in {'.js','.mjs'}:
                        result=subprocess.run(['node','--check',str(target)],capture_output=True,text=True,timeout=20)
                        if result.returncode:raise ValueError(result.stderr[:1000])
                except (ValueError,SyntaxError,OSError,subprocess.TimeoutExpired) as exc:ok=False;detail=str(exc)
                checks.append({'path':path,'ok':ok,'detail':detail})
            change['validation']={'ok':all(c['ok'] for c in checks),'scope':'syntax_and_manifest_only','fingerprint':digest,'checks':checks}
            change['state']=('awaiting_preflight' if change.get('package') else 'awaiting_package') if change['validation']['ok'] else 'validation_failed'
            change.pop('preflight',None);change.pop('confirmation',None)
            self._event(db,change,'validated','monitor-user',change['validation']);self._save(db,change)
        return self.get(cid)

    def _package(self,change):
        package=change.get('package')
        if not package:raise ValueError('尚未接续完成受审正式包')
        root=safe(self._folder(change),'artifacts/package')
        entries={}
        for path in root.rglob('*'):
            rel=path.relative_to(root).as_posix();safe(root,rel)
            if path.is_file():entries[rel]=sha(path.read_bytes())
        if sha(encoded(entries))!=package['digest']:raise Conflict('正式包文件在登记后发生变化')
        for path,target in package['mapping'].items():
            if sha(safe(root,target).read_bytes())!=change['files'][path]['after_sha256']:raise Conflict('正式包未包含本次候选文件')
        return root

    def register_package(self,cid,actor,lease_id,expected_revision,mapping):
        with self.tx() as db:
            change=self._load(db,cid);self._revision(change,expected_revision);self._lease(change,actor,lease_id)
            self._integrity(change)
            if not isinstance(mapping,dict) or set(mapping)!=set(change['files']):raise ValueError('正式包映射必须逐项覆盖本次候选文件')
            root=safe(self._folder(change),'artifacts/package')
            if not root.is_dir():raise ValueError('需要先在本候选 artifacts/package 构建正式包')
            entries={}
            for p in root.rglob('*'):
                rel=p.relative_to(root).as_posix();safe(root,rel)
                if p.is_file():entries[rel]=sha(p.read_bytes())
            for path,target in mapping.items():
                if sha(safe(root,target).read_bytes())!=change['files'][path]['after_sha256']:raise ValueError('包内文件与候选内容不同')
            for required in ('FILES.sha256','RELEASE.json','agentos/scripts/switch_release.py'):
                if required not in entries:raise ValueError('正式包缺少 '+required)
            change['package']={'digest':sha(encoded(entries)),'mapping':mapping}
            change.pop('preflight',None);change.pop('confirmation',None)
            self._event(db,change,'package_registered',actor,{'digest':change['package']['digest']});self._save(db,change)
        return self.get(cid)

    def prepare_release(self,cid,expected_revision):
        with self.tx() as db:
            change=self._load(db,cid);self._guard_unknown(db);self._revision(change,expected_revision)
            if change.get('lease'):raise Conflict('尚有 Agent 执行权，不能预检发布')
            digest=self._integrity(change)
            if not change.get('validation',{}).get('ok') or change['validation']['fingerprint']!=digest:
                raise ValueError('必须先完成当前候选的检查')
            package=self._package(change)
            if self.driver is None:raise ValueError('本机原生发布执行器未配置，不能进行正式发版')
            report=self.driver.preflight(package)
            if report.get('ok') and change.get('target_version') and report.get('version')!=change['target_version']:
                report={**report,'ok':False,'error':'正式包版本与本次目标版本不一致'}
            change['last_preflight']=report
            atomic(safe(self._folder(change),'reports/preflight.json'),encoded(report)+b'\n')
            if not report.get('ok'):
                change['state']='preflight_failed';change.pop('preflight',None);change.pop('confirmation',None)
                change['release_error']=report.get('error','原生预检未通过')
            else:
                change['preflight']={'native':report,'source_fingerprint':digest,'package_digest':change['package']['digest']}
                change['confirmation']=str(uuid.uuid4());change['state']='awaiting_release'
                change.pop('release_error',None)
            self._event(db,change,'preflight','monitor-user',{'ok':report.get('ok'),'error':report.get('error')});self._save(db,change)
            revision=change['revision'];confirmation=change.get('confirmation')
        return {'change':self.get(cid),'revision':revision,'confirmation':confirmation,'preflight':report}

    def publish(self,cid,expected_revision,confirmation,request_id):
        if not isinstance(request_id,str) or not request_id:raise ValueError('需要稳定发布请求 ID')
        request_hash=sha(encoded([cid,expected_revision,confirmation]))
        with self.tx() as db:
            old=db.execute('SELECT * FROM publishes WHERE id=?',(request_id,)).fetchone()
            if old:
                if old['request_hash']!=request_hash:raise Conflict('同一发布请求 ID 的内容冲突')
                return json.loads(old['result']) if old['result'] else {'ok':False,'state':'publishing','request_id':request_id}
            change=self._load(db,cid);self._guard_unknown(db);self._revision(change,expected_revision)
            if change['state']!='awaiting_release' or not confirmation or confirmation!=change.get('confirmation'):
                raise Conflict('请从本次具体预检结果确认正式发版')
            fingerprint=self._integrity(change);package=self._package(change)
            ticket=change['preflight']['native']
            if fingerprint!=change['preflight']['source_fingerprint'] or time.time()>ticket.get('expires_at',0):
                raise Conflict('候选或预检已变化 / 过期，请重新预检')
            if self.driver is None:raise ValueError('原生发布执行器未配置')
            try:db.execute('INSERT INTO publishes VALUES (?,?,?,?,NULL)',(request_id,cid,request_hash,'running'))
            except sqlite3.IntegrityError as exc:raise Conflict('已有正式发布正在执行') from exc
            change['state']='publishing';change.pop('confirmation',None)
            self._event(db,change,'publish_started','monitor-user',{'request_id':request_id,'version':ticket.get('version')});self._save(db,change)
            ticket_path=safe(self._folder(change),'reports/preflight.json')
        try:
            result=self.driver.apply(package,ticket_path)
        except Exception as exc:result={'ok':False,'error':str(exc),'requires_manual_readback':True}
        if not isinstance(result,dict):result={'ok':False,'error':'发布驱动返回了无法解析的结果','requires_manual_readback':True}
        # Unknown outcome wins over a self-reported success: only a human can settle it.
        if result.get('requires_manual_readback'):
            result={**result,'state':self.UNKNOWN}
            state=self.UNKNOWN
        else:state='published' if result.get('ok') is True else 'release_failed'
        with self.tx() as db:
            change=self._load(db,cid);change['state']=state
            change['release_result']=result
            if state!='published':change.pop('preflight',None)
            if state==self.UNKNOWN:
                change['release_unknown']={'reason':'driver_manual_readback','detected_at':time.time(),
                    'note':'发布驱动未给出可确认的结果，须人工核对现场后再恢复'}
            self._event(db,change,'publish_finished','monitor-user',{'request_id':request_id,'ok':result.get('ok') is True,'state':state,'result':result})
            self._save(db,change)
            db.execute('UPDATE publishes SET state=?,result=? WHERE id=?',(state,encoded(result).decode(),request_id))
            version=str(result.get('read_back',{}).get('version') or result.get('version') or ticket.get('version') or change.get('target_version') or 'unknown')
            record={'change_id':cid,'version':version,'base_version':change['base_version'],'base_source':change['base_source'],
                    'module_id':change['module_id'],'files':change['files'],'package':change.get('package'),
                    'actual_release_result':result,'published':state=='published','request_id':request_id}
            atomic(safe(self.workspace,'版本记录/'+cid+'/RELEASE-RECORD.json'),encoded(record)+b'\n')
        return result

    def acknowledge_release_unknown(self,cid,expected_revision,conclusion,evidence,actor='monitor-user'):
        """Record the human read-back only.  Never calls the release driver."""
        if conclusion not in self.RECOVERY:raise ValueError('核对结论只能是 applied（已生效）或 not_applied（已证实没有改动或已退回）')
        if not isinstance(evidence,str) or not evidence.strip():raise ValueError('需要写明核对依据')
        if not isinstance(actor,str) or not actor.strip():raise ValueError('需要核对人标识')
        with self.tx() as db:
            change=self._load(db,cid)
            if change['state']!=self.UNKNOWN:raise Conflict('该候选不处于发布结果未知状态，无需恢复')
            if expected_revision!=change['revision']:raise Conflict('候选版本已变化，请刷新后重试')
            state='published' if conclusion=='applied' else 'release_failed'
            change['state']=state
            change['release_recovery']={'conclusion':conclusion,'evidence':evidence.strip(),'actor':actor,'at':time.time()}
            change.pop('preflight',None);change.pop('confirmation',None)
            if conclusion!='applied':change.pop('release_unknown',None)
            self._event(db,change,'release_unknown_acknowledged',actor,{'conclusion':conclusion,'evidence':evidence.strip(),'state':state})
            self._save(db,change)
            db.execute("UPDATE publishes SET state=? WHERE change_id=? AND state=?",(state,cid,self.UNKNOWN))
            if conclusion=='applied':
                record={'change_id':cid,'version':str(change.get('target_version') or 'unknown'),
                        'base_version':change['base_version'],'base_source':change['base_source'],'module_id':change['module_id'],
                        'files':change['files'],'package':change.get('package'),'actual_release_result':change.get('release_result'),
                        'published':True,'request_id':None,'verified_by':'manual_readback','evidence':evidence.strip()}
                atomic(safe(self.workspace,'版本记录/'+cid+'/RELEASE-RECORD.json'),encoded(record)+b'\n')
        return self.get(cid)

    def get(self,cid):
        with self.tx() as db:
            change=self._load(db,cid);history=[dict(r) for r in db.execute('SELECT * FROM events WHERE change_id=? ORDER BY sequence',(cid,))]
        change.pop('confirmation',None)
        if 'preflight' in change:change['preflight']={k:v for k,v in change['preflight'].items() if k!='source_fingerprint'}
        change['history']=history;change['folder']=str(self._folder(change))
        diffs={}
        for path in change['files']:
            file=safe(self._folder(change),'diffs/'+sha(path.encode())+'.patch')
            diffs[path]=file.read_text() if file.is_file() else ''
        change['diffs']=diffs
        return change

    def state(self):
        with self.tx() as db:ids=[r['id'] for r in db.execute('SELECT id FROM changes ORDER BY rowid DESC')]
        return {'modules':list(self.modules.values()),'changes':[self.get(cid) for cid in ids],
                'driver_available':self.driver is not None,'pending_root':str(self.pending),
                'mode':'iteration_workspace','notice':'网页编辑只写待发布区；正式切换保留原生发布门禁。'}
