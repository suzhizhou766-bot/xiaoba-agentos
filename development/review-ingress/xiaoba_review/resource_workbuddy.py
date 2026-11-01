"""WorkBuddy's bundled lite engine: an owned, password-protected ACP server.

The app's lite bundle does not implement the standalone print/stdio lifecycle.
Only the CLI reads its own settings/authentication. This client neither copies
desktop login material nor asks for account information or available models.
"""
import json
from pathlib import Path
import secrets
import socket
import re
import threading
import time
import urllib.error
import urllib.request

from .resource_agy import InteractiveError
from .resource_cli import CliWorklogResource


FILE_TOOLS = 'Read,Write,Edit,Glob,Grep'


def protocol_failure():
    return InteractiveError('workbuddy_protocol_error',
        'WorkBuddy本地ACP通信未完成；请检查应用内置CLI是否支持服务模式。不自动重试。')


def execution_failure(error, model):
    from .cli_identity import cli_failure
    text = error if isinstance(error, str) else json.dumps(error)
    try: data = json.loads(error) if isinstance(error, str) else error
    except ValueError: data = None
    if isinstance(data, dict) and isinstance(data.get('error'), dict): data = data['error']
    details = data.get('data') if isinstance(data, dict) else None
    category = details.get('category') if isinstance(details, dict) else None
    failure = cli_failure('workbuddy', 'WorkBuddy', model, 1, '',
        'authentication required' if category == 'auth' else text)
    # Only known enum values and numeric protocol codes survive classification.
    failure.native_category = category if category in {'auth', 'model_service', 'custom_model_auth',
        'permission', 'network', 'validation', 'config'} else 'unknown'
    code = data.get('code') if isinstance(data, dict) else None
    if type(code) is int and -32768 <= code <= 32767: failure.native_code = code
    failure.native_local_loopback = bool(re.search(r'https?://(?:127\.0\.0\.1|localhost)(?=[:/])', text))
    return failure


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward our ephemeral bearer password to another listener.
        return None


