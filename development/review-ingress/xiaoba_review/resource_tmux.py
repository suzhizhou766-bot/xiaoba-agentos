"""Attended CLI transport. Only file receipts settle work; never inspect a pane.

The host persists `sending` BEFORE send-keys. Uncertain sends are never replayed.
The existing monitor loop collects pending receipts without an inactivity timeout.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

from .agent_workspace import AgentWorkspace
from .store import Conflict, canonical, digest
from .workday import WorkdayReceipts

EVENT = 'agent_pool.tmux_job'
ACTIVE = {'sending', 'waiting_user_agent'}


def validate_session(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
        raise ValueError('tmux会话名只能包含字母、数字、下划线、短横线，且以字母或数字开头')
    return value


def validate_directory(value):
    if not isinstance(value, str) or any(c in value for c in '\r\n\0'):
        raise ValueError('需要节点工作目录的绝对路径')
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir() or path.resolve() != path:
        raise ValueError('节点工作目录必须是已存在的真实绝对目录，不能经符号链接')
    return str(path)


class TmuxChannel:
    def __init__(self, store, executable=None):
        self.store = store
        # executable is trusted-host/test injection, never a web field.
        self.executable = executable

    def _command(self, *args):
        executable = self.executable or shutil.which('tmux')
        if not executable:
            raise ValueError('本机未安装tmux；未发送任务')
        try:
            result = subprocess.run([executable, *args], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('tmux调用未确认完成；不自动重发，请核对原任务') from None
        if result.returncode:
            raise ValueError('tmux会话不存在或调用失败；不自动重发')

    def jobs(self, db):
        jobs = {}
        for row in db.execute('SELECT payload_json FROM events WHERE event_type=? ORDER BY sequence', (EVENT,)):
            value = json.loads(row[0])
            jobs[value['job_id']] = value
        return jobs

    def _save(self, db, job):
        self.store.native._append_event_tx(db, None, EVENT, job)

    def workspace(self, source):
        node = source['node']
        directory = Path(validate_directory(node['working_directory']))
        # Never adopt a user's existing CLI project as an owned empty workspace.
        # Only the dedicated child is managed by AgentWorkspace.
        return AgentWorkspace(directory, dict(source, node=dict(node, name='.xiaoba-tmux-'+digest(source['reviewer_id'])[:24])))

    def submit(self, source, task_id, step_code, instruction, materials):
        node = source['node']
        if node.get('interface') != 'tmux' or source['kind'] != 'collaborator':
            raise ValueError('tmux上下文不隔离，只能作为协作员')
        session = validate_session(node['tmux_session'])
        payload = {'task_id':task_id, 'step_code':step_code, 'instruction':instruction, 'context':materials}
        fingerprint = digest(payload)
        job_id = digest([source['reviewer_id'], task_id, step_code])
        # Check duplicates and serialize all work for one session or daily log.
        with self.store.transaction() as db:
            from .agent_pool import require_enabled
            current = self.store.source(db, source['reviewer_id'])
            require_enabled(current)
            if current != source:raise Conflict('节点已变化，请刷新后派发')
            jobs = self.jobs(db)
            if job_id in jobs:
                if jobs[job_id]['input_digest'] != fingerprint:
                    raise Conflict('同一步骤已登记不同任务卡')
                return self.public(jobs[job_id])
            if any(j['status'] in ACTIVE and (j['node_id'] == source['reviewer_id'] or j['session'] == session) for j in jobs.values()):
                raise Conflict('该节点或tmux会话已有等待任务；不并发发送')
            self._command('has-session', '-t', '='+session)
            workspace = self.workspace(source)
            record = workspace.begin('work', job_id, payload)
            ticket = WorkdayReceipts(workspace).prepare(record, task_id, step_code, instruction, materials)
            job = {'job_id':job_id, 'node_id':source['reviewer_id'], 'session':session,
                   'source':source, 'input_digest':fingerprint, 'record':record,
                   'ticket':{k:str(v) if isinstance(v,Path) else v for k,v in ticket.items()},
                   'status':'sending', 'created_at':time.time(), 'context_isolated':False,
                   'role':'collaborator', 'automatic_retry':False}
            self._save(db, job)
        try:
            # -l treats all task text literally; Enter is a separate key command.
            self._command('send-keys', '-t', '='+session+':', '-l',
                          '请读取并执行这张任务卡：'+json.dumps(str(ticket['card']), ensure_ascii=False)+'；按卡内格式写回执。')
            self._command('send-keys', '-t', '='+session+':', 'Enter')
        except ValueError:
            with self.store.transaction() as db:
                job.update(status='send_uncertain', finished_at=time.time())
                self._save(db, job)
            raise
        with self.store.transaction() as db:
            current = self.jobs(db)[job_id]
            # A cancellation racing with send must remain cancelled.
            if current['status'] == 'sending':
                job.update(status='waiting_user_agent', sent_at=time.time())
                self._save(db, job)
            else:job = current
        return self.public(job)

    def public(self, job):
        return {k:v for k,v in job.items() if k not in {'source','ticket'}} | {
            'task_card':job['ticket']['card'], 'receipt_file':job['ticket']['log'],
            'message':'等待用户/Agent' if job['status']=='waiting_user_agent' else job['status']}

    def collect(self):
        # Runs every 2 s: without a waiting job, take neither the write lock nor
        # the chain verification a write transaction performs.
        with self.store.native.read_only() as db:
            if not any(j['status'] == 'waiting_user_agent' for j in self.jobs(db).values()):return
        # BEGIN IMMEDIATE also serializes concurrent collectors/cancellation.
        with self.store.transaction() as db:
            for job in self.jobs(db).values():
                if job['status'] != 'waiting_user_agent':continue
                try:
                    workspace = self.workspace(job['source'])
                    workspace.verify()
                    ticket = dict(job['ticket'])
                    for k in ('log','card'):ticket[k] = Path(ticket[k])
                    receipt = WorkdayReceipts(workspace).read(ticket)
                    if receipt is None:continue
                    evidence = {'model':job['source']['node']['model'],
                        'model_source':'node_configuration', 'model_verification':'not_performed',
                        'context_isolated':False, 'role':'collaborator', 'worklog':receipt['worklog']}
                    path = workspace.finish(job['record'], 'execution_completed', result=receipt, evidence=evidence)
                    job.pop('receipt_error', None)
                    job.update(status=receipt['status'], result=receipt, execution_evidence=evidence,
                               execution_receipt=path, finished_at=time.time())
                except (ValueError, OSError) as exc:
                    # An unacceptable receipt can still be corrected in the file:
                    # keep waiting, show why, and record each reason only once.
                    reason = str(exc) or type(exc).__name__
                    if job.get('receipt_error') == reason:continue
                    job['receipt_error'] = reason
                self._save(db, job)

    def cancel(self, node_id, job_id):
        with self.store.transaction() as db:
            job = self.jobs(db).get(job_id)
            if not job or job['node_id'] != node_id:raise ValueError('等待任务不存在')
            if job['status'] not in ACTIVE:return self.public(job)
            job.update(status='cancelled', finished_at=time.time())
            self._save(db, job)
        return self.public(job)
