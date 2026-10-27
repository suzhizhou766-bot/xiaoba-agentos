"""Module for the existing Xiaoba monitoring HTTP kernel and its CSRF guards."""
import json
from pathlib import Path
import threading

from monitor_core.modules import Module
from monitor_core.web import Response, Route
from .store import Conflict


class OpenReviewModule(Module):
    id, title = 'open-review', 'DSH＋开放审查'

    def __init__(self, ctx, store, inbox, backends):
        super().__init__(ctx)
        self.store, self.inbox, self.backends = store, inbox, backends
        self.stopping = threading.Event()
        self.thread = None

    def routes(self):
        return [Route('GET', '/open-review', self.page, 'page'),
                Route('GET', '/open-review.js', self.script, 'page'),
                Route('GET', '/open-review-native.js', self.native_script, 'page'),
                Route('GET', '/api/open-review/state', self.state, 'read'),
                Route('POST', '/api/open-review/sources', self.sources, 'write'),
                Route('POST', '/api/open-review/requests', self.create, 'write'),
                Route('POST', '/api/open-review/receipts', self.receive, 'write'),
                Route('POST', '/api/open-review/attest', self.attest, 'write'),
                Route('POST', '/api/open-review/replace', self.replace, 'write'),
                Route('POST', '/api/open-review/run', self.run, 'write'),
                Route('POST', '/api/open-review/retry', self.retry, 'write')]

    def page(self, req):
        source = Path(__file__).with_name('page.html').read_text()
        source = source.replace('__TOKEN__', self.ctx.page_token)
        return Response(200, source.encode(), 'text/html; charset=utf-8', csp="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")

    def script(self, req):
        return Response(200, Path(__file__).with_name('page.js').read_bytes(), 'text/javascript; charset=utf-8')

    def native_script(self, req):
        return Response(200, Path(__file__).with_name('native-center.js').read_bytes(), 'text/javascript; charset=utf-8')

    def state(self, req):
        return 200, {'ok': True, 'inbox_online': self.inbox.online, **self.store.state()}

    def perform(self, action):
        try:
            value = action()
            self.store.deliver(self.inbox)
            return 200, {'ok': True, 'result': value}
        except Conflict as exc:
            return 409, {'ok': False, 'error': str(exc)}
        except (ValueError, KeyError, TypeError) as exc:
            return 400, {'ok': False, 'error': str(exc)}

    def sources(self, req):
        return self.perform(lambda: self.store.register_source(req.json()))

    def create(self, req):
        return self.perform(lambda: self.store.create(**req.json()))

    def receive(self, req):
        return self.perform(lambda: self.store.receive(req.json()))

    def attest(self, req):
        return self.perform(lambda: self.store.attest(**req.json()))

    def replace(self, req):
        return self.perform(lambda: self.store.replace_peer(**req.json()))

    def run(self, req):
        # Synchronous HTTP call; native DSH retains its progress/deadline logic.
        # No automatic retry or replacement on authentication/upstream failures.
        return self.perform(lambda: self.backends.execute(req.json()['assignment_id']))

    def retry(self, req):
        req.json()
        return self.perform(lambda: {'state': 'outbox_checked'})

    def start(self):
        def loop():
            while not self.stopping.wait(2):
                try:
                    # Revalidate against the original card before every delivery.
                    self.store.state()
                    self.store.deliver(self.inbox)
                except Exception:
                    self.ctx.log(self.id, '材料归集待重试', level='warning')
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=5)

    def health(self):
        return {'status': 'ok' if self.inbox.online else 'warn',
                'detail': '裁决材料收件箱在线；经验批准沿用原入口' if self.inbox.online else '收件箱离线，保留待投递材料'}
