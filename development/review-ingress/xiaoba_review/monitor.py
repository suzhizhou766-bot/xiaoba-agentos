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
        from .agent_pool import AgentPool
        self.pool = AgentPool(store,backends)
        self.stopping = threading.Event()
        self.thread = None

    def routes(self):
        return [Route('GET', '/agent-pool.js', self.pool_script, 'page'),
                Route('GET', '/agent-pool.css', self.pool_style, 'page'),
                Route('GET', '/agent-pool-native.js', self.pool_native, 'page'),
                Route('GET', '/api/agent-pool/state', lambda req: (200,self.pool.state()), 'read'),
                Route('POST', '/api/agent-pool/preview', self.terminal_preview, 'write'),
                Route('POST', '/api/agent-pool/nodes', lambda req: self.perform(lambda: self.pool.add(req.json())), 'write'),
                Route('POST', '/api/agent-pool/toggle', lambda req: self.perform(lambda: self.pool.change(**req.json(),operation='toggle')), 'write'),
                Route('POST', '/api/agent-pool/remove', lambda req: self.perform(lambda: self.pool.change(**req.json(),operation='remove')), 'write'),
                Route('POST', '/api/agent-pool/restore', lambda req: self.perform(lambda: self.pool.change(**req.json(),operation='restore')), 'write'),
                Route('POST', '/api/agent-pool/onboard', lambda req: self.perform(lambda: self.pool.runtime.onboard(**req.json())), 'write'),
                Route('POST', '/api/agent-pool/prepare', lambda req: self.perform(lambda: self.pool.runtime.prepare(**req.json())), 'write'),
                Route('POST', '/api/agent-pool/test', lambda req: self.perform(lambda: self.pool.runtime.test_run(**req.json())), 'write'),
                Route('POST', '/api/agent-pool/workday-start', lambda req: self.perform(lambda: self.pool.runtime.start_workday(**req.json())), 'write'),
                Route('POST', '/api/agent-pool/work', lambda req: self.perform(lambda: self.pool.runtime.execute_work(**req.json())), 'write'),
                Route('POST', '/api/agent-pool/workday-inputs', lambda req: self.perform(lambda: self.pool.runtime.workday_inputs(**req.json())), 'write'),
                Route('POST', '/api/agent-pool/task-assess', lambda req: self.perform(lambda: self.pool.runtime.scoring.assess(**req.json())), 'write'),
                Route('POST', '/api/agent-pool/weekly-scores', lambda req: self.perform(lambda: self.pool.runtime.scoring.weekly(**req.json())), 'write'),
                Route('POST', '/api/agent-pool/interactive-confirm', lambda req: self.perform(lambda: self.pool.runtime.confirm_interactive_workspace(**req.json())), 'write'),
                Route('POST', '/api/agent-pool/dispatch', self.dispatch, 'write'),
                Route('GET', '/open-review', self.page, 'page'),
                Route('GET', '/open-review.js', self.script, 'page'),
                Route('GET', '/open-review-native.js', self.native_script, 'page'),
                Route('GET', '/api/open-review/state', self.state, 'read'),
                Route('POST', '/api/open-review/sources', self.sources, 'write'),
                Route('POST', '/api/open-review/requests', self.create, 'write'),
                Route('POST', '/api/open-review/receipts', self.receive, 'write'),
                Route('POST', '/api/open-review/attest', self.attest, 'write'),
                Route('POST', '/api/open-review/replace', self.replace, 'write'),
                Route('POST', '/api/open-review/run', self.run, 'write'),
                Route('POST', '/api/open-review/retry', self.retry, 'write'),
                Route('POST', '/api/open-review/validation/resume', self.validation_resume, 'write'),
                Route('POST', '/api/open-review/validation/diff', self.validation_diff, 'write')]

    def page(self, req):
        source = Path(__file__).with_name('page.html').read_text()
        source = source.replace('__TOKEN__', self.ctx.page_token)
        return Response(200, source.encode(), 'text/html; charset=utf-8', csp="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")

    def script(self, req):
        return Response(200, Path(__file__).with_name('page.js').read_bytes(), 'text/javascript; charset=utf-8')

    @staticmethod
    def mount_pool(response, page_token):
        if response.code == 200 and b'id="topo"' in response.body:
            fragment = Path(__file__).with_name('agent-pool.html').read_bytes().replace(b'__TOKEN__',page_token.encode())
            response.body = response.body.replace(b'</body>',
                b'<link rel="stylesheet" href="/agent-pool.css">' + fragment +
                b'<script src="/agent-pool.js"></script><script src="/agent-pool-native.js"></script></body>',1)
        return response

    def pool_script(self, req):
        return Response(200,Path(__file__).with_name('agent-pool.js').read_bytes(),'text/javascript; charset=utf-8')

    def terminal_preview(self, req):
        data = req.json()
        if set(data) != {'node_id'}:raise ValueError('终端预览只接受节点ID，不接受输入或命令')
        return 200, {'ok':True,'preview':self.pool.runtime.preview(data['node_id'])}

    def pool_style(self, req):
        return Response(200,Path(__file__).with_name('agent-pool.css').read_bytes(),'text/css; charset=utf-8')

    def pool_native(self, req):
        return Response(200,Path(__file__).with_name('agent-pool-native.js').read_bytes(),'text/javascript; charset=utf-8')

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
        data=req.json()
        def action():
            if 'contract_version' in data:
                raise ValueError('接口不再接受 contract_version；第 1 版只留给已存在的任务卡')
            task_type=data.pop('task_type',None)
            if not data.get('dsh_id') or not data.get('peer_id'):
                prepared=self.pool.runtime.prepare(['dsh','peer'],{k:data[k+'_id'] for k in ('dsh','peer') if data.get(k+'_id')},task_type)
                if prepared['route']!='run':return prepared
                for kind,node_id in prepared['selections'].items():data[kind+'_id']=node_id
            review_type = data.get('review_type')
            if not review_type:
                raise ValueError('创建审查任务卡必须指定审查类型 (review_type)')
            if review_type not in {'new_experience', 'revalidation', 'reuse_change'}:
                raise ValueError('未知审查类型')
            return self.store.create(**data)
        return self.perform(action)

    def dispatch(self, req):
        data=req.json()
        def action():
            if 'contract_version' in data:
                raise ValueError('接口不再接受 contract_version；第 1 版只留给已存在的任务卡')
            if set(data)-{'candidate_id','scope','evidence','request_id','selections','task_type','review_type','duplicate_of'} or not data.get('request_id'):
                raise ValueError('直接运行需要稳定任务请求ID、候选、范围和证据')
            review_type = data.get('review_type')
            if not review_type:
                raise ValueError('派工必须指定审查类型 (review_type)')
            if review_type not in {'new_experience', 'revalidation', 'reuse_change'}:
                raise ValueError('未知审查类型')
            prepared=self.pool.runtime.prepare(['dsh','peer'],data.get('selections'),data.get('task_type'))
            if prepared['route']!='run':return prepared
            selected=prepared['selections']
            request=self.store.create(data['candidate_id'],selected['dsh'],selected['peer'],data['scope'],data['evidence'],
                request_id=data['request_id'],dsh_channel='backend_adapter',peer_channel='backend_adapter',
                review_type=review_type, duplicate_of=data.get('duplicate_of'))
            # Existing successful receipts are retained on exact request retries.
            for assignment in request['assignments']:
                if any(r['assignment_id']==assignment['id'] and r['status']=='valid' for r in request['receipts']):continue
                self.backends.execute(assignment['id'],require_test=True)
            return {'route':'completed','questions':[],'request':self.store.get(request['id'])}
        return self.perform(action)

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

    def validation_resume(self, req):
        def action():
            from xiaoba_agentos.experience_workflow import resume_paused_validation
            with self.store.native.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                return resume_paused_validation(self.store.native, db, req.json()['candidate_id'])
        return self.perform(action)

    def validation_diff(self, req):
        data = req.json()
        return self.perform(lambda: self.store.native.submit_validation_diff_materials(data['candidate_id'], data['materials']))

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
        self.backends.close()
        if self.thread:
            self.thread.join(timeout=5)

    def health(self):
        return {'status': 'ok' if self.inbox.online else 'warn',
                'detail': '裁决材料收件箱在线；经验批准沿用原入口' if self.inbox.online else '收件箱离线，保留待投递材料'}
