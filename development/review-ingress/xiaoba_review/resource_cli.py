"""One CLI process per card, with shared workspace, worklog and process ownership.

Raw stdout/stderr only supplies bounded diagnostic classification and event
counts. A validated task-card file receipt is the only business result.
"""
import json
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time
import uuid

from .agent_workspace import AgentWorkspace, WorkspaceError
from .resource_agy import (
    InteractiveError, NativeWorkspaceConsent, ReviewReceiptError,
    REVIEW_RUN_INSTRUCTION, parse_review_receipt
)
from .terminal_preview import TerminalPreview
from .workday import WorkdayReceipts


ANTI_DRIFT = '只执行这张卡；不要检索旧任务记录、其它日期的工作日志或小八源码。'
HEALTH_DISPATCH = ('这是文件写回探针，请读取这张卡并直接写入它指定的回执文件。'
    '不要自行做系统健康诊断，不要新建当日台账。')
WORK_DISPATCH = '读取并执行这张任务卡，在卡内指定的当日Markdown工作文件中追加过程和完整回执。'
STDERR_LIMIT = 64 * 1024
PREVIEW_LINES = 40
PREVIEW_BYTES = 64 * 1024


class CliWorklogResource:
    """Shared per-step execution; subclasses only supply the CLI dialect."""

    supports_resource_plan = True
    persistent_resource = True
    supports_workday_receipts = True
    execution_mode = 'native_cli_per_step'
    test_budget_seconds = 120
    # Production defaults.  Tests may lower them on an instance; the shipped
    # values stay 300 s of no progress and a 30 s receipt grace window.
    no_progress_seconds = 300
    receipt_grace_seconds = 30
    output_limit = 1024 * 1024

    def __init__(self, source, workspace_root=None):
        self.kind, self.source_ref = source['kind'], source['source_ref']
        self.entry, self.model = source['node']['entry'], source['node']['model']
        self._serial = threading.Lock()
        self._process = None
        self._workspace_lock = None
        self.workspace = AgentWorkspace(workspace_root, source)
        self.workday = WorkdayReceipts(self.workspace)
        self._preview = TerminalPreview()
        self._state = 'not_started'
        self._retired = False
        self._turns = 0
        self._call_id = None
        self._queue = None
        self._stdout = bytearray()
        self._partial = b''
        self._events = 0
        self._stderr = None
        self._stderr_kept = 0
        self._eof = False
        # Never implement confirm_workspace: a native trust prompt blocks.
        self.pending_action = None

    # -- identity and read-only projection -----------------------------------

    agent_type = 'agy'
    label = 'AGY'
    provider = 'antigravity_cli'

    def resource_identity(self):
        from .cli_identity import cli_identity
        return cli_identity(self.entry, self.workspace, self.agent_type)

    def terminal_snapshot(self):
        return {**self._preview.snapshot(), 'supported': True, 'state': self._state,
                'session_id': self._call_id, 'turn': self._turns}

    # -- lifecycle -----------------------------------------------------------

    def retire(self):
        # Keep an already running receipt, per AgentPool's existing contract.
        self._retired = True
        if self._serial.acquire(blocking=False):
            try: self.close()
            finally: self._serial.release()

    def close(self):
        self._release()
        self._state = 'closed'

    def _release(self):
        """End this call's process group and release the workspace lock."""
        process = self._process
        try: self._kill_group()
        except PermissionError:
            self._retired = True
            raise InteractiveError('session_cleanup_blocked',
                f'系统拒绝结束本次{self.label}进程，请人工核对；本节点停止派活，未绕过系统限制。') from None
        finally:
            self._process = None
            for stream in (getattr(process, 'stdin', None), getattr(process, 'stdout', None), getattr(process, 'stderr', None)):
                try:
                    if stream is not None: stream.close()
                except OSError: pass
            if self._stderr is not None:
                try: self._stderr.close()
                except OSError: pass
                self._stderr = None
            if self._workspace_lock is not None:
                self._workspace_lock.close()
                self._workspace_lock = None

    # -- one CLI invocation --------------------------------------------------

    def _environment(self):
        from .cli_identity import cli_environment
        return cli_environment(self.entry, self.agent_type)

    def _command(self, entry, timeout):
        raise NotImplementedError

    def _input(self, ticket, health_only):
        return (self._dispatch(ticket, health_only) + '\n').encode()

    def _dispatch(self, ticket, health_only):
        # The task itself stays in the card file; argv carries no task text.
        return ((HEALTH_DISPATCH if health_only else WORK_DISPATCH) + ANTI_DRIFT +
                '只执行卡内授权范围；保留先前记录；不用在终端复述结果。\n' +
                'XIAOBA_TASK_CARD=' + json.dumps(str(ticket['card']), ensure_ascii=False))

    def _start(self, directory, ticket, timeout, health_only):
        self.resource_identity()
        argv = self._command(self.entry, timeout)
        self._preview = TerminalPreview()
        self._stdout = bytearray()
        self._partial = b''
        self._events = 0
        self._eof = False
        self._stderr = tempfile.TemporaryFile()
        self._stderr_kept = 0
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=str(directory), env=self._environment(),
            start_new_session=True, close_fds=True)
        self._process = process
        try:
            process.stdin.write(self._input(ticket, health_only))
            process.stdin.flush()
        finally:
            try: process.stdin.close()
            except OSError: pass
        self._queue = queue.Queue()
        threading.Thread(target=self._pump, args=(process.stdout, self._queue.put),
                         daemon=True).start()
        self._stderr_thread = threading.Thread(target=self._pump_stderr, args=(process.stderr,), daemon=True)
        self._stderr_thread.start()
        self._refresh_preview(ticket, self._log_size(ticket))

    @staticmethod
    def _pump(stream, sink):
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk: break
                sink(chunk)
        except (OSError, ValueError): pass
        finally: sink(None)

    def _pump_stderr(self, stream):
        # Bounded temp file: the raw text is scanned once and never returned.
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk: break
                if self._stderr_kept >= STDERR_LIMIT: continue
                data = chunk[:STDERR_LIMIT - self._stderr_kept]
                self._stderr_kept += len(data)
                self._stderr.write(data)
        except (OSError, ValueError): pass

    def _stderr_text(self):
        if self._stderr is None: return ''
        try:
            self._stderr.flush()
            self._stderr.seek(0)
            return self._stderr.read(STDERR_LIMIT).decode('utf-8', errors='replace')
        except (OSError, ValueError): return ''

    def _log_size(self, ticket):
        try: return ticket['log'].stat().st_size
        except OSError: return 0

    def _refresh_preview(self, ticket, size):
        lines = []
        try:
            with ticket['log'].open('rb') as stream:
                start = max(ticket['prefix_size'], size - PREVIEW_BYTES)
                stream.seek(start)
                lines = stream.read(PREVIEW_BYTES).decode('utf-8', errors='replace').splitlines()
        except (OSError, ValueError): lines = []
        head = '第%d次调用 · 已收到%d条流事件 · 回执%d字节' % (self._turns + 1, self._events, size)
        self._preview.set_screen('\n'.join([head, *lines[-PREVIEW_LINES:]]))

    def _consume(self, chunk):
        """Count complete, parseable JSON lines as verifiable progress."""
        self._partial += chunk
        found = 0
        while b'\n' in self._partial:
            line, self._partial = self._partial.split(b'\n', 1)
            if not line.strip(): continue
            try:
                if isinstance(json.loads(line.decode('utf-8', 'replace')), dict): found += 1
            except (ValueError, UnicodeError): continue
        return found

    def _drain(self):
        """Pick up whatever the child already wrote before we settle."""
        while True:
            try: chunk = self._queue.get_nowait()
            except queue.Empty: return
            if chunk is None: self._eof = True; return
            self._events += self._consume(chunk)

    def _wait(self, ticket, timeout):
        process = self._process
        deadline = None if timeout is None else time.monotonic() + timeout
        last_progress = time.monotonic()
        size = self._log_size(ticket)
        received = 0
        exited_at = None
        while True:
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                self._kill_group()
                raise InteractiveError('cli_timeout', f'{self.label}调用未在本次测试期限内完成；不会自动重测。')
            if now - last_progress >= self.no_progress_seconds:
                self._kill_group()
                raise InteractiveError('cli_no_progress',
                    f'{self.label}调用连续%d秒无可验证结果进展，已停止本次调用。' % self.no_progress_seconds)
            try: chunk = self._queue.get(timeout=0.05)
            except queue.Empty: chunk = b''
            if chunk is None: self._eof = True
            elif chunk:
                received += len(chunk)
                if received > self.output_limit:
                    self._kill_group()
                    raise InteractiveError('cli_output_limit',
                        f'{self.label}输出超过安全上限，已停止；不保存原始输出。')
                self._stdout.extend(chunk)
                found = self._consume(chunk)
                if found:
                    self._events += found
            # stdout never supplies a business result; only the file does.
            receipt = self.workday.read(ticket)
            if receipt is not None:
                self._drain()
                self._refresh_preview(ticket, self._log_size(ticket))
                return self._settle(receipt, process)
            current = self._log_size(ticket)
            if current != size:
                size = current; last_progress = time.monotonic()
                self._refresh_preview(ticket, current)
            if process.poll() is not None:
                # A helper agy left behind may hold stdout open; once the leader
                # has exited, wait at most 2 s for the stream to close.
                exited_at = exited_at or time.monotonic()
                if self._eof or time.monotonic() - exited_at >= 2: break
        # The last write may land between the read above and the exit check.
        receipt = self.workday.read(ticket)
        if receipt is not None:
            self._drain()
            self._refresh_preview(ticket, self._log_size(ticket))
            return receipt, False
        return self._settle_exit()

    def _settle(self, receipt, process):
        if process.poll() is not None: return receipt, False
        grace = time.monotonic() + self.receipt_grace_seconds
        while time.monotonic() < grace:
            if process.poll() is not None: return receipt, False
            time.sleep(0.02)
        self._kill_group()
        return receipt, True

    def _settle_exit(self):
        self._drain()
        self._stderr_thread.join(timeout=1)
        returncode = self._process.returncode if self._process is not None else None
        stdout, stderr = bytes(self._stdout).decode('utf-8', 'replace'), self._stderr_text()
        from .cli_identity import cli_failure
        raise cli_failure(self.agent_type, self.label, self.model, returncode, stdout, stderr)

    def _kill_group(self):
        """End the call's whole process group, including helpers agy left behind.

        Each step starts a fresh group, so an exited agy leader does not mean the
        step is over: anything still in the group would otherwise pile up step
        after step.  SIGTERM first, SIGKILL for whatever remains after 2 s.
        """
        process = self._process
        if process is None: return
        # macOS answers a group holding only unreaped zombies with EPERM.  Reap
        # our own leader first; after that EPERM means nothing is left alive,
        # and only a live leader that still refuses is a blocked cleanup.
        def signal_group(number):
            try: os.killpg(process.pid, number)
            except ProcessLookupError: return False
            except PermissionError:
                # A leader caught mid-exit answers EPERM before poll() can see it.
                try: process.wait(timeout=1)
                except subprocess.TimeoutExpired: raise PermissionError from None
                return False
            return True
        process.poll()
        if not signal_group(signal.SIGTERM): return
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try: process.wait(timeout=0.05)
            except subprocess.TimeoutExpired: pass
            if not signal_group(0): return
        signal_group(signal.SIGKILL)
        try: process.wait(timeout=2)
        except subprocess.TimeoutExpired: pass

    # -- evidence and the shared turn shape ----------------------------------

    def _evidence(self, call_id=None):
        return {'provider': self.provider, 'model': self.model,
                'route_source': 'native_cli_per_step', 'model_source': 'node_configuration' if self.model else 'cli_default',
                'call_id': call_id or self._call_id,
                'execution_mode': self.execution_mode}

    def _failure_evidence(self, exc, call_id):
        evidence = self._evidence(call_id)
        failure_class = getattr(exc, 'failure_class', None)
        if failure_class: evidence['failure_class'] = failure_class
        return evidence

    def _turn(self, instruction, context, timeout=None, kind='review', record_id=None,
              step_code=None, task_id=None, health_only=False):
        with self._serial:
            if self._retired:
                raise InteractiveError('session_closed', f'{self.label}节点已停用，本会话不再接收任务。')
            task_id = task_id or record_id or uuid.uuid4().hex
            step_code = step_code or ('HEALTH-01' if kind == 'test' else 'REVIEW-01')
            materials = json.loads(context)
            payload = {'task_id': task_id, 'step_code': step_code, 'model': self.model,
                       'instruction': instruction, 'context': materials}
            if kind == 'work':
                prior = self.workspace.previous_work(record_id or task_id, payload)
                if prior:
                    value, evidence = prior
                    if value is None:
                        raise InteractiveError('work_step_failed', '原任务步骤已失败并留有记录，不自动重跑。')
                    return value, dict(evidence, replayed=True)
            self.resource_identity()
            record = None
            call_id = uuid.uuid4().hex
            try:
                if self.pending_action: raise NativeWorkspaceConsent()
                self._workspace_lock = self.workspace.lock_session()
                directory = self.workspace.ensure()
                self.workspace.verify()
                record = self.workspace.begin(kind, record_id or task_id, payload)
                ticket = self.workday.prepare(record, task_id, step_code, instruction, materials,
                    review=kind == 'review', health_only=health_only)
                self._call_id = call_id
                self._state = 'running'
                self._start(directory, ticket, timeout, health_only)
                value, terminated = self._wait(ticket, timeout)
                self._turns += 1
                evidence = dict(self._evidence(call_id), reply_source='daily_worklog',
                                worklog=value['worklog'])
                if terminated: evidence['process_terminated_after_receipt'] = True
                self.workspace.finish(record,
                    'execution_completed' if value['status'] == 'completed' else value['status'],
                    value, evidence)
                self._release()
                self._state = 'receipt_received'
                return value, evidence
            except NativeWorkspaceConsent:
                self.close()
                self._state = 'failed'
                raise
            except BaseException as exc:
                try:
                    if record and (not isinstance(exc, WorkspaceError) or exc.code in {
                            'worklog_receipt_invalid', 'worklog_task_mismatch', 'worklog_non_answer',
                            'worklog_path_invalid', 'worklog_limit', 'worklog_receipt_conflict'}):
                        self.workspace.finish(record, 'failed',
                            evidence=self._failure_evidence(exc, call_id),
                            reason=getattr(exc, 'code', type(exc).__name__))
                finally:
                    self.close()
                    self._state = 'failed'
                raise
            finally:
                if self._retired: self.close()

    # -- the interface the callers already use -------------------------------

    def test_resource(self, plan, challenge):
        value, evidence = self._turn('本任务只验证文件写回通道，不要求你执行系统健康检查或诊断。'
            '主入口负责根据写回结果判断健康，你只需用文件读写工具将给定内容写入本卡指定的回执文件。'
            '步骤码只是关联编号，不需要查找其实现，不查历史任务或小八源码。'
            '既有治理要求保持；不为本卡运行命令、不联网、不做额外诊断，只写指定回执文件，不新建当日台账。'
            '回执正文只写材料中的challenge原文，不要JSON或代码围栏。',
            json.dumps({'challenge': challenge}, ensure_ascii=False), timeout=90, kind='test',
            record_id=challenge, health_only=True)
        if value['status'] != 'completed' or value['body'] != challenge:
            self.close()
            raise InteractiveError('challenge_mismatch', '文件回执未完成本次健康挑战；没有标记可用。')
        return dict(evidence, output=value['body'])

    def initialize_workday(self, health_evidence, task_id):
        value, evidence = self._turn('主入口已核验前一步健康检测通过。由你新建或沿用当日工作Markdown，'
            '只追加本次开工记录，保留原记录。回执正文只写材料中的challenge原文，不用JSON。',
            json.dumps({'challenge': 'WORKDAY_READY', 'health_check': health_evidence},
                       ensure_ascii=False), timeout=90, kind='test', record_id=task_id,
            step_code='WORKDAY-INIT')
        if value['status'] != 'completed' or value['body'] != 'WORKDAY_READY':
            self.close()
            raise InteractiveError('workday_initialization_failed',
                '健康检测通过，但当日工作台账初始化未完成。')
        return evidence

    def run(self, card):
        value, evidence = self._turn(REVIEW_RUN_INSTRUCTION,
            json.dumps(card, ensure_ascii=False), record_id=card.get('assignment_id'),
            task_id=card.get('task_id') or card.get('review_request_id'),
            step_code=card.get('step_code'))
        return parse_review_receipt(value, card, evidence)

    def run_task(self, task_id, step_code, instruction, materials):
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError('需要明确授权的工作内容和范围')
        value, evidence = self._turn(instruction, json.dumps(materials, ensure_ascii=False),
            kind='work', task_id=task_id, step_code=step_code, record_id=task_id + '-' + step_code)
        return {'task_id': task_id, 'step_code': step_code, 'status': value['status'],
                'body': value['body'], 'execution_evidence': evidence, 'score_assigned': False}
