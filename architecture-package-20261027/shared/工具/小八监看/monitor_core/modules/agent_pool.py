"""Optional release-owned Agent UI; no review implementation is copied here."""
import hashlib
import json
from pathlib import Path
import sys

from .. import config as cfg
from ..web import Response, Route
from . import Module


def verified_source(settings):
    """Check all release bytes before importing its binding verifier."""
    config_path = cfg.agentos_config_path(settings)
    raw = json.loads(config_path.read_text(encoding='utf-8'))
    policy = raw.get('release', {})
    if policy.get('acceptance_mode'):
        raise ValueError('candidate release')
    index_path = Path(policy.get('index_path', ''))
    if not index_path.is_absolute() or index_path.is_symlink():
        raise ValueError('invalid release index')
    index = json.loads(index_path.read_text(encoding='utf-8'))
    version = policy.get('expected_version')
    entries = [e for e in index.get('releases', []) if e.get('version') == version]
    if not version or index.get('current_release') != version or len(entries) != 1:
        raise ValueError('release binding mismatch')
    entry = entries[0]
    relative = Path(entry.get('path', ''))
    if relative.is_absolute() or '..' in relative.parts or relative == Path('.'):
        raise ValueError('invalid release path')
    release = index_path.parent / relative
    if release.is_symlink() or not release.resolve().is_relative_to(index_path.parent.resolve()):
        raise ValueError('release escapes index')
    if entry.get('status') not in ('verified_immutable', 'verified_immutable_baseline'):
        raise ValueError('unverified release')
    manifest = release / 'FILES.sha256'
    if manifest.is_symlink() or hashlib.sha256(manifest.read_bytes()).hexdigest() != entry.get('files_manifest_sha256'):
        raise ValueError('manifest mismatch')
    recorded = {}
    for line in manifest.read_text().splitlines():
        digest, name = line.split('  ', 1)
        name = name.removeprefix('./')
        part = Path(name)
        if part.is_absolute() or '..' in part.parts or name in recorded:
            raise ValueError('invalid manifest path')
        recorded[name] = digest
    actual = set()
    for path in release.rglob('*'):
        if path.is_symlink():
            raise ValueError('release contains symlink')
        if path.is_file() and path != manifest:
            name = path.relative_to(release).as_posix()
            actual.add(name)
            if hashlib.sha256(path.read_bytes()).hexdigest() != recorded.get(name):
                raise ValueError('release file mismatch')
    if actual != set(recorded):
        raise ValueError('release file set mismatch')
    declaration = json.loads((release / 'RELEASE.json').read_text())
    if declaration.get('status') != 'verified_immutable_release' or declaration.get('version') != version:
        raise ValueError('release declaration mismatch')
    return release / 'agentos/src', config_path


class AgentPoolModule(Module):
    id, title, kind = 'agent-pool-loader', '后台 Agent 接入', 'service'

    def __init__(self, ctx):
        super().__init__(ctx)
        self.detail = '尚未加载'
        self.extension = None
        self.expected_absence = False

    def attach(self, app):
        before, old_path = set(sys.modules), list(sys.path)
        routes, prefixes = dict(app.routes), list(app.prefixes)
        modules, registry = list(app.modules), dict(app.ctx.modules)
        try:
            source, config_path = verified_source(self.ctx.config)
            if not (source / 'xiaoba_review/__init__.py').is_file():
                # 旧版本是正常状态，不能把监看中心总状态染成告警。
                self.detail = '活动版本未包含 xiaoba_review，保留原页面'
                self.expected_absence = True
                return
            for name, module in list(sys.modules.items()):
                if name.split('.')[0] in ('xiaoba_review', 'xiaoba_agentos'):
                    filename = getattr(module, '__file__', None)
                    if not filename or not Path(filename).resolve().is_relative_to(source.resolve()):
                        raise ValueError('another release is already imported')
            # Keep lazy imports read-only too: the release is immutable even
            # when monitor was started without Python's -B flag.
            sys.dont_write_bytecode = True
            sys.path.insert(0, str(source))
            from xiaoba_agentos.config import AppConfig
            from xiaoba_agentos.release import release_binding_status
            native_config = AppConfig.load(config_path)
            if native_config.project_dir.resolve() != Path(self.ctx.config['agentos_project']).resolve():
                raise ValueError('monitor/core project mismatch')
            binding = release_binding_status(native_config)
            if not binding.get('ok') or binding.get('skipped') or Path(binding['release_dir']).resolve() != source.parent.parent.resolve():
                raise ValueError('core release binding failed')
            from xiaoba_agentos.store import EventStore
            from xiaoba_review.store import ReviewStore, DecisionInbox
            from xiaoba_review.backends import BackendRegistry
            from xiaoba_review.resource_stdio import json_stdio_factory
            from xiaoba_review.monitor import OpenReviewModule
            from xiaoba_review import resource_tmux
            # Check UI before any database initialization or route registration.
            OpenReviewModule.mount_pool(Response(200, b'<body><div id="topo"></div></body>'), self.ctx.page_token)
            native = EventStore(native_config.database_path)
            native.workday_root = Path(native_config.memory['obsidian_vault']).expanduser().parent
            native.initialize()
            # 用户在 monitor.json 指定；没指定时 AGY CLI 和桌面文件通道不可用，其它功能照常。
            workspace_root = (self.ctx.config.get('agent_pool') or {}).get('workspace_root') or None
            # 服务进程的 PATH 里可能没有 tmux，用户在场通道也要能指定；活动版本还没有
            # 这个入口时照常加载，只在详情里写明配置没生效。
            configure_tmux = getattr(resource_tmux, 'configure_executable', None)
            tmux_path = (configure_tmux((self.ctx.config.get('agent_pool') or {}).get('tmux_path'))
                         if callable(configure_tmux) else None)
            store = ReviewStore(native, native_config.runtime_dir / 'memory-outbox', workspace_root)
            backends = BackendRegistry(store, resource_factory=lambda node: json_stdio_factory(node, workspace_root))
            extension = OpenReviewModule(self.ctx, store, DecisionInbox(), backends)
            app.register(extension)
            for key in (('GET', '/'), ('GET', '/index.html')):
                original = app.routes[key]
                def index(req, handler=original.handler):
                    response = extension.mount_pool(handler(req), self.ctx.page_token)
                    if response.code == 200:
                        # 裁决中心里的独立审查材料，和开发版 run.py 同一个脚本。
                        response.body = response.body.replace(b'</body>', b'<script src="/open-review-native.js"></script></body>', 1)
                    return response
                app.routes[key] = Route(original.method, original.path, index, original.guard)
            self.extension = extension
            self.detail = '已加载活动版本的 xiaoba_review' + ('' if workspace_root else '；未指定 agent_pool.workspace_root，AGY CLI 和桌面文件通道不可用') + ('' if tmux_path else '；未指定 agent_pool.tmux_path 或活动版本不支持，用户在场通道用 PATH 里的 tmux')
        except Exception as exc:
            app.routes, app.prefixes = routes, prefixes
            app.modules, app.ctx.modules = modules, registry
            self.detail = 'Agent 模块未注册：' + type(exc).__name__
            for name in set(sys.modules) - before:
                if name.split('.')[0] in ('xiaoba_review', 'xiaoba_agentos'):
                    sys.modules.pop(name, None)
        finally:
            sys.path[:] = old_path

    def health(self):
        return {'status': 'ok' if self.extension or self.expected_absence else 'warn', 'detail': self.detail}
