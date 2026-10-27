#!/usr/bin/env python3
"""Run the extension in Xiaoba's existing monitor, with explicit source roots."""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PACKAGE = REPO / 'architecture-package-20261027'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--core-source', type=Path, default=PACKAGE / 'implementation/candidate-v1.6.5/src')
    parser.add_argument('--monitor-source', type=Path, default=PACKAGE / 'shared/工具/小八监看')
    parser.add_argument('--config', type=Path, help='现有 AgentOS 配置；本工具不修改它')
    parser.add_argument('--runtime', type=Path, default=HERE / '.runtime', help='无配置时的新建独立运行目录')
    parser.add_argument('--port', type=int, default=8800)
    parser.add_argument('--dsh-reviewer', help='已登记来源 ID；仅在明确指定时绑定真实 DSH 适配器')
    parser.add_argument('--dsh-agent', help='原配置内明确的 DSH Agent 名称')
    parser.add_argument('--inbox-offline', action='store_true')
    args = parser.parse_args()
    sys.path[:0] = [str(args.core_source.resolve()), str(args.monitor_source.resolve())]
    from xiaoba_agentos.config import AppConfig
    from xiaoba_agentos.store import EventStore
    from monitor_core import config, logs
    from monitor_core.web import App, Context, Route, Response
    from monitor_core.modules.system import SystemModule
    from monitor_core.modules.review import ReviewModule
    from xiaoba_review.store import ReviewStore, DecisionInbox
    from xiaoba_review.backends import BackendRegistry, NativeDsh
    from xiaoba_review.monitor import OpenReviewModule

    native_config = AppConfig.load(args.config) if args.config else None
    runtime = native_config.runtime_dir if native_config else args.runtime.resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    native = EventStore(native_config.database_path if native_config else runtime / 'agentos.sqlite3')
    native.initialize()
    store = ReviewStore(native, runtime / 'memory-outbox')
    backends = BackendRegistry(store)
    if args.dsh_reviewer or args.dsh_agent:
        if not (native_config and args.dsh_reviewer and args.dsh_agent):
            parser.error('真实 DSH 需要 --config、--dsh-reviewer 和 --dsh-agent，禁止隐式默认')
        with store.transaction() as db:
            source = store.source(db, args.dsh_reviewer)
        backends.register(args.dsh_reviewer, NativeDsh(native_config, args.dsh_agent, source['source_ref']))
    settings = config.load(runtime / 'unused-monitor.json', {'port': args.port,
        'page_file': str(args.monitor_source.resolve() / 'xiaoba-monitor.html'),
        'agentos_project': str(native_config.project_dir if native_config else runtime),
        'agentos_config': str(args.config.resolve()) if args.config else ''})
    settings['root'] = str(runtime)
    ctx = Context(settings, logs.setup(None), args.port)
    app = App(ctx)
    system = SystemModule(ctx)
    app.register(system)
    # Add a navigation entry without altering the archived monitor HTML.
    original_index = app.routes[('GET', '/')].handler
    def index(req):
        response = original_index(req)
        if response.code == 200:
            link = '<nav style="padding:12px"><a href="/open-review">DSH＋开放审查 · 任务卡 / 回执 / 裁决材料</a></nav>'
            response.body = response.body.replace(b'<body>', b'<body>' + link.encode(), 1)
            response.body = response.body.replace(b'</body>', b'<script src="/open-review-native.js"></script></body>', 1)
        return response
    app.routes[('GET', '/')].handler = index
    if native_config:
        # Retain native decisions through the existing guarded proxy; no new
        # approval command or trial-gate bypass is introduced here.
        state_file = runtime / 'review-center/backend-state.json'
        if state_file.is_file():
            backend_state = json.loads(state_file.read_text())
            if Path(backend_state['config_path']).resolve() != args.config.resolve():
                raise ValueError('运行中裁决后端配置与本次配置不一致，停止接入')
            settings['review']['port'] = int(backend_state['port'])
        app.register(ReviewModule(ctx, manage=False))
    extension = OpenReviewModule(ctx, store, DecisionInbox(not args.inbox_offline), backends)
    app.register(extension)
    httpd = app.server()
    # Start only the extension; native service lifecycle stays with its existing
    # installed monitor/launcher. The proxy can connect to that service if online.
    extension.start()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())
    print(json.dumps({'listening': 'http://127.0.0.1:%d/open-review' % args.port,
                      'database': str(native.path), 'dsh_adapter_registered': bool(args.dsh_reviewer),
                      'production_switch': False}), flush=True)
    try:
        httpd.serve_forever()
    finally:
        extension.stop()
        httpd.server_close()


if __name__ == '__main__':
    main()
