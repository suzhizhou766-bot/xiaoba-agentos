"""Real HTTP guards and a real-browser release flow against an isolated fixture.

The fixture driver does not call native production tools or alter a release.
"""
import importlib.util
import json
import logging
from pathlib import Path
import re
import sys
import threading
import unittest
import urllib.error
import urllib.request
import test_center as fixtures

ROOT=Path(__file__).resolve().parents[1]
REPO=ROOT.parents[1]
sys.path.insert(0,str(REPO/'architecture-package-20261027/shared/工具/小八监看'))
spec=importlib.util.spec_from_file_location('architecture_center_tests',ROOT/'__init__.py',submodule_search_locations=[str(ROOT)])
module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
from architecture_center_tests.monitor import ArchitectureCenterModule
from architecture_center_tests.core import Center
from monitor_core.web import App,Context
from monitor_core import config

class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.CenterTests();self.fixture.setUp();self.fixture.center=Center(self.fixture.repo,self.fixture.root/'work',self.fixture.catalog,self.fixture.driver);self.center=self.fixture.center
        settings=config.load(self.fixture.root/'no-config.json',{'port':0});settings['root']=str(self.fixture.root)
        self.ctx=Context(settings,logging.getLogger('architecture-test'),0);self.app=App(self.ctx)
        self.app.register(ArchitectureCenterModule(self.ctx,self.center));self.httpd=self.app.server();self.ctx.port=self.httpd.server_address[1]
        self.base='http://127.0.0.1:'+str(self.ctx.port);self.ctx.hosts={'127.0.0.1:'+str(self.ctx.port)};self.ctx.origins={self.base};self.thread=threading.Thread(target=self.httpd.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):self.httpd.shutdown();self.httpd.server_close();self.thread.join();self.fixture.tearDown()
    def request(self,path,payload=None,headers=None):
        headers=headers or {};data=None
        if payload is not None:data=json.dumps(payload).encode();headers.setdefault('Content-Type','application/json')
        req=urllib.request.Request(self.base+path,data=data,headers=headers)
        try:
            with urllib.request.urlopen(req) as response:return response.status,response.read()
        except urllib.error.HTTPError as error:return error.code,error.read()
    def test_page_and_meta_do_not_leak_publish_challenge(self):
        a=self.fixture.ready();status,body=self.request('/architecture/');self.assertEqual(status,200);self.assertIn(self.ctx.page_token.encode(),body)
        status,body=self.request('/api/release-center/meta');self.assertEqual(status,200);self.assertNotIn(a['confirmation'].encode(),body)
    def test_write_rejects_missing_token_and_cross_origin(self):
        self.assertEqual(self.request('/api/release-center/create',{})[0],403)
        self.assertEqual(self.request('/api/release-center/create',{}, {'X-Xiaoba-Token':self.ctx.page_token,'Origin':'http://evil.example'})[0],403)
    def test_real_post_cannot_publish_without_current_preflight(self):
        c=self.fixture.create();status,body=self.request('/api/release-center/publish',{'cid':c['id'],'expected_revision':c['revision'],'confirmation':'fake','request_id':'blocked'}, {'X-Xiaoba-Token':self.ctx.page_token,'Origin':self.base})
        self.assertEqual(status,409);self.assertEqual(self.fixture.driver.calls,0)
    def test_browser_user_button_applies_once_and_shows_receipt(self):
        try:from playwright.sync_api import sync_playwright,expect
        except ImportError:self.skipTest('Optional real browser dependency is unavailable')
        c=self.fixture.package(self.fixture.create(target_version='v1.6.6'))
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox']);page=browser.new_page(viewport={'width':1400,'height':1000});errors=[];page.on('pageerror',lambda exc:errors.append(str(exc)))
            page.goto(self.base+'/architecture/');page.get_by_role('button',name='待发布 / 版本记录',exact=True).click();page.locator('.upgrade-list>button').filter(has_text='Make the change').click()
            expect(page.get_by_role('button',name='正式发版',exact=True)).to_be_disabled();page.get_by_role('button',name='正式包预检',exact=True).click();expect(page.get_by_role('button',name='正式发版',exact=True)).to_be_enabled();page.get_by_role('button',name='正式发版',exact=True).click()
            dialog=page.locator('dialog').last;expect(dialog).to_be_visible();expect(dialog).to_contain_text('v1.6.6');dialog.get_by_role('button',name='确认正式发版',exact=True).click()
            expect(page.locator('.upgrade-item .badge')).to_have_text('已发布');self.assertEqual(self.fixture.driver.calls,1);self.assertEqual(self.center.get(c['id'])['state'],'published');self.assertFalse(errors);browser.close()

if __name__=='__main__':unittest.main()
