"""One CLI process per card, with shared workspace, worklog and process ownership.

Raw stdout/stderr only supplies bounded diagnostic classification and event
counts. A validated task-card file receipt is the only business result.
"""
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

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
# 材料总量上限（常量）：单任务最多 10000 个文件、200 MB。
MATERIAL_FILE_LIMIT = 10000
MATERIAL_BYTE_LIMIT = 200 * 1024 * 1024
MATERIAL_SKIP_DIRS = ('.git', '__pycache__')
# 凭据文件的文件名特征：只认真正存放凭据的文件，workbuddy_login.py 这类源码不算。
MATERIAL_SENSITIVE_NAME = re.compile(
    r'(?i)^(?:\.env(?:\..+)?|\.netrc|\.npmrc|\.pgpass|id_(?:rsa|dsa|ecdsa|ed25519)|'
    r'credentials?(?:\.(?:json|ya?ml|toml|txt|ini))?|secrets?\.(?:json|ya?ml|toml|txt|ini)|'
    r'auth\.json|cookies?(?:\.(?:json|txt|sqlite))?)$'
    r'|\.(?:pem|key|p12|pfx|kdbx)$'
    r'|凭据|密码|密钥|口令')
# 代码和差异文件里常有假密钥夹具和字段名，内容只拦私钥；其它文本按 _sensitive_reason 全量检查。
MATERIAL_CODE_SUFFIXES = frozenset(('.py', '.pyi', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx',
    '.html', '.css', '.sh', '.zsh', '.bash', '.swift', '.c', '.h', '.go', '.rs', '.java',
    '.diff', '.patch'))
