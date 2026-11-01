"""CLI type, availability and stable code identity. Never open client settings."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import sys
from types import SimpleNamespace

from xiaoba_agentos.gateway import command_environment

TYPES = ('agy', 'dsh', 'qoder', 'workbuddy', 'codex', 'claude', 'other')
ENTRY_TYPES = {'agy':'agy', 'antigravity':'agy', 'dsh':'dsh', 'qoder':'qoder',
               'qodercli':'qoder', 'codebuddy':'workbuddy', 'workbuddy':'workbuddy',
               'codex':'codex', 'codex.js':'codex', 'claude':'claude'}
SESSION_VARIABLES = ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CODEX_THREAD_ID',
                     'CODEX_INTERNAL_ORIGINATOR_OVERRIDE', 'AGY_SESSION_ID')


def agent_type(node):
    if node.get('agent_type') in TYPES:
        return node['agent_type']
    path = Path(node.get('entry') or '')
    if path.name == 'bin.js' and path.parts[-4:-1] == ('apps', 'cli', 'lib'):
        return 'dsh'
    return ENTRY_TYPES.get(path.name.casefold(), 'other')


def entry_warning(entry):
    if re.search(r'(?:^|[/\\])versions[/\\]|\d+\.\d+\.\d+', entry):
        return ('入口含版本目录或版本号，更新后可能失效；建议改用不带版本的稳定入口或软链。'
                'DSH 建议使用 current 下的 CLI；仍允许保存当前路径。')
    return None


def require_entry(entry):
    from .resource_agy import InteractiveError
    path = Path(entry)  # Deliberately do not resolve a symlink or hash its target.
    if not path.is_file() or not os.access(path, os.X_OK):
        raise InteractiveError('entry_unavailable', '调用入口不存在或不可执行；请修改节点的入口路径后重新测试运行。')
    return path


def implementation_digest(names):
    return hashlib.sha256(b''.join(Path(__file__).with_name(name).read_bytes() for name in names)).hexdigest()


def cli_identity(entry, workspace, kind):
    require_entry(entry)
    return {'protocol':'cli-worklog-v1', 'agent_type':kind,
            'implementation_sha256':implementation_digest((
                'cli_identity.py', 'resource_cli.py', 'resource_agy_cli.py', 'resource_vendor_cli.py',
                'resource_stdio.py', 'resource_agy.py', 'agent_workspace.py', 'workday.py')),
            'workspace':str(workspace.path)}


def cli_environment(entry, kind):
    driver = kind if kind in {'agy', 'dsh', 'qoder'} else 'agy'
    config = SimpleNamespace(integrations={driver+'_command':entry,
        'node_command':shutil.which('node') or sys.executable})
    env = dict(command_environment(config, driver), NO_COLOR='1')
    for key in SESSION_VARIABLES:
        env.pop(key, None)
    # Qoder's native adapter removes every QODER_* variable, including session
    # state. Apply that same rule to every node so it cannot leak into peers.
    for key in list(env):
        if key.startswith('QODER_'):env.pop(key)
    return env


def specific_failure(kind, label, model, stdout, stderr, check_model=True):
    from .resource_agy import InteractiveError
    text = (stderr+'\n'+stdout).casefold()
    # Do not return any excerpt, even when it looks harmless.
    code = message = None
    if any(s in text for s in ('cannot find module', 'unknown option', 'unrecognized option',
                                'unexpected argument', 'unknown command')):
        code, message = 'unsupported_entry', label+'入口无法启动或不支持适配参数；请检查 CLI 安装和节点类型。'
    elif any(s in text for s in ('not logged in', 'not authenticated', 'authentication required',
                                  'authentication failed', 'login required', 'please log in',
                                  'please login', 'please sign in', 'unauthorized',
                                  'invalid api key', 'invalid authentication', '未登录', '请先登录')):
        code, message = 'cli_not_logged_in', label+'尚未登录或登录已失效；请你在该 CLI 中处理登录，再手动测试运行。'
    elif check_model and any(re.search(p, text) for p in (
            r'(?:unknown|invalid|unsupported|unrecognized) model',
            r'model[^\n]{0,160}(?:not found|not recognized|does not exist|not supported|not available)',
            r'(?:模型不存在|不支持的模型|未知模型)')):
        code = 'model_not_recognized'
        if kind == 'dsh' or not model:  # 这次调用没有传节点里的模型名。
            message = label+'不认它当前配置的模型；请在该 CLI 中处理模型设置后重新测试运行。'
        else:
            message = label+'不认节点里的模型名“'+model+'”；请修改节点模型后重新测试运行。'
    if code:
        failure = InteractiveError(code, message+'不自动重试、不换模型、不换后台。')
        failure.failure_class = 'permanent'
        return failure
    return None


def cli_failure(kind, label, model, returncode, stdout, stderr):
    from .resource_agy import InteractiveError
    from xiaoba_agentos.adapters import _is_transient_cli_error, _redact_error
    specific = specific_failure(kind, label, model, stdout, stderr)
    if specific:return specific
    if returncode == 0:
        return InteractiveError('receipt_missing', label+'本轮已结束，但没有写出合格的任务卡回执；不自动重试。')
    failure = InteractiveError('cli_failed', label+'调用异常退出，未完成任务卡回执；不自动重试、不换模型、不换后台。')
    failure.failure_class = 'transient' if _is_transient_cli_error(_redact_error(stderr or stdout)) else 'permanent'
    return failure
