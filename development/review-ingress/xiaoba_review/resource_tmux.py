"""Attended CLI transport. Only file receipts settle work; pane text is never read.

The host persists `sending` BEFORE send-keys. Uncertain sends are never replayed.
The existing monitor loop collects pending receipts without an inactivity timeout.
Before typing anything the channel reads pane metadata (`display-message -p`),
which reports the foreground program and its directory, never screen content.
"""
import json
import os
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

# A shell in the foreground means the card text would be typed at a prompt and
# run as commands (卡 26). `login` is how tmux names a login shell's wrapper.
SHELLS = frozenset({'zsh', 'bash', 'sh', 'fish', 'dash', 'tcsh', 'ksh', 'login'})

_EXECUTABLE = None


def validate_executable(value):
    if not isinstance(value, str) or any(c in value for c in '\r\n\0'):
        raise ValueError('需要tmux可执行文件路径')
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError('tmux路径必须是已存在的可执行文件：' + str(path))
    return str(path)


def configure_executable(value):
    """Point the attended channel at a tmux the service PATH cannot see.

    Trusted host input (monitor config `agent_pool.tmux_path` or `run.py
    --tmux-path`), never a web field. Empty clears it and PATH is used again.
    """
    global _EXECUTABLE
    _EXECUTABLE = validate_executable(value) if value else None
    return _EXECUTABLE


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

    def _run(self, args, capture):
        executable = self.executable or _EXECUTABLE or shutil.which('tmux')
        if not executable:
            raise ValueError('本机未安装tmux；未发送任务')
        try:
            return subprocess.run([executable, *args], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, text=capture, timeout=5, check=False,
                # Without a UTF-8 locale (launchd) tmux prints tabs and non-ASCII
                # as "_", and a pane directory could never match its node.
                env=dict(os.environ, LC_ALL='en_US.UTF-8'))
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('tmux调用未确认完成；不自动重发，请核对原任务') from None

    def _command(self, *args):
        if self._run(args, False).returncode:
            raise ValueError('tmux会话不存在或调用失败；不自动重发')

    def _pane(self, session):
        """Foreground program and directory of the session's active pane.

        `display-message -p` expands a format string; it prints no pane text, so
        the channel still never reads the screen.
        """
        result = self._run(('display-message', '-p', '-t', '=' + session + ':',
                            '#{pane_current_command}\t#{pane_current_path}'), True)
        if result.returncode:
            raise ValueError('tmux窗格元数据读取失败；未发送任务，不自动重发')
        command, separator, path = (result.stdout or '').strip().partition('\t')
        if not separator or not command or not path:
            raise ValueError('tmux窗格元数据不完整；未发送任务，不自动重发')
        return command.strip(), path.strip()

    def _require_agent_foreground(self, session, node):
        """Refuse to type a card at a shell prompt or in an unregistered directory.

        卡 26 sent a task to a pane whose foreground was zsh, so the card text was
        executed as commands. Only metadata is checked; nothing is read or retyped.
        """
        command, pane_path = self._pane(session)
        if Path(command.lstrip('-')).name.lower() in SHELLS:
            raise ValueError('tmux前台是shell（' + command + '），任务文字会被当成命令执行；'
                             '未发送，请把该会话切到节点登记的Agent后再派发')
        registered = Path(node['working_directory']).resolve()
        if Path(pane_path).resolve() != registered:
            raise ValueError('tmux窗格工作目录（' + pane_path + '）与节点登记目录（' + str(registered)
                             + '）不同；未发送')

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
            # Before any owned file is created and before `sending` is persisted:
            # a refusal here must never leave a "已发送" record behind.
            self._require_agent_foreground(session, node)
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
                        'model_source':'node_configuration',
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

    def cancel_waiting(self, node_id):
        """Cancel this node's in-person waits. No-op when it has none."""
        with self.store.native.read_only() as db:
            waiting = [job['job_id'] for job in self.jobs(db).values()
                       if job['node_id'] == node_id and job['status'] in ACTIVE]
        for job_id in waiting:
            self.cancel(node_id, job_id)

    def cancel(self, node_id, job_id):
        with self.store.transaction() as db:
            job = self.jobs(db).get(job_id)
            if not job or job['node_id'] != node_id:raise ValueError('等待任务不存在')
            if job['status'] not in ACTIVE:return self.public(job)
            job.update(status='cancelled', finished_at=time.time())
            self._save(db, job)
        return self.public(job)