MATERIAL_PRIVATE_KEY = re.compile(r'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----\s*[A-Za-z0-9+/=]{16,}')


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
    # 只防失控刷屏。流式输出会带上读过的文件内容，审查大材料时远超 1 MiB；业务结果只看文件回执。
    output_limit = 64 * 1024 * 1024
    # 材料上限取模块常量；测试可在实例上调低，出厂值不变。
    material_file_limit = MATERIAL_FILE_LIMIT
    material_byte_limit = MATERIAL_BYTE_LIMIT

    def __init__(self, source, workspace_root=None):
        self.kind, self.source_ref = source['kind'], source['source_ref']
        self.entry, self.model = source['node']['entry'], source['node']['model']
        limit = source['node'].get('no_progress_seconds', self.no_progress_seconds)
        if type(limit) is int and limit >= 1:
            self.no_progress_seconds = limit
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
        self._spawn(argv, directory, self._input(ticket, health_only))
        self._refresh_preview(ticket, self._log_size(ticket))

    def _spawn(self, argv, directory, input_data=None):
        """Own a fresh process group; service dialects send their input over IPC."""
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
            if input_data is not None:
                process.stdin.write(input_data)
                process.stdin.flush()
        finally:
            try: process.stdin.close()
            except OSError: pass
        self._queue = queue.Queue()
        threading.Thread(target=self._pump, args=(process.stdout, self._queue.put),
                         daemon=True).start()
        self._stderr_thread = threading.Thread(target=self._pump_stderr, args=(process.stderr,), daemon=True)
        self._stderr_thread.start()

    def _check_operation(self):
        """Service dialects can finish a request before their server exits."""

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
            try:
                receipt = self.workday.read(ticket)
            except WorkspaceError as error:
                # 进程还在跑时，写坏的回执块不算结论，节点可能还在写；退出时仍判无效。
                if error.code != 'worklog_receipt_invalid' or process.poll() is not None: raise
                receipt = None
            if receipt is not None:
                self._drain()
                _, terminated = self._settle(receipt, process)
                # 进程可能在 done 行之后又改回执：等它结束再读，证据绑定最终内容。
                receipt = self.workday.read(ticket)
                self._refresh_preview(ticket, self._log_size(ticket))
                if receipt is None:
                    raise WorkspaceError('worklog_receipt_invalid', '节点写完回执后又把它改坏了；原文件保留。')
                return receipt, terminated
            current = self._log_size(ticket)
            if current != size:
                size = current; last_progress = time.monotonic()
                self._refresh_preview(ticket, current)
            self._check_operation()
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

    # -- material staging: every CLI reads copies in its own workspace -------

    @staticmethod
    def declared_materials(materials):
        """冻结声明里的文件材料：material_files 全部；evidence_refs 只认绝对路径 ref。

        标签或 URI 形式的 ref（如 test://、task:）不是文件，不当材料处理。
        """
        found, seen = [], set()
        if not isinstance(materials, dict): return found

        def add(path, sha256):
            if not isinstance(path, str) or not path or path in seen: return
            seen.add(path)
            found.append({'path': path, 'sha256': sha256})

        for item in materials.get('material_files') or []:
            if isinstance(item, dict): add(item.get('path'), item.get('sha256'))
        for item in materials.get('evidence_refs') or []:
            if isinstance(item, dict) and isinstance(item.get('ref'), str) and os.path.isabs(item['ref']):
                add(item['ref'], item.get('sha256'))
        return found

    def _material_refused(self, source):
        """凭据类材料拒绝派发，不是静默跳过。"""
        from xiaoba_agentos.memory import MemoryBridge
        if MATERIAL_SENSITIVE_NAME.search(source.name):
            raise WorkspaceError('material_sensitive',
                '任务材料“%s”是凭据类文件，本次不派活。' % source)
        try:
            with source.open('rb') as stream:
                content = stream.read()
        except OSError:
            raise WorkspaceError('material_missing',
                '任务材料“%s”读不到，本次不派活。' % source) from None
        if b'\0' in content[:65536]: return
        text = content.decode('utf-8', 'replace')
        found = (MATERIAL_PRIVATE_KEY.search(text) if source.suffix.lower() in MATERIAL_CODE_SUFFIXES
                 else MemoryBridge._sensitive_reason(text))
        if found:
            raise WorkspaceError('material_sensitive',
                '任务材料“%s”包含凭据或登录资料，本次不派活。' % source)

    @staticmethod
    def _readonly_tree(target):
        for dirpath, dirnames, filenames in os.walk(target):
            for name in filenames:
                os.chmod(Path(dirpath) / name, 0o444)
            for name in dirnames:
                os.chmod(Path(dirpath) / name, 0o555)

    def _stage_materials(self, record, materials):
        """派活前把声明材料复制进 任务记录/<记录>/材料/，副本只读。

        每份材料都要过：符号链接拒绝、目录跳过 .git/__pycache__、总量上限、
        凭据类拒绝、复制前后 SHA-256 都与冻结值比对。任何一项不过就拒绝派活并写明原因，
        CLI 不会被调用。同一记录重新派发时复用校验过的副本。
        """
        from xiaoba_agentos.task_context import material_digest
        declared = self.declared_materials(materials)
        if not declared: return []
        directory = self.workspace.ensure() / '任务记录' / record / '材料'
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        staged, names = [], set()
        total_files, total_bytes = 0, 0
        for item in declared:
            source = Path(item['path'])
            if source.is_symlink():
                raise WorkspaceError('material_symlink',
                    '任务材料“%s”是符号链接，本次不派活。' % item['path'])
            if not source.exists():
                raise WorkspaceError('material_missing',
                    '任务材料“%s”不存在，本次不派活。' % item['path'])
            files = []
            if source.is_dir():
                for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
                    for name in list(dirnames):
                        if name in MATERIAL_SKIP_DIRS:
                            dirnames.remove(name)
                        elif (Path(dirpath) / name).is_symlink():
                            raise WorkspaceError('material_symlink',
                                '任务材料“%s”内含符号链接，本次不派活。' % item['path'])
                    files.extend(Path(dirpath) / name for name in sorted(filenames))
            elif source.is_file():
                files.append(source)
            else:
                raise WorkspaceError('material_missing',
                    '任务材料“%s”不是普通文件或目录，本次不派活。' % item['path'])
            for path in files:
                if path.is_symlink():
                    raise WorkspaceError('material_symlink',
                        '任务材料“%s”内含符号链接，本次不派活。' % item['path'])
            total_files += len(files)
            total_bytes += sum(path.stat().st_size for path in files)
            if total_files > self.material_file_limit or total_bytes > self.material_byte_limit:
                raise WorkspaceError('material_limit',
                    '任务材料总量超过上限（最多 %d 个文件、%d 字节），本次不派活。'
                    % (self.material_file_limit, self.material_byte_limit))
            for path in files:
                self._material_refused(path)
            if not item.get('sha256') or material_digest(source) != item['sha256']:
                raise WorkspaceError('material_hash_mismatch',
                    '任务材料“%s”与冻结时的 SHA-256 不符，本次不派活。' % item['path'])
            name = source.name
            while name in names:
                name += '-2'
            names.add(name)
            target = directory / name
            if target.exists():
                if material_digest(target) != item['sha256']:
                    raise WorkspaceError('material_hash_mismatch',
                        '任务记录里已有的材料副本“%s”与冻结时的 SHA-256 不符，本次不派活。' % name)
                staged.append(target)
                continue
            # 先复制到临时名，校验通过才换成正式名，中断时不留半份副本。
            temporary = directory / ('.staging-' + uuid.uuid4().hex)
            if source.is_dir():
                shutil.copytree(source, temporary,
                    ignore=shutil.ignore_patterns(*MATERIAL_SKIP_DIRS), symlinks=False)
            else:
                shutil.copy2(source, temporary)
            if material_digest(temporary) != item['sha256']:
                raise WorkspaceError('material_hash_mismatch',
                    '任务材料“%s”复制后与冻结时的 SHA-256 不符，本次不派活。' % item['path'])
            if source.is_dir():
                self._readonly_tree(temporary)
                os.chmod(temporary, 0o555)
            else:
                os.chmod(temporary, 0o444)
            os.rename(temporary, target)
            staged.append(target)
        os.chmod(directory, 0o555)
        return staged

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
                staged = self._stage_materials(record, materials)
                ticket = self.workday.prepare(record, task_id, step_code, instruction, materials,
                    review=kind == 'review', health_only=health_only, staged=staged)
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
