"""Owned per-node workspaces, task receipts and a verifiable local journal."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from datetime import datetime
from zoneinfo import ZoneInfo


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def checksum(data):return hashlib.sha256(data).hexdigest()


class WorkspaceError(ValueError):
    def __init__(self,code,message):
        super().__init__(message);self.code,self.user_message=code,message


class AgentWorkspace:
    def __init__(self,root,source):
        self.root=Path(root) if root else None
        self.node_id,self.source_ref=source['reviewer_id'],source['source_ref']
        self.name=source['node']['name']
        self.owner={'schema':'xiaoba-agent-workspace-v1','node_id':self.node_id,
                    'source_ref':self.source_ref,'name':self.name}

    @property
    def path(self):
        name=self.name
        if (not name or name in {'.','..'} or name!=name.strip()
                or any(c in name for c in '/\\') or any(ord(c)<32 for c in name)):
            raise WorkspaceError('workspace_name_invalid','节点名称不能作为安全目录名；请使用不含斜线和控制字符的名称。')
        if self.root is None:
            raise WorkspaceError('workspace_root_required','本机宿主尚未指定Agent工作区根目录；没有启动或发送任务。')
        return self.root.resolve()/name

    def _read(self,path):
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise WorkspaceError('workspace_conflict','工作区记录不是普通文件，请人工核对。')
            return stream.read()

    def _new(self,path,data):
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())

    def ensure(self):
        path=self.path
        if not path.parent.is_dir():
            raise WorkspaceError('workspace_root_required','指定的Agent工作区根目录不存在，请核对宿主入口。')
        if path.is_symlink():raise WorkspaceError('workspace_conflict','同名工作区是符号链接，不能接管。')
        path.mkdir(mode=0o700,exist_ok=True)
        if not path.is_dir():raise WorkspaceError('workspace_conflict','同名对象不是目录，不能覆盖。')
        marker=path/'.xiaoba-agent-workspace.json'
        if marker.exists() or marker.is_symlink():
            if json.loads(self._read(marker))!=self.owner:
                raise WorkspaceError('workspace_conflict','同名目录归属不同节点，不能混用或覆盖；请由用户决定。')
        else:
            if any(path.iterdir()):
                raise WorkspaceError('workspace_conflict','同名目录已有未登记材料，不能自动接管；请由用户决定。')
            try:self._new(marker,(canonical(self.owner)+'\n').encode())
            except FileExistsError:
                if json.loads(self._read(marker))!=self.owner:raise WorkspaceError('workspace_conflict','工作区归属冲突。')
        records=path/'任务记录'
        if records.is_symlink():raise WorkspaceError('workspace_conflict','任务记录目录不能是符号链接。')
        records.mkdir(mode=0o700,exist_ok=True)
        readme=path/'工作区说明.md'
        if not readme.exists():
            text=('# '+self.name+' 工作区\n\n由小八AgentOS按节点身份管理。首次原生目录信任由用户确认。\n'
                  '任务记录包含任务卡、合格结果和执行回执；记录.jsonl为可校验索引。\n'
                  '不保存登录材料、账号横幅或原始终端转录；记录不代表用户交付验收或经验批准。\n'
                  '关闭、重启和可恢复移除节点均保留材料，不自动删除或另建备份。\n')
            try:self._new(readme,text.encode())
            except FileExistsError:pass
        return path

    def lock_session(self):
        path=self.ensure()/'.session.lock'
        stream=os.fdopen(os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600),'a+b')
        try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close();raise WorkspaceError('workspace_session_busy','此节点工作区已有交互会话，不能并行另开；未发送任务。') from None
        return stream

    def _append(self,payload):
        root=self.ensure();journal=root/'记录.jsonl';lock=root/'.records.lock'
        with os.fdopen(os.open(lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600),'a+b') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            previous,sequence=self._verified_rows(root)
            row={'sequence':sequence+1,'previous':previous,'time':time.time(),**payload}
            row['sha256']=checksum(canonical(row).encode())
            fd=os.open(journal,os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'ab') as target:
                target.write((canonical(row)+'\n').encode());target.flush();os.fsync(target.fileno())

    def _verified_rows(self, root):
        if root.is_symlink() or (root/'任务记录').is_symlink():
            raise WorkspaceError('workspace_journal_invalid','工作区或任务记录目录归属异常，停止派活。')
        journal=root/'记录.jsonl';previous='0'*64;sequence=0
        if journal.exists() or journal.is_symlink():
            for line in self._read(journal).splitlines():
                try:
                    row=json.loads(line);expected=row.pop('sha256',None);record=row['record']
                    if not re.fullmatch(r'(?:test|review|work)-[a-f0-9]{24}',record):raise ValueError('record')
                    if (row.get('sequence')!=sequence+1 or row.get('previous')!=previous
                            or checksum(canonical(row).encode())!=expected):raise ValueError('chain')
                    directory=root/'任务记录'/record
                    if directory.is_symlink():raise ValueError('directory')
                    if row['event']=='task_recorded':
                        if checksum(self._read(directory/'任务卡.json'))!=row['input_sha256']:raise ValueError('input')
                    elif row['event']=='task_finished':
                        receipt=row['receipt']
                        if not re.fullmatch(r'执行回执-[a-f0-9]{24}\.json',receipt):raise ValueError('receipt')
                        if checksum(self._read(directory/receipt))!=row['receipt_sha256']:raise ValueError('output')
                        value=json.loads(self._read(directory/receipt))
                        worklog=(value.get('execution_evidence') or {}).get('worklog')
                        if worklog:
                            from .workday import verify_worklog
                            verify_worklog(root,worklog)
                    else:raise ValueError('event')
                except (ValueError,KeyError,TypeError,OSError):
                    raise WorkspaceError('workspace_journal_invalid','Agent工作区记录链或文件摘要校验失败；停止本节点派活，保留原证据。') from None
                previous=expected;sequence+=1
        return previous,sequence

    def verify(self):return self._verified_rows(self.ensure())

    def workday_inputs(self, day):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):raise ValueError('工作日格式异常')
        root=self.path
        if not root.exists():return []
        self._verified_rows(root)
        journal=root/'记录.jsonl'
        if not journal.exists():return []
        starts={};items=[];seen=set()
        for line in self._read(journal).splitlines():
            row=json.loads(line);record=row['record']
            if row['event']=='task_recorded':starts[record]=row['time'];continue
            receipt=json.loads(self._read(root/'任务记录'/record/row['receipt']))
            evidence=receipt.get('execution_evidence') or {};worklog=evidence.get('worklog')
            if row['receipt_sha256'] in seen:continue
            if not worklog:
                host_day=datetime.fromtimestamp(row['time'],ZoneInfo('Asia/Shanghai')).date().isoformat()
                source=json.loads(self._read(root/'任务记录'/record/'任务卡.json'))
                if receipt.get('status')!='failed' or host_day!=day or not source.get('task_id') or not evidence.get('model'):continue
                worklog={'task_id':source['task_id'],'step_code':source['step_code'],'date':day}
            if worklog['date']!=day:continue
            seen.add(row['receipt_sha256'])
            result=receipt.get('result') or {}
            is_review = record.startswith('review-')
            review_qualified = None
            if is_review:
                meta = result.get('metadata') or {}
                verdict = meta.get('审查结论') or meta.get('verdict')
                has_verdict = verdict in {'PASS', 'FAIL', 'NEEDS_INFO'}
                refs = result.get('evidence_refs') or []
                has_refs = bool(refs) and all(r is not None and (not isinstance(r, str) or bool(r.strip())) for r in refs)
                status_ok = (result.get('status') or receipt.get('status')) in {'completed', 'execution_completed'}
                no_failure = not receipt.get('reason') and not receipt.get('failure_reason')

                card_file = root / '任务记录' / record / '任务卡.json'
                card_data = {}
                if card_file.exists():
                    try:
                        raw_card = json.loads(self._read(card_file))
                        card_data = raw_card.get('context') if isinstance(raw_card.get('context'), dict) else raw_card
                    except Exception:
                        pass
                contract = card_data.get('receipt_contract') or {}
                is_v2 = contract.get('version', 1) >= 2
                v2_ok = True
                if is_v2:
                    rtype = meta.get('审查类型') or meta.get('review_type')
                    score_raw = meta.get('评分') or meta.get('score')
                    app = meta.get('适用条件') or meta.get('适用条件与限制') or meta.get('applicability')
                    score_ok = False
                    if score_raw is not None:
                        try:
                            s = float(score_raw)
                            if math.isfinite(s) and 0 <= s <= 10:
                                score_ok = True
                        except (ValueError, TypeError):
                            pass
                    rtype_ok = rtype in {'new_experience', 'revalidation', 'reuse_change'}
                    app_ok = isinstance(app, str) and bool(app.strip())
                    card_evidence = card_data.get('evidence_refs', [])
                    def match_ref(r):
                        if isinstance(r, str):
                            return bool(r.strip()) and any(c.get('ref') == r for c in card_evidence if isinstance(c, dict))
                        if isinstance(r, dict):
                            ref_val = r.get('ref')
                            sha_val = r.get('sha256')
                            return (bool(ref_val and isinstance(ref_val, str) and ref_val.strip()) and
                                    bool(sha_val and isinstance(sha_val, str) and sha_val.strip()) and
                                    any(c.get('ref') == ref_val and c.get('sha256') == sha_val for c in card_evidence if isinstance(c, dict)))
                        return False
                    refs_ok = bool(refs) and all(match_ref(r) for r in refs)
                    v2_ok = rtype_ok and score_ok and app_ok and refs_ok

                review_qualified = bool(status_ok and no_failure and has_verdict and has_refs and v2_ok)

            items.append({'task_id':worklog['task_id'],'step_code':worklog['step_code'],
                'record':record,'receipt_sha256':row['receipt_sha256'],'worklog':worklog,
                'node_id':self.node_id,'source_ref':self.source_ref,'model':evidence.get('model'),
                'status':result.get('status') or receipt.get('status'),'body':result.get('body'),
                'failure_reason':receipt.get('reason'),
                'evidence_refs':result.get('evidence_refs',[]),'completed_at':row['time'],
                'elapsed_seconds':max(0,row['time']-starts.get(record,row['time'])),
                'health_check':record.startswith('test-'),
                'is_review':is_review,'review_qualified':review_qualified,
                'self_report_is_independently_verified':False,
                'user_delivery_accepted':receipt.get('user_delivery_accepted',False),
                'experience_approved':False})
        return items

    def begin(self,kind,record_id,payload):
        if kind not in {'test','review','work'}:raise ValueError('未知任务记录类型')
        from xiaoba_agentos.memory import MemoryBridge
        body=canonical(payload).encode()
        if MemoryBridge._sensitive_reason(body.decode()):
            raise WorkspaceError('workspace_sensitive_input','任务材料包含敏感资料，未归档或发送。')
        root=self.ensure();self.verify();name=kind+'-'+checksum(str(record_id).encode())[:24]
        directory=root/'任务记录'/name
        if directory.is_symlink():raise WorkspaceError('workspace_conflict','任务记录目录归属异常。')
        directory.mkdir(mode=0o700,exist_ok=True)
        card=directory/'任务卡.json'
        if card.exists():
            if self._read(card)!=body:raise WorkspaceError('workspace_record_conflict','同一请求ID已有不同材料，不能覆盖。')
        else:self._new(card,body)
        self._append({'event':'task_recorded','record':name,'input_sha256':checksum(body),'experience_approval':False})
        return name

    def previous_work(self,record_id,payload):
        root=self.path
        if not root.exists():return None
        self.ensure()
        self._verified_rows(root)
        record='work-'+checksum(str(record_id).encode())[:24]
        directory=root/'任务记录'/record;card=directory/'任务卡.json'
        if directory.is_symlink() or card.is_symlink():raise WorkspaceError('workspace_record_conflict','任务步骤记录不能指向符号链接。')
        if not card.exists():return None
        if self._read(card)!=canonical(payload).encode():
            raise WorkspaceError('workspace_record_conflict','同一任务步骤已有不同任务卡，不能重放为新工作。')
        for line in reversed(self._read(root/'记录.jsonl').splitlines()):
            row=json.loads(line)
            if row['record']==record and row['event']=='task_finished':
                value=json.loads(self._read(directory/row['receipt']))
                return value.get('result'),value.get('execution_evidence')
        raise WorkspaceError('work_step_unsettled','本任务步骤已有未结算派工；不自动重复执行，请主入口核对原记录。')

    def finish(self,record,status,result=None,evidence=None,reason=None):
        if not re.fullmatch(r'(?:test|review|work)-[a-f0-9]{24}',record):raise ValueError('任务记录ID异常')
        from xiaoba_agentos.memory import MemoryBridge
        value={'status':status,'reason':reason,'execution_evidence':evidence,'result':result,
               'user_delivery_accepted':False,'experience_approved':False}
        body=canonical(value).encode()
        if MemoryBridge._sensitive_reason(body.decode()):
            raise WorkspaceError('workspace_sensitive_output','结果含敏感资料，未归档或交付。')
        self.verify();directory=self.path/'任务记录'/record
        if directory.is_symlink():raise WorkspaceError('workspace_conflict','任务记录目录归属异常。')
        receipt=directory/('执行回执-'+checksum(body)[:24]+'.json')
        if receipt.exists() or receipt.is_symlink():
            if self._read(receipt)!=body:raise WorkspaceError('workspace_record_conflict','执行回执已变化，不能覆盖。')
        else:self._new(receipt,body)
        self._append({'event':'task_finished','record':record,'status':status,'receipt':receipt.name,
                      'receipt_sha256':checksum(body),'experience_approval':False})
        return str(receipt)
