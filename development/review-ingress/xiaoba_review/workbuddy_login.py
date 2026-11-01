"""Explicit, attended native CLI login. Never used by automatic task dispatch.

Run with --login to request browser authentication in the CLI's own profile.
No desktop credentials, model discovery, userinfo API, or model prompt is used.
"""
import argparse
from pathlib import Path
import secrets
import socket
import time
from urllib.parse import urlsplit
import webbrowser

from .cli_identity import require_entry
from .resource_agy import InteractiveError
from .resource_workbuddy import AcpClient, WorkBuddyCliResource


def login(entry, directory, open_browser=webbrowser.open, announce=print):
    require_entry(entry)
    directory = Path(directory).resolve(strict=True)
    if not directory.is_dir(): raise ValueError('需要现有的空白登录工作目录')
    source = {'kind': 'peer', 'source_ref': 'workbuddy-cli-login',
        'reviewer_id': 'workbuddy-cli-login',
        'node': {'entry': entry, 'model': '', 'name': 'WorkBuddy CLI 登录'}}
    resource = WorkBuddyCliResource(source, directory)
    client = None
    opened = False
    def notification(message):
        nonlocal opened
        if message.get('method') != '_codebuddy.ai/authUrl': return
        params = message.get('params') or {}
        url = params.get('authUrl') if isinstance(params, dict) else None
        if not isinstance(url, str): raise ValueError('invalid native auth URL')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('invalid native auth URL')
        if not opened:
            if not open_browser(url, new=2): raise ValueError('cannot open native auth URL')
            opened = True
            announce('已打开原生认证页面，请在浏览器中完成登录；本工具不显示或保存凭据。')
    try:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            resource._port = sock.getsockname()[1]
        resource._password = secrets.token_urlsafe(32)
        resource._spawn(resource._command(entry, 300), directory)
        client = AcpClient(resource._port, resource._password, 300)
        deadline = time.monotonic() + resource.startup_seconds
        while True:
            try:
                client.request('/api/v1/health', timeout=1)
                break
            except OSError:
                if resource._process.poll() is not None or time.monotonic() >= deadline: raise
                time.sleep(.05)
        client.initialize(notification)
        # This is an explicit user action: native authenticate may reauthenticate
        # an existing CLI session. Automatic tests/tasks never invoke this method.
        announce('正在请求WorkBuddy AI原生CLI认证（Google/GitHub），不会调用模型。')
        client.rpc('authenticate', {'methodId': 'external'})
        if client.notification_error: raise client.notification_error
        announce('原生CLI已确认认证完成。请回到节点设置手动测试运行。')
    finally:
        resource.close()
        if client and client.notification_thread: client.notification_thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entry', required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--login', action='store_true', required=True,
        help='明确发起CLI独立认证；不读取或复制桌面登录材料')
    args = parser.parse_args()
    try: login(args.entry, args.workspace)
    except KeyboardInterrupt:
        print('认证已取消，已结束本工具拥有的引擎进程。')
        return 130
    except InteractiveError as exc:
        print(exc.user_message)
        return 1
    except Exception:
        print('原生CLI认证未完成；未保存原始错误或凭据，请核对浏览器和CLI安装。')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
