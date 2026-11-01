"""One owned interactive AGY terminal per node; never use print mode or retry.

The node's model is a user configuration value, not upstream attestation.
Terminal transcripts stay in memory and are never stored in the knowledge vault.
"""
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from types import SimpleNamespace
import uuid

from xiaoba_agentos.adapters import _sanitize_answer
from xiaoba_agentos.gateway import command_environment
from xiaoba_agentos.output_hygiene import contains_non_answer_markers, detect_reasoning_leak
from .agent_workspace import AgentWorkspace, WorkspaceError
from .terminal_preview import TerminalPreview
from .terminal_screen import TerminalScreen
from .workday import WorkdayReceipts


ANSI = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Z0-9]|\x1b[=>]')
# Only observed empty input prompts qualify; permission choices never do.
READY = re.compile(r'(?m)^[ \t]*[>❯][ \t]*(?:Plan mode: research & plan only \(shift\+tab to cycle\)|Accept-edits mode: file edits auto-approved \(shift\+tab to cycle\))?[ \t]*$')
MAX_OUTPUT = 1024 * 1024


class InteractiveError(ValueError):
    """A safe, fixed diagnostic; never expose raw terminal/account output."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.user_message = code, message


class NativeWorkspaceConsent(InteractiveError):
    def __init__(self):
        super().__init__('native_workspace_trust_required',
            'AGY原生请求信任本次隔离工作目录；请由你在此节点确认，尚未发送模型任务。')


class ReviewReceiptError(InteractiveError):
    preserve_node_health = True


def visible(raw):
    return ANSI.sub('', raw.decode('utf-8', errors='replace')).replace('\r', '')


REVIEW_RUN_INSTRUCTION = (
    '独立审查下面的冻结任务卡及其明确授权材料。'
    '在当日工作回执中写完整审查正文，并在头部注明审查结论和实际证据引用。'
    '注明未执行的测试，按模板写明评分、审查类型与适用条件，不伪造评分、执行证据或用户批准。'
)


def parse_review_receipt(value, card, evidence):
    verdict = value['metadata'].get('审查结论') or value['metadata'].get('verdict')
    refs = value.get('evidence_refs') or []
    contract = card.get('receipt_contract', {})
    is_v2 = contract.get('version', 1) >= 2
    if value['status'] != 'completed' or verdict not in {'PASS', 'FAIL', 'NEEDS_INFO'} or not refs or any(r is None or (isinstance(r, str) and not r.strip()) for r in refs):
        raise ReviewReceiptError('review_fields_missing', '工作正文已收取并保存在当日文件；审查字段不全，未形成有效审查结论。')
    text = _sanitize_answer(value['body'])
    result = {'verdict': verdict, 'findings': text, 'evidence_refs': refs,
              'execution_evidence': dict(evidence, output_sha256=hashlib.sha256(text.encode()).hexdigest())}
    if is_v2:
        review_type = value['metadata'].get('审查类型') or value['metadata'].get('review_type')
        score_raw = value['metadata'].get('评分') or value['metadata'].get('score')
        applicability = value['metadata'].get('适用条件') or value['metadata'].get('适用条件与限制') or value['metadata'].get('applicability')
        duplicate_of = value['metadata'].get('重复经验') or value['metadata'].get('与已有经验重复') or value['metadata'].get('duplicate_of')
        score = None
        if score_raw is not None:
            try:
                score = float(score_raw)
                if score.is_integer():
                    score = int(score)
                if not (0 <= score <= 10):
                    score = None
            except (ValueError, TypeError):
                score = None
        card_evidence = card.get('evidence_refs', [])
        card_refs = {c.get('ref') for c in card_evidence if isinstance(c, dict)}
        valid_refs = all(
            (isinstance(r, str) and r in card_refs) or
            (isinstance(r, dict) and any(c.get('ref') == r.get('ref') and c.get('sha256') == r.get('sha256') for c in card_evidence if isinstance(c, dict)))
            for r in refs
        )
        if (review_type not in {'new_experience', 'revalidation', 'reuse_change'}
                or score is None
                or not isinstance(applicability, str) or not applicability.strip()
                or not valid_refs):
            raise ReviewReceiptError('review_fields_missing', '工作正文已收取并保存在当日文件；审查字段不全，未形成有效审查结论。')
        result['review_type'] = review_type
        result['score'] = score
        result['applicability'] = applicability.strip()
        if duplicate_of and isinstance(duplicate_of, str) and duplicate_of.strip() and duplicate_of.strip() not in {'无', 'none', 'None'}:
            result['duplicate_of'] = duplicate_of.strip()
        else:
            result['duplicate_of'] = None
    return result


class AgyInteractiveResource:
    supports_resource_plan = True
    persistent_resource = True
    supports_workday_receipts = True
    execution_mode = 'native_interactive_serial'
    test_budget_seconds = 120

    def __init__(self, source, workspace_root=None):
        self.kind, self.source_ref = source['kind'], source['source_ref']
        self.entry, self.model = source['node']['entry'], source['node']['model']
        self._serial = threading.Lock()
        self._process = self._master = self._workspace_lock = None
        self.workspace = AgentWorkspace(workspace_root, source)
        self.workday = WorkdayReceipts(self.workspace)
        self._preview = TerminalPreview()
        self._screen = TerminalScreen()
        self._view_session = None
        self._state = 'not_started'
        self._session = uuid.uuid4().hex
        self._retired = False
        self._turns = 0
        self.pending_action = None
        self._launch_checksum = None
        self._input_ready = False
        self._consent_submitted = False

    def resource_identity(self):
        path = Path(self.entry).resolve(strict=True)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise InteractiveError('entry_unavailable', 'AGY入口不是可执行文件。')
        if self._process is not None and self._process.poll() is not None:
            raise InteractiveError('session_closed', 'AGY交互会话已结束，请显式测试后再运行。')
        with path.open('rb') as stream:
            checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        if self._launch_checksum is not None and checksum!=self._launch_checksum:
            raise InteractiveError('entry_changed', 'AGY入口已变化，请关闭再开启节点并显式测试。')
        implementation = hashlib.sha256(b''.join(Path(__file__).with_name(name).read_bytes()
            for name in ('resource_agy.py','agent_workspace.py','workday.py','terminal_preview.py','terminal_screen.py'))).hexdigest()
        return {'protocol':'agy-interactive-worklog-v2', 'entry':str(path), 'sha256':checksum,
                'implementation_sha256':implementation, 'session':self._session,
                'workspace':str(self.workspace.path)}

    def terminal_snapshot(self):
        state = self._state
        if self._process is not None and self._process.poll() is not None:state = 'closed'
        return {**self._preview.snapshot(), 'supported':True, 'state':state,
                'session_id':self._view_session, 'turn':self._turns}

    def retire(self):
        # Turning a node off keeps an already running receipt, per AgentPool's
        # existing contract. No queued turn may begin after this flag is set.
        self._retired = True
        if self._serial.acquire(blocking=False):
            try:self.close()
            finally:self._serial.release()

    def close(self):
        process = self._process
        if process is not None:
            # First use the terminal's own cancel input; never confirm a prompt.
            if self._master is not None and process.poll() is None:
                try:os.write(self._master,b'\x03')
                except OSError:pass
            try:process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL);process.wait(timeout=2)
                except ProcessLookupError:process.wait(timeout=2)
                except PermissionError:
                    self._retired = True
                    raise InteractiveError('session_cleanup_blocked',
                        '系统拒绝结束本次AGY进程，请人工核对；本节点停止派活，未绕过系统限制。') from None
        self._process = None
        if self._master is not None:
            os.close(self._master);self._master = None
        if self._workspace_lock is not None:
            self._workspace_lock.close();self._workspace_lock = None
        self._state = 'closed'
        self._input_ready = False
        self._consent_submitted = False
        self.pending_action = None
        self._launch_checksum = None
        self._session = uuid.uuid4().hex

    def _environment(self):
        config = SimpleNamespace(integrations={'agy_command':self.entry,
            'node_command':shutil.which('node') or sys.executable})
        return dict(command_environment(config, 'agy'), TERM='xterm-256color', NO_COLOR='1')

    def _wait(self, done, timeout, idle_timeout=300, file_ticket=None):
        raw = bytearray();started = last_progress = time.monotonic();progress = ''
        while True:
            if file_ticket:
                receipt = self.workday.read(file_ticket)
                if receipt is not None:return receipt
                try:marker = str(file_ticket['log'].stat().st_size)
                except FileNotFoundError:marker = ''
                if marker != progress:progress=marker;last_progress=time.monotonic()
            if self._process is None or self._process.poll() is not None:
                raise InteractiveError('session_closed', 'AGY交互会话已退出；未自动重启或换用后台。')
            now = time.monotonic()
            if timeout is not None and now-started >= timeout:
                raise InteractiveError('interactive_timeout', 'AGY交互响应未在本次测试期限内完成；不会自动重测。')
            if now-last_progress >= idle_timeout:
                raise InteractiveError('interactive_no_progress', 'AGY交互会话连续300秒无可验证结果进展，已停止本次调用。')
            if not select.select([self._master], [], [], .1)[0]:continue
            try:chunk = os.read(self._master, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:raise InteractiveError('session_closed', 'AGY交互终端已关闭。') from None
                raise
            if not chunk:raise InteractiveError('session_closed', 'AGY交互终端已关闭。')
            self._screen.feed(chunk)
            self._preview.set_screen(self._screen.text())
            if not file_ticket:raw.extend(chunk)
            if not file_ticket and len(raw)>MAX_OUTPUT:
                raise InteractiveError('interactive_output_limit', 'AGY交互输出超过安全上限，已停止；不保存原始终端内容。')
            screen = self._screen.text()
            self._input_ready = bool(READY.search(screen) and not re.search(r'(?im)^\s*esc to cancel',screen))
            if file_ticket:
                # Terminal text is for observation only. It never supplies a
                # work result or proves that the current task is complete.
                continue
            answer, marker = done(screen)
            if marker != progress:
                progress = marker;last_progress = time.monotonic()
            if answer is not None:return answer

    def _start(self):
        identity = self.resource_identity()
        self._workspace_lock = self.workspace.lock_session()
        directory = self.workspace.ensure()
        self.workspace.verify()
        self._preview = TerminalPreview()
        self._view_session = self._session
        self._screen = TerminalScreen()
        self._state = 'starting'
        env = self._environment()
        # Pass the user's value verbatim. Do not query models, read defaults,
        # infer an effort variant, or require the banner to identify a model.
        argv = [identity['entry'], '--mode', 'accept-edits', '--sandbox', '--model', self.model]
        master, slave = pty.openpty();self._master = master
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH',80,240,0,0))
        try:
            self._process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                cwd=str(directory), env=env, start_new_session=True, close_fds=True)
        finally:os.close(slave)
        self._launch_checksum = identity['sha256']
        try:self._await_ready()
        except InteractiveError as exc:
            if exc.code=='interactive_timeout':
                error=InteractiveError('startup_ready_timeout','AGY已启动，但20秒内未识别可输入状态；尚未发送模型任务。')
                error.model_tasks_sent=0
                raise error from None
            raise

    def _await_ready(self, *, consent_submitted=False):
        def ready(screen):
            # A persistent screen may retain the already confirmed prompt.
            # After the user's Enter, wait for READY; never confirm again.
            if (not (consent_submitted or self._consent_submitted) and 'Do you trust the contents of this project?' in screen
                    and 'Yes, I trust this folder' in screen):
                return {'consent':True}, 'consent'
            if READY.search(screen) and not re.search(r'(?im)^\s*esc to cancel',screen):return True, 'ready'
            return None, ''
        result,_ = ready(self._screen.text()) if self._input_ready else (None,'')
        if result is None:result = self._wait(ready,20)
        if isinstance(result,dict):
            self._state = 'waiting_user'
            self.pending_action={'type':'native_workspace_trust','session_id':self._session,
                'action_id':uuid.uuid4().hex,'directory':str(self.workspace.path),
                'message':'AGY原生要求允许读取、编辑和执行此节点固定工作区的文件；只由用户确认。'}
            raise NativeWorkspaceConsent()
        self.pending_action = None
        self._state = 'idle'

    def confirm_workspace(self, action_id, session_id):
        with self._serial:
            pending=self.pending_action
            if (self._retired or not pending or action_id!=pending['action_id']
                    or session_id!=self._session or self._process.poll() is not None):
                raise InteractiveError('native_consent_stale', '原生目录确认已失效，请刷新节点。')
            try:
                # This method is reachable only via the explicit consent POST.
                # Ordinary test/run paths cannot press Enter on a native prompt.
                os.write(self._master,b'\r');self.pending_action=None;self._consent_submitted=True
                self._await_ready(consent_submitted=True)
            except NativeWorkspaceConsent:raise
            except BaseException:
                self.close();raise
            return {'state':'ready_for_explicit_test','model_tasks_sent':0}

    def _evidence(self):
        # model_verification was dropped by the user's 2026-11-01 ruling; old
        # receipts carrying it stay readable because no reader requires the key.
        return {'provider':'antigravity_cli', 'model':self.model, 'route_source':'native_interactive_session',
                'model_source':'node_configuration',
                'session_id':self._session, 'turn':self._turns, 'execution_mode':self.execution_mode}

    def _turn(self, instruction, context, timeout=None, kind='review', record_id=None, step_code=None, task_id=None, health_only=False):
        with self._serial:
            if self._retired:raise InteractiveError('session_closed', 'AGY节点已停用，本会话不再接收任务。')
            task_id = task_id or record_id or uuid.uuid4().hex
            step_code = step_code or ('HEALTH-01' if kind=='test' else 'REVIEW-01')
            materials = json.loads(context)
            payload={'task_id':task_id,'step_code':step_code,'model':self.model,'instruction':instruction,'context':materials}
            if kind=='work':
                prior=self.workspace.previous_work(record_id or task_id,payload)
                if prior:
                    value,evidence=prior
                    if value is None:raise InteractiveError('work_step_failed','原任务步骤已失败并留有记录，不自动重跑。')
                    return value,dict(evidence,replayed=True)
            before = self.resource_identity()
            record = None
            try:
                if self.pending_action:raise NativeWorkspaceConsent()
                if self._process is None:self._start()
                else:self._await_ready()
                record = self.workspace.begin(kind, record_id or task_id,payload)
                ticket = self.workday.prepare(record,task_id,step_code,instruction,materials,
                    review=kind=='review',health_only=health_only)
                self._state = 'busy'
                self._input_ready = False
                dispatch = ('这是文件写回探针，请读取这张卡并直接写入它指定的回执文件。'
                    '不要自行做系统健康诊断，不要新建当日台账。') if health_only else (
                    '读取并执行这张任务卡，在卡内指定的当日Markdown工作文件中追加过程和完整回执。')
                prompt = (dispatch+'只执行卡内授权范围；保留先前记录；不用在终端复述结果。\n'
                    'XIAOBA_TASK_CARD='+json.dumps(str(ticket['card']),ensure_ascii=False))
                # A single bracketed paste prevents embedded newlines/slashes
                # from becoming terminal commands. Enter submits one turn.
                encoded = prompt.encode('utf-8')
                if any(c in prompt for c in ('\x1b','\x00')) or len(encoded)>128*1024:
                    raise InteractiveError('invalid_interactive_input', '交互任务含终端控制字符或超过输入上限。')
                data = memoryview(b'\x1b[200~'+encoded+b'\x1b[201~')
                while data:data=data[os.write(self._master,data):]
                os.write(self._master, b'\r')
                value = self._wait(lambda screen:(None,''), timeout, file_ticket=ticket)
                if before != self.resource_identity():raise InteractiveError('entry_changed', 'AGY入口在交互期间发生变化。')
                self._turns += 1
                evidence = dict(self._evidence(),reply_source='daily_worklog',worklog=value['worklog'])
                self.workspace.finish(record,'execution_completed' if value['status']=='completed' else value['status'],value,evidence)
                self._state = 'receipt_received'
                return value, evidence
            except NativeWorkspaceConsent:raise
            except BaseException as exc:
                try:
                    if record and (not isinstance(exc,WorkspaceError) or exc.code in {
                            'worklog_receipt_invalid','worklog_task_mismatch','worklog_non_answer',
                            'worklog_path_invalid','worklog_limit','worklog_receipt_conflict'}):
                        self.workspace.finish(record,'failed',evidence=self._evidence(),reason=getattr(exc,'code',type(exc).__name__))
                finally:self.close()
                raise
            finally:
                if self._retired:self.close()

    def test_resource(self, plan, challenge):
        value, evidence = self._turn('本任务只验证文件写回通道，不要求你执行系统健康检查或诊断。'
            '主入口负责根据写回结果判断健康，你只需用文件读写工具将给定内容写入本卡指定的回执文件。'
            '步骤码只是关联编号，不需要查找其实现，不查历史任务或小八源码。'
            '既有治理要求保持；不为本卡运行命令、不联网、不做额外诊断，只写指定回执文件，不新建当日台账。'
            '回执正文只写材料中的challenge原文，不要JSON或代码围栏。',
            json.dumps({'challenge':challenge},ensure_ascii=False), timeout=90,kind='test',record_id=challenge,health_only=True)
        if value['status']!='completed' or value['body'] != challenge:
            self.close()
            raise InteractiveError('challenge_mismatch', '文件回执未完成本次健康挑战；没有标记可用。')
        return dict(evidence, output=value['body'])

    def initialize_workday(self,health_evidence,task_id):
        value,evidence=self._turn('主入口已核验前一步健康检测通过。由你新建或沿用当日工作Markdown，'
            '只追加本次开工记录，保留原记录。回执正文只写材料中的challenge原文，不用JSON。',
            json.dumps({'challenge':'WORKDAY_READY','health_check':health_evidence},ensure_ascii=False),
            timeout=90,kind='test',record_id=task_id,step_code='WORKDAY-INIT')
        if value['status']!='completed' or value['body']!='WORKDAY_READY':
            self.close();raise InteractiveError('workday_initialization_failed','健康检测通过，但当日工作台账初始化未完成。')
        return evidence

    def run(self, card):
        value, evidence = self._turn(REVIEW_RUN_INSTRUCTION,
            json.dumps(card, ensure_ascii=False), record_id=card.get('assignment_id'),
            task_id=card.get('task_id') or card.get('review_request_id'), step_code=card.get('step_code'))
        return parse_review_receipt(value, card, evidence)

    def run_task(self,task_id,step_code,instruction,materials):
        if not isinstance(instruction,str) or not instruction.strip():raise ValueError('需要明确授权的工作内容和范围')
        value,evidence=self._turn(instruction,json.dumps(materials,ensure_ascii=False),kind='work',
            task_id=task_id,step_code=step_code,record_id=task_id+'-'+step_code)
        return {'task_id':task_id,'step_code':step_code,'status':value['status'],'body':value['body'],
            'execution_evidence':evidence,'score_assigned':False}
