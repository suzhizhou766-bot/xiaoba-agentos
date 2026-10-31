"""Release outcome unknown: block, then recover by recording a human read-back only.

Every driver here is an isolated fixture; no native script, real release or
production file is touched.
"""
import json
import os
import signal
from pathlib import Path
import unittest

import test_center as fixtures

core=fixtures.core


class UnknownDriver:
    def __init__(self,mode='manual'):
        self.mode=mode;self.calls=0
    def preflight(self,package):
        return {'ok':True,'version':'v1.6.6','previous_version':'v1.6.4','expires_at':core.time.time()+300,'checks':[{'name':'isolated-fixture','ok':True}]}
    def apply(self,package,ticket):
        self.calls+=1
        if self.mode=='raise':raise RuntimeError('隔离夹具：驱动未返回可确认结果')
        if self.mode=='crash':raise SystemExit('隔离夹具：模拟发版中途进程硬退出')
        if self.mode=='garbage':return 'not-a-result'
        if self.mode=='clean':return {'ok':False,'error':'隔离夹具：明确的干净失败'}
        if self.mode=='ok':return {'ok':True,'version':'v1.6.6','activated':True,'read_back':{'ok':True,'version':'v1.6.6'}}
        return {'ok':False,'error':'隔离夹具：原生回执缺失','requires_manual_readback':True}


class ReleaseUnknownTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.CenterTests();self.f.setUp()
        self.root=self.f.root;self.repo=self.f.repo;self.catalog=self.f.catalog;self.path=self.f.path
    def tearDown(self):self.f.tearDown()
    def start(self,mode='manual'):
        self.driver=UnknownDriver(mode)
        self.center=core.Center(self.repo,self.root/'work',self.catalog,self.driver)
        self.f.center=self.center
        return self.center
    def ready(self):return self.f.ready()
    def publish(self,a,request_id='unknown-request'):
        return self.center.publish(a['change']['id'],a['revision'],a['confirmation'],request_id)
    def state(self,cid):return self.center.get(cid)['state']

    # --- 1. 结果带 requires_manual_readback ---
    def test_manual_readback_enters_unknown_and_blocks_preflight_and_publish(self):
        self.start('manual');a=self.ready();result=self.publish(a)
        cid=a['change']['id']
        self.assertFalse(result['ok']);self.assertEqual(self.state(cid),'release_unknown')
        self.assertEqual(self.driver.calls,1)
        with self.assertRaises(core.Conflict) as caught:self.center.prepare_release(cid,self.center.get(cid)['revision'])
        self.assertIn(cid,str(caught.exception))
        with self.assertRaises(core.Conflict):self.center.publish(cid,self.center.get(cid)['revision'],'any','second-request')
        self.assertEqual(self.driver.calls,1)

    # --- 2. 驱动抛异常 ---
    def test_driver_exception_enters_unknown_and_blocks(self):
        self.start('raise');a=self.ready();self.publish(a);cid=a['change']['id']
        self.assertEqual(self.state(cid),'release_unknown');self.assertEqual(self.driver.calls,1)
        with self.assertRaises(core.Conflict):self.center.prepare_release(cid,self.center.get(cid)['revision'])
        with self.assertRaises(core.Conflict):self.center.publish(cid,self.center.get(cid)['revision'],'any','second-request')
        self.assertEqual(self.driver.calls,1)

    def test_unparseable_driver_result_is_unknown(self):
        self.start('garbage');a=self.ready();self.publish(a);cid=a['change']['id']
        self.assertEqual(self.state(cid),'release_unknown');self.assertEqual(self.driver.calls,1)

    # --- 3. 重启后发现遗留 publishing ---
    def test_restart_turns_leftover_publishing_into_unknown(self):
        self.start('crash');a=self.ready()
        with self.assertRaises(SystemExit):self.publish(a)
        cid=a['change']['id']
        self.assertEqual(self.state(cid),'publishing')
        self.start('manual')  # 新的 Center 实例＝重启
        c=self.center.get(cid)
        self.assertEqual(c['state'],'release_unknown')
        self.assertEqual(c['release_unknown']['reason'],'interrupted_publish')
        self.assertIn('release_unknown',[event['type'] for event in c['history']])
        self.assertEqual(self.driver.calls,0)
        with self.assertRaises(core.Conflict):self.center.prepare_release(cid,c['revision'])

    def test_second_center_does_not_recover_a_live_publisher(self):
        self.start('ok');a=self.ready();cid=a['change']['id']
        real_apply=self.driver.apply
        def apply(package,ticket):
            other=core.Center(self.repo,self.root/'work',self.catalog,UnknownDriver())
            self.assertEqual(other.get(cid)['state'],'publishing')
            self.assertNotIn('release_unknown',[e['type'] for e in other.get(cid)['history']])
            return real_apply(package,ticket)
        self.driver.apply=apply
        self.assertTrue(self.publish(a)['ok'])
        self.assertEqual(self.state(cid),'published')

    def test_sigkilled_lock_holder_is_recovered_by_a_new_process(self):
        self.start('ok');a=self.ready();cid=a['change']['id']
        def die(*args):os.kill(os.getpid(),signal.SIGKILL)
        self.driver.apply=die
        pid=os.fork()
        if pid==0:
            try:self.publish(a)
            except BaseException:os._exit(91)
            os._exit(92)
        _,status=os.waitpid(pid,0)
        self.assertTrue(os.WIFSIGNALED(status));self.assertEqual(os.WTERMSIG(status),signal.SIGKILL)
        self.assertEqual(self.state(cid),'publishing')
        self.start()
        self.assertEqual(self.state(cid),'release_unknown')

    def test_http_acknowledge_ignores_forged_actor(self):
        import test_monitor as http_fixture
        fixture=http_fixture.MonitorTests();fixture.setUp()
        try:
            fixture.center.driver=UnknownDriver('manual')
            a=fixture.fixture.ready();cid=a['change']['id']
            fixture.fixture.publish(a)
            revision=fixture.center.get(cid)['revision']
            status,body=fixture.request('/api/release-center/acknowledge',{
                'cid':cid,'expected_revision':revision,'conclusion':'not_applied',
                'evidence':'isolated readback','actor':'forged-admin'},
                {'X-Xiaoba-Token':fixture.ctx.page_token,'Origin':fixture.base})
            self.assertEqual(status,200,body)
            c=fixture.center.get(cid)
            self.assertEqual(c['release_recovery']['actor'],'monitor-user')
            event=next(e for e in c['history'] if e['type']=='release_unknown_acknowledged')
            self.assertEqual(event['actor'],'monitor-user')
        finally:fixture.tearDown()

    # --- 4. 恢复入口 ---
    def test_acknowledge_not_applied_allows_new_preflight_and_never_applies(self):
        self.start('manual');a=self.ready();self.publish(a);cid=a['change']['id'];calls=self.driver.calls
        c=self.center.acknowledge_release_unknown(cid,self.center.get(cid)['revision'],'not_applied','隔离夹具：活动指针仍是 v1.6.4，版本记录未更新')
        self.assertEqual(c['state'],'release_failed');self.assertEqual(self.driver.calls,calls)
        self.assertIn('release_unknown_acknowledged',[event['type'] for event in c['history']])
        again=self.center.prepare_release(cid,c['revision'])
        self.assertTrue(again['preflight']['ok']);self.assertTrue(again['confirmation'])
        self.center.driver.ok=True;self.driver.mode='ok'
        result=self.center.publish(cid,again['revision'],again['confirmation'],'after-recovery')
        self.assertTrue(result['ok']);self.assertEqual(self.state(cid),'published');self.assertEqual(self.driver.calls,calls+1)

    def test_acknowledge_applied_marks_published_without_applying(self):
        self.start('manual');a=self.ready();self.publish(a);cid=a['change']['id'];calls=self.driver.calls
        c=self.center.acknowledge_release_unknown(cid,self.center.get(cid)['revision'],'applied','隔离夹具：活动版本已回读为 v1.6.6，指纹一致')
        self.assertEqual(c['state'],'published');self.assertEqual(self.driver.calls,calls)
        record=json.loads((self.root/'work/版本记录'/cid/'RELEASE-RECORD.json').read_text())
        self.assertTrue(record['published']);self.assertEqual(record['verified_by'],'manual_readback')
        with self.assertRaises(core.Conflict):self.center.prepare_release(cid,c['revision'])

    def test_acknowledge_requires_evidence_and_valid_conclusion(self):
        self.start('manual');a=self.ready();self.publish(a);cid=a['change']['id'];c=self.center.get(cid)
        for conclusion,evidence in [('applied','   '),('not_applied',''),('maybe','有依据')]:
            with self.subTest(conclusion=conclusion),self.assertRaises(ValueError):
                self.center.acknowledge_release_unknown(cid,c['revision'],conclusion,evidence)
        self.assertEqual(self.state(cid),'release_unknown')

    def test_acknowledge_rejected_when_not_unknown(self):
        self.start('manual');a=self.ready();cid=a['change']['id']
        with self.assertRaises(core.Conflict):self.center.acknowledge_release_unknown(cid,self.center.get(cid)['revision'],'applied','隔离夹具')

    # --- 5. 工作区内任一未知状态都拒绝 ---
    def test_other_change_blocked_while_any_unknown_exists(self):
        self.start('manual')
        b=self.ready()  # 另一条候选先通过预检，再让第一条进入未知状态
        a=self.ready();self.publish(a);unknown=a['change']['id']
        self.driver.mode='ok'
        with self.assertRaises(core.Conflict) as caught:self.center.prepare_release(b['change']['id'],b['revision'])
        self.assertIn(unknown,str(caught.exception))
        with self.assertRaises(core.Conflict):self.center.publish(b['change']['id'],b['revision'],b['confirmation'],'blocked-by-unknown')
        self.assertEqual(self.driver.calls,1)

    # --- 6. 干净失败行为不变 ---
    def test_clean_failure_unchanged(self):
        self.start('clean');a=self.ready();result=self.publish(a);cid=a['change']['id']
        self.assertEqual(self.state(cid),'release_failed')
        self.assertNotEqual(self.state(cid),'release_unknown')
        again=self.center.prepare_release(cid,self.center.get(cid)['revision'])
        self.assertTrue(again['preflight']['ok'])
        self.driver.mode='ok'
        self.assertTrue(self.center.publish(cid,again['revision'],again['confirmation'],'clean-retry')['ok'])
        self.assertEqual(self.driver.calls,2)


if __name__=='__main__':unittest.main()