class AcpClient:
    """Bounded local JSON/SSE RPC; raw events and secrets never leave this object."""

    limit = 1024 * 1024

    def __init__(self, port, password, timeout, model=''):
        self.base = 'http://127.0.0.1:%d' % port
        self.timeout = timeout
        self.model = model
        self.headers = {'Authorization': 'Bearer ' + password,
            'X-CodeBuddy-Request': '1', 'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream'}
        # Programmatic clients omit Origin; the native request guard stays on.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.sequence = 0
        self.notification_error = None
        self.notification_ready = threading.Event()
        self.notification_thread = None
        self.on_notification = None

    def events(self, response):
        used, event = 0, []
        while used <= self.limit:
            line = response.readline(self.limit - used + 1)
            if not line: return
            used += len(line)
            if line.startswith(b'data:'): event.append(line[5:].strip())
            if line.strip(): continue
            if not event: continue
            message = json.loads(b'\n'.join(event)); event = []
            if not isinstance(message, dict): raise protocol_failure()
            yield message
        raise protocol_failure()

    @staticmethod
    def check_request(message):
        if 'method' in message and 'id' in message:
            # Never supply unattended approval, client-side fs, or a terminal.
            raise InteractiveError('native_permission_required',
                'WorkBuddy请求人工许可或客户端工具，本次已停止；请在原生界面处理，不自动批准。')

    def handle_message(self, message):
        self.check_request(message)
        if 'method' in message and self.on_notification: self.on_notification(message)

    def request(self, path, value=None, timeout=None):
        data = None if value is None else json.dumps(value, ensure_ascii=False).encode()
        request = urllib.request.Request(self.base + path, data=data, headers=self.headers)
        try: response = self.opener.open(request, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise protocol_failure() from None
        with response:
            if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                body = response.read(self.limit + 1)
                if len(body) > self.limit: raise protocol_failure()
                result = json.loads(body)
                if not isinstance(result, dict): raise protocol_failure()
                return result
            for message in self.events(response):
                self.handle_message(message)
                if message.get('id') == (value or {}).get('id') and ('result' in message or 'error' in message):
                    return message
            raise protocol_failure()

    def initialize(self, on_notification=None):
        self.on_notification = on_notification
        result = self.request('/api/v1/acp/connect', {})
        connection = result.get('data', result)
        if not isinstance(connection, dict) or not connection.get('connectionId') or not connection.get('sessionToken'):
            raise protocol_failure()
        self.headers.update({'acp-connection-id': connection['connectionId'],
                             'acp-session-token': connection['sessionToken']})
        def listen():
            try:
                request = urllib.request.Request(self.base + '/api/v1/acp', headers=self.headers)
                with self.opener.open(request, timeout=self.timeout) as response:
                    if 'text/event-stream' not in response.headers.get('Content-Type', ''): raise protocol_failure()
                    self.notification_ready.set()
                    for message in self.events(response):
                        self.handle_message(message)
            except InteractiveError as exc: self.notification_error = exc
            except Exception: self.notification_error = protocol_failure()
            finally: self.notification_ready.set()
        self.notification_thread = threading.Thread(target=listen, daemon=True)
        self.notification_thread.start()
        if not self.notification_ready.wait(min(5, self.timeout)): raise protocol_failure()
        if self.notification_error: raise self.notification_error
        self.rpc('initialize', {'protocolVersion': 1, 'clientCapabilities': {},
            'clientInfo': {'name': 'xiaoba-worklog', 'version': '1'}})

    def rpc(self, method, params):
        self.sequence += 1
        message = self.request('/api/v1/acp', {'jsonrpc': '2.0', 'id': self.sequence,
            'method': method, 'params': params})
        if message.get('jsonrpc') != '2.0' or message.get('id') != self.sequence: raise protocol_failure()
        if 'error' in message:
            raise execution_failure(message['error'], self.model)
        result = message.get('result')
        if not isinstance(result, dict): raise protocol_failure()
        meta = result.get('_meta')
        # Native ACP can finish the RPC successfully while execution failed.
        # Error data is classified in memory, never treated as a task receipt.
        error = meta.get('codebuddy.ai/errorMessage') if isinstance(meta, dict) else None
        if error: raise execution_failure(error, self.model)
        return result


class WorkBuddyCliResource(CliWorklogResource):
    agent_type, label, provider = 'workbuddy', 'WorkBuddy', 'workbuddy_cli'
    receipt_grace_seconds = 0
    startup_seconds = 20

    def _environment(self):
        env = dict(super()._environment())
        # Do not inherit another application's gateway or credential bootstrap.
        # The CLI retains its normal profile; no desktop config is opened here.
        for key in list(env):
            if key.startswith(('CODEBUDDY_', 'WORKBUDDY_', 'ACC_PRODUCT_CONFIG')) or key in ('SERVER__PORT', 'SERVER__HOST'):
                env.pop(key)
        env.update(CODEBUDDY_FORCE_LITE_WB_BUNDLE='1', DISABLE_AUTOUPDATER='1',
                   CODEBUDDY_HOST='workbuddy-desktop', CODEBUDDY_DISABLE_PRODUCT_CACHE='1',
                   CODEBUDDY_GATEWAY_AUTH='password')
        if getattr(self, '_password', None):
            env.update(CODEBUDDY_GATEWAY_PASSWORD=self._password,
                       SERVER__HOST='127.0.0.1', SERVER__PORT=str(self._port))
        # This is installed public product metadata, not a user's settings file.
        # Let the native product manager select WorkBuddy AI versus WorkBuddy.
        entry = Path(self.entry).resolve()
        if entry.parts[-6:-2] == ('Contents', 'Resources', 'app.asar.unpacked', 'cli'):
            product = entry.parent.parent / 'product.json'
            if product.is_file(): env['ACC_PRODUCT_CONFIG_PATH'] = str(product)
        return env

    def _command(self, entry, timeout):
        return [entry, '--serve', '--host', '127.0.0.1', '--port', str(self._port),
                '--auth', 'password', *(['--model', self.model] if self.model else []),
                '--permission-mode', 'acceptEdits', '--no-session-persistence',
                '--tools', FILE_TOOLS, '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}']

    @staticmethod
    def _pump(stream, sink):
        # Startup logs contain the local gateway password. Drain, never retain.
        try:
            while stream.read(8192): pass
        except (OSError, ValueError): pass
        finally: sink(None)

    def _pump_stderr(self, stream):
        try:
            while stream.read(4096): pass
        except (OSError, ValueError): pass

    def _start(self, directory, ticket, timeout, health_only):
        self.resource_identity()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self._port = sock.getsockname()[1]
        self._password = secrets.token_urlsafe(32)
        self._operation = {'done': threading.Event(), 'error': None}
        self._spawn(self._command(self.entry, timeout), directory)
        self._refresh_preview(ticket, self._log_size(ticket))
        # Keep request state per process, so cleanup/reuse cannot mix sessions.
        process = self._process
        client = AcpClient(self._port, self._password, (timeout or self.no_progress_seconds) + 5, self.model)
        self._client = client
        dispatch = self._dispatch(ticket, health_only)
        operation = self._operation

        def run():
            account_ready = False
            try:
                deadline = time.monotonic() + min(self.startup_seconds, timeout or self.startup_seconds)
                while True:
                    try:
                        client.request('/api/v1/health', timeout=1)
                        break
                    except (OSError, urllib.error.URLError):
                        if process.poll() is not None or time.monotonic() >= deadline: raise protocol_failure()
                        time.sleep(.05)
                # This native status endpoint waits for auth initialization and
                # contains no tokens (unlike ACP getUserInfo). Ignore all account
                # fields: only the readiness boolean is needed here.
                status = client.request('/api/v1/auth/account/status')
                status = status.get('data', status)
                authenticated = status.get('authenticated') if isinstance(status, dict) else None
                if authenticated is False:
                    from .cli_identity import specific_failure
                    raise specific_failure('workbuddy', 'WorkBuddy', self.model, '', 'authentication required')
                if authenticated is not True: raise protocol_failure()
                account_ready = True
                client.initialize()
                session = client.rpc('session/new', {'cwd': str(directory), 'mcpServers': []})
                session_id = session.get('sessionId')
                if not isinstance(session_id, str) or not session_id: raise protocol_failure()
                client.rpc('session/prompt', {'sessionId': session_id,
                    'prompt': [{'type': 'text', 'text': dispatch}]})
            except InteractiveError as exc:
                if account_ready and exc.code == 'cli_not_logged_in':
                    failure = InteractiveError('workbuddy_execution_auth_rejected',
                        'WorkBuddy CLI账号已就绪，但任务通道拒绝认证；不能据此判断桌面端退出登录。'
                        '本次已停止，不自动重试或重新登录。')
                    failure.failure_class = 'permanent'
                    for field in ('native_category', 'native_code', 'native_local_loopback'):
                        if hasattr(exc, field): setattr(failure, field, getattr(exc, field))
                    exc = failure
                operation['error'] = exc
            except Exception:
                operation['error'] = protocol_failure()
            finally: operation['done'].set()

        self._operation_thread = threading.Thread(target=run, daemon=True)
        self._operation_thread.start()

    def _check_operation(self):
        if self._client.notification_error: raise self._client.notification_error
        if self._operation['done'].is_set():
            raise self._operation['error'] or InteractiveError('receipt_missing',
                'WorkBuddy本轮请求已结束，但没有写出合格的任务卡回执；不自动重试。')

    def _settle_exit(self):
        self._check_operation()
        raise protocol_failure()

    def _release(self):
        try: super()._release()
        finally:
            thread = getattr(self, '_operation_thread', None)
            if thread is not None: thread.join(timeout=2)
            client = getattr(self, '_client', None)
            if client and client.notification_thread: client.notification_thread.join(timeout=2)
            self._client = None
            self._password = None

    def _evidence(self, call_id=None):
        return dict(super()._evidence(call_id), transport='native_acp_http')

    def _failure_evidence(self, exc, call_id):
        evidence = super()._failure_evidence(exc, call_id)
        for field in ('native_category', 'native_code', 'native_local_loopback'):
            if hasattr(exc, field): evidence[field] = getattr(exc, field)
        return evidence
