"""Same-origin monitor integration. Hosted architecture previews stay read-only."""
import json
from pathlib import Path
import threading

from monitor_core.modules import Module
from monitor_core.web import Route, Response
from .core import Conflict


class ArchitectureCenterModule(Module):
    id,title='architecture-center','架构与升级发布'

    def __init__(self,ctx,center):
        super().__init__(ctx);self.center=center;self.assets=Path(__file__).with_name('web')

    def routes(self):
        routes=[Route('GET','/architecture',lambda r:Response(302,b'',headers={'Location':'/architecture/'}),'page'),
            Route('GET','/api/release-center/meta',self.meta,'read'),
            Route('GET','/api/release-center/module',self.module,'read'),
            Route('GET','/api/release-center/change',self.change,'read'),
            Route('POST','/api/release-center/create',self.create,'write'),
            Route('POST','/api/release-center/validate',self.validate,'write'),
            Route('POST','/api/release-center/preflight',self.preflight,'write'),
            Route('POST','/api/release-center/publish',self.publish,'write'),
            Route('POST','/api/release-center/acknowledge',self.acknowledge,'write')]
        for name in ('index.html','style.css','data.js','app.js','editor.js','editor.css','native-center.js'):
            url='/architecture/' if name=='index.html' else '/architecture/'+name
            routes.append(Route('GET',url,lambda req,n=name:self.asset(n),'page'))
        return routes

    def asset(self,name):
        content=(self.assets/name).read_bytes()
        if name=='index.html':content=content.replace(b'</head>',('<meta name="upgrade-page-token" content="'+self.ctx.page_token+'"></head>').encode())
        kind='text/html' if name.endswith('.html') else 'text/css' if name.endswith('.css') else 'text/javascript'
        return Response(200,content,kind+'; charset=utf-8',csp="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")

    def action(self,fn):
        try:return 200,{'ok':True,'result':fn()}
        except Conflict as exc:return 409,{'ok':False,'error':str(exc)}
        except (ValueError,KeyError,TypeError,OSError) as exc:return 400,{'ok':False,'error':str(exc)}

    def meta(self,req):return 200,{'ok':True,**self.center.state()}
    def module(self,req):return self.action(lambda:self.center.module(req.param('id')))
    def change(self,req):return self.action(lambda:self.center.get(req.param('id')))
    def create(self,req):return self.action(lambda:self.center.create(**req.json()))
    def validate(self,req):return self.action(lambda:self.center.validate(**req.json()))
    def preflight(self,req):return self.action(lambda:self.center.prepare_release(**req.json()))
    def publish(self,req):return self.action(lambda:self.center.publish(**req.json()))
    def acknowledge(self,req):
        def record():
            payload=dict(req.json());payload.pop('actor',None)
            return self.center.acknowledge_release_unknown(**payload,actor='monitor-user')
        return self.action(record)

    def health(self):
        return {'status':'ok','detail':'模块编辑与待发布工作区可用；原生发版执行器'+('已接入' if self.center.driver else '未配置')}
