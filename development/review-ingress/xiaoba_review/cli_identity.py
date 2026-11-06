"""CLI type, availability and stable code identity. Never open client settings."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
from types import SimpleNamespace

from xiaoba_agentos.gateway import command_environment
from xiaoba_agentos.pool_dispatch import WORKBUDDY_RETIRED_CODE, WORKBUDDY_RETIRED_MESSAGE

# 卡 45：内置 WorkBuddy 引擎已退役。'workbuddy' 仍在这里，只为识别旧节点数据；
# 新建节点不再提供这个类型（Agent 池页面与 add 都会拒绝）。
TYPES = ('agy', 'dsh', 'qoder', 'workbuddy', 'codebuddy', 'codex', 'claude', 'other')
ENTRY_TYPES = {'agy':'agy', 'antigravity':'agy', 'dsh':'dsh', 'qoder':'qoder',
               'qodercli':'qoder',
               # 入口名 codebuddy 是 WorkBuddy CLI（CodeBuddy），判成 codebuddy；
               # 入口名 workbuddy 的旧内置引擎条目仍判 workbuddy。
               'codebuddy':'codebuddy', 'workbuddy':'workbuddy',
               'codex':'codex', 'codex.js':'codex', 'claude':'claude'}
SESSION_VARIABLES = ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CODEX_THREAD_ID',
                     'CODEX_INTERNAL_ORIGINATOR_OVERRIDE', 'AGY_SESSION_ID')
# 被结束的进程（含小八自己发出的 SIGTERM/SIGKILL）：单独归类，绝不显示成“被系统拒绝执行”。
TERMINATED_RETURNCODES = (-signal.SIGTERM, -signal.SIGKILL,
                          128 + signal.SIGTERM, 128 + signal.SIGKILL)
# 无人值守下权限系统拒绝读文件/跑命令的原文标记（卡 40 summary-B 逐字取用）。
PERMISSION_MARKERS = ('permission check failed', 'auto-denied', 'cannot prompt')
PERMISSION_MESSAGE = ('节点读工作区外的文件或要跑命令被权限系统拒绝，'
    '请把文件作为材料提交，或走 tmux 在场通道。')


def agent_type(node):
    if node.get('agent_type') in TYPES:
        return node['agent_type']
    path = Path(node.get('entry') or '')
    if path.name == 'bin.js' and path.parts[-4:-1] == ('apps', 'cli', 'lib'):
        return 'dsh'
    return ENTRY_TYPES.get(path.name.casefold(), 'other')


def source_family(kind):
    """用户 2026-11-02：WorkBuddy 的 CLI 就是 CodeBuddy，两种类型只算一个来源。"""
    return 'codebuddy' if kind == 'workbuddy' else kind


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


# 卡 43：节点测试运行期限不再散落常量。取节点已设的掉线时限（上限 300 秒）；节点
# 没设掉线时限时仍是 90 秒。所有 CLI 类型的测试调用（健康检查、上班初始化）都从这里取。
TEST_TIMEOUT_SECONDS = 90
TEST_TIMEOUT_CAP_SECONDS = 300
# 卡 43：失败分类只看诊断文字；失败记录只留脱敏后的错误尾部，原始输出不保存。
DIAGNOSTIC_SCOPE_BYTES = 4096
ERROR_TAIL_BYTES = 2048


def test_timeout_seconds(node):
    """测试运行期限：跟节点掉线时限走（上限 300 秒）；没设掉线时限仍是 90 秒。"""
    limit = node.get('no_progress_seconds') if isinstance(node, dict) else None
    if type(limit) is int and limit >= 1:
        return min(limit, TEST_TIMEOUT_CAP_SECONDS)
    return TEST_TIMEOUT_SECONDS


def _last_bytes(text, limit):
    """text 的最后 limit 字节，按 UTF-8 截断（切坏的多字节序列按 replace 显示）。"""
    return (text or '').encode('utf-8', 'replace')[-limit:].decode('utf-8', 'replace')


def non_event_lines(text, limit=DIAGNOSTIC_SCOPE_BYTES):
    """text 末尾 limit 字节里不是结构化事件（不能解析成 JSON 的行）的文字。

    流式 CLI 把读过的源码和工具结果都装在 JSON 事件里，这些内容整体丢弃，不参与
    匹配；只有 CLI 自己打的普通文字（横幅、报错）算诊断材料。被截断的 JSON 行解析
    不了，也按普通文字留下。
    """
    kept = []
    for line in _last_bytes(text, limit).splitlines():
        if not line.strip():
            continue
        try:
            if isinstance(json.loads(line.strip()), dict):
                continue
        except ValueError:
            pass
        kept.append(line)
    return '\n'.join(kept)


def diagnostic_text(stdout, stderr):
    """失败分类用的诊断文字：stderr 全文，加上 stdout 末 4 KB 的非事件行。

    读过的源码和工具结果都在结构化事件里，不参与匹配（卡 42 第七次发卡：源码里的
    `unknown option` 曾把超时退出误判成 unsupported_entry）。
    """
    return (stderr or '') + '\n' + non_event_lines(stdout)


def error_tail(stdout, stderr):
    """失败记录保留的脱敏错误尾部：stderr 末 2 KB；stderr 为空时取 stdout 末 2 KB
    的非事件行。只返回经 `_redact_error` 处理后的文字，原始输出不返回、不保存。
    """
    from xiaoba_agentos.adapters import _redact_error
    tail = _last_bytes(stderr, ERROR_TAIL_BYTES)
    if not tail.strip():
        tail = non_event_lines(stdout, ERROR_TAIL_BYTES)
    return _redact_error(tail).strip()


def specific_failure(kind, label, model, stdout, stderr, check_model=True, returncode=None):
    from .resource_agy import InteractiveError
    text = diagnostic_text(stdout, stderr).casefold()
    # 被 SIGTERM/SIGKILL 结束的进程最先归类：提示是“调用被结束”。
    if returncode in TERMINATED_RETURNCODES:
        failure = InteractiveError('call_terminated',
            label+'调用被结束；不自动重试、不换模型、不换后台。')
        failure.failure_class = 'permanent'
        failure.preserve_node_health = True
        return failure
    # 无人值守下被权限系统拒绝读文件/跑命令：优先于 receipt_missing。
    if any(marker in text for marker in PERMISSION_MARKERS):
        failure = InteractiveError('permission_denied', PERMISSION_MESSAGE)
        failure.failure_class = 'permission_denied'
        failure.preserve_node_health = True
        return failure
    # Do not return any excerpt, even when it looks harmless.
    code = message = None
    if any(s in text for s in ('cannot find module', 'unknown option', 'unrecognized option',
                                'unexpected argument', 'unknown command')):
        code, message = 'unsupported_entry', label+'入口无法启动或不支持适配参数；请检查 CLI 安装和节点类型。'
    elif any(s in text for s in ('permission denied', 'operation not permitted', '权限被拒', 'not permitted by sandbox')):
        code, message = 'permission_denied', label+'被系统拒绝执行；这不是上游故障，也不记成节点测试失败。'
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
        if code == 'permission_denied':
            failure.failure_class = 'permission_denied'
            failure.preserve_node_health = True
        else:
            failure.failure_class = 'permanent'
        return failure
    return None


def cli_failure(kind, label, model, returncode, stdout, stderr):
    from .resource_agy import InteractiveError
    from xiaoba_agentos.adapters import _is_transient_cli_error, _redact_error
    specific = specific_failure(kind, label, model, stdout, stderr, returncode=returncode)
    if specific:return specific
    if returncode == 0:
        return InteractiveError('receipt_missing', label+'本轮已结束，但没有写出合格的任务卡回执；不自动重试。')
    failure = InteractiveError('cli_failed', label+'调用异常退出，未完成任务卡回执；不自动重试、不换模型、不换后台。')
    # 卡 43：瞬态/永久判定也只看诊断文字；stdout 里的结构化事件（读过的源码、工具
    # 结果）不参与匹配。
    failure.failure_class = 'transient' if _is_transient_cli_error(
        _redact_error(diagnostic_text(stdout, stderr))) else 'permanent'
    failure.error_tail = error_tail(stdout, stderr) or None
    return failure
