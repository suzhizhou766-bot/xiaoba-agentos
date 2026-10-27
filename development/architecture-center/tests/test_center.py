import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
core=load('core');native=load('native_release')

class Driver:
    def __init__(self,ok=True):self.calls=0;self.ok=ok
    def preflight(self,package):return {'ok':self.ok,'version':'v1.6.6','previous_version':'v1.6.4','expires_at':time.time()+300,'checks':[{'name':'isolated-fixture','ok':self.ok}]}
    def apply(self,package,ticket):
        self.calls+=1
        return {'ok':self.ok,'version':'v1.6.6','activated':self.ok,'read_back':{'ok':self.ok}}

class CenterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.repo=self.root/'repo';self.repo.mkdir()
        self.path='development/example/SPEC.md';p=self.repo/self.path;p.parent.mkdir(parents=True);p.write_text('# Original\n')
        self.catalog=self.root/'MODULES.json';self.catalog.write_text(json.dumps({'schema_version':1,'modules':[{'id':'example','title':'Example','spec_path':self.path,'editable_prefixes':['development/example/'],'nodes':['example']}]}))
        self.driver=Driver();self.center=core.Center(self.repo,self.root/'work',self.catalog,self.driver)
    def tearDown(self):self.tmp.cleanup()
    def create(self,**kw):
        return self.center.create('example','# Updated\n',self.center.module('example')['sha256'],'Make the change','v1.6.4','commit:fixture',**kw)
    def claim(self,c,actor='Agent A'):return self.center.claim(c['id'],actor,c['revision'])
    def update(self,c,operation,actor=None,**kw):return self.center.agent_update(c['id'],actor or c['lease']['actor'],c['lease']['id'],c['revision'],operation,**kw)
    def package(self,c):
        c=self.claim(c);p=Path(c['folder'])/'artifacts/package';p.mkdir(parents=True)
        for relative,body in {'FILES.sha256':'fixture','RELEASE.json':'{}','agentos/scripts/switch_release.py':'# fixture','module/SPEC.md':'# Updated\n'}.items():
            f=p/relative;f.parent.mkdir(parents=True,exist_ok=True);f.write_text(body)
        c=self.center.register_package(c['id'],c['lease']['actor'],c['lease']['id'],c['revision'],{self.path:'module/SPEC.md'})
        c=self.update(c,'complete',summary='Isolated test fixture; not a native accepted package')
        return self.center.validate(c['id'],c['revision'])
    def ready(self):
        c=self.package(self.create(target_version='v1.6.6'));return self.center.prepare_release(c['id'],c['revision'])
    def publish(self,a,**kw):return self.center.publish(a['change']['id'],a['revision'],a['confirmation'],kw.get('request_id','test-request'))
    def test_stages_without_active_write_and_records_diff(self):
        c=self.create();self.assertEqual((self.repo/self.path).read_text(),'# Original\n');self.assertIn('-# Original',c['diffs'][self.path]);self.assertNotIn('confirmation',c)
        for name in ['change.json','ACCESS.json','HANDOFF.md','events.jsonl']:self.assertTrue((Path(c['folder'])/name).is_file())
    def test_stale_spec_rejected(self):
        with self.assertRaises(core.Conflict):self.center.create('example','x','bad','goal','base','source')
    def test_module_scope_and_traversal_rejected(self):
        c=self.claim(self.create())
        for path in ['development/other/a.py','development/example/../../evil.py','development/example/.auth.json','development/example/auth.json']:
            with self.subTest(path=path),self.assertRaises(ValueError):self.update(c,'write',path=path,body='x')
    def test_staged_symlink_rejected(self):
        c=self.create();f=Path(c['folder'])/'files'/self.path;f.unlink();f.symlink_to(self.repo/self.path)
        with self.assertRaises(core.Conflict):self.center.validate(c['id'],c['revision'])
    def test_parent_module_cannot_overwrite_other_module_spec(self):
        self.center.modules['nested']={'spec_path':'development/example/nested/SPEC.md'}
        c=self.claim(self.create())
        with self.assertRaises(ValueError):self.update(c,'write',path='development/example/nested/SPEC.md',body='out of scope')
    def test_unregistered_file_rejected(self):
        c=self.create();(Path(c['folder'])/'files/extra.md').write_text('x')
        with self.assertRaises(core.Conflict):self.center.validate(c['id'],c['revision'])
    def test_source_and_staged_drift_rejected(self):
        c=self.create();(self.repo/self.path).write_text('source drift')
        with self.assertRaises(core.Conflict):self.center.validate(c['id'],c['revision'])
        (self.repo/self.path).write_text('# Original\n');(Path(c['folder'])/'files'/self.path).write_text('candidate drift')
        with self.assertRaises(core.Conflict):self.center.validate(c['id'],c['revision'])
    def test_lease_exclusion_and_wrong_revision(self):
        c=self.claim(self.create())
        with self.assertRaises(core.Conflict):self.center.claim(c['id'],'Agent B',c['revision'])
        with self.assertRaises(core.Conflict):self.center.agent_update(c['id'],'Agent B','wrong',c['revision'],'renew')
        with self.assertRaises(core.Conflict):self.center.validate(c['id'],c['revision']-1)
    def test_checkpoint_and_other_agent_continuation(self):
        c=self.claim(self.create());c=self.update(c,'checkpoint',summary='Implemented half',next_step='Finish integration');c=self.update(c,'complete',summary='Handing off')
        c=self.claim(c,'Agent B');self.assertEqual(c['next_step'],'Finish integration');self.assertEqual(c['lease']['actor'],'Agent B')
    def test_expired_lease_can_be_reclaimed_old_agent_cannot_write(self):
        c=self.claim(self.create());old=copy.deepcopy(c)
        with patch.object(core.time,'time',return_value=time.time()+1000):c=self.claim(c,'Agent B')
        with self.assertRaises(core.Conflict):self.update(old,'renew')
    def test_validation_is_only_syntax_and_manifest(self):
        c=self.create();c=self.center.validate(c['id'],c['revision']);self.assertTrue(c['validation']['ok']);self.assertEqual(c['validation']['scope'],'syntax_and_manifest_only');self.assertEqual(c['state'],'awaiting_package')
    def test_bad_python_prevents_passing_validation(self):
        c=self.claim(self.create());c=self.update(c,'write',path='development/example/bad.py',body='def broken(');c=self.update(c,'complete',summary='Bad fixture')
        c=self.center.validate(c['id'],c['revision']);self.assertFalse(c['validation']['ok'])
    def test_missing_package_or_driver_cannot_publish(self):
        c=self.create();c=self.center.validate(c['id'],c['revision'])
        with self.assertRaises(ValueError):self.center.prepare_release(c['id'],c['revision'])
        c=self.package(self.create());self.center.driver=None
        with self.assertRaises(ValueError):self.center.prepare_release(c['id'],c['revision'])
    def test_package_tamper_and_mapping_coverage(self):
        c=self.package(self.create());(Path(c['folder'])/'artifacts/package/RELEASE.json').write_text('changed')
        with self.assertRaises(core.Conflict):self.center.prepare_release(c['id'],c['revision'])
    def test_target_version_mismatch_blocks(self):
        c=self.package(self.create(target_version='v9.0.0'));a=self.center.prepare_release(c['id'],c['revision']);self.assertFalse(a['preflight']['ok']);self.assertIsNone(a['confirmation'])
    def test_success_records_once_and_idempotent_duplicate(self):
        a=self.ready();result=self.publish(a);self.assertTrue(result['ok']);self.assertEqual(self.publish(a),result);self.assertEqual(self.driver.calls,1)
        c=self.center.get(a['change']['id']);self.assertEqual(c['state'],'published');record=json.loads((self.root/'work/版本记录'/c['id']/'RELEASE-RECORD.json').read_text());self.assertTrue(record['published']);self.assertEqual(record['files'][self.path]['after_sha256'],core.sha(b'# Updated\n'))
    def test_nonce_and_request_reuse_conflict(self):
        a=self.ready()
        with self.assertRaises(core.Conflict):self.center.publish(a['change']['id'],a['revision'],'wrong','test-request')
        self.publish(a)
        with self.assertRaises(core.Conflict):self.center.publish(a['change']['id'],a['revision'],'different','test-request')
    def test_expired_preflight_cannot_apply(self):
        a=self.ready()
        with patch.object(core.time,'time',return_value=time.time()+400),self.assertRaises(core.Conflict):self.publish(a)
        self.assertEqual(self.driver.calls,0)
    def test_changes_invalidate_preflight_and_package(self):
        a=self.ready();c=self.claim(a['change']);c=self.update(c,'write',path=self.path,body='# Changed again\n');self.assertNotIn('package',c);self.assertNotIn('validation',c);self.assertNotIn('preflight',c)
        with self.assertRaises(core.Conflict):self.publish(a)
    def test_apply_failure_not_marked_published(self):
        a=self.ready();self.driver.ok=False;self.assertFalse(self.publish(a)['ok']);self.assertEqual(self.center.get(a['change']['id'])['state'],'release_failed')
    def test_audit_corruption_blocks(self):
        c=self.create()
        with self.center.tx() as db:db.execute("UPDATE events SET actor='tampered'")
        with self.assertRaises(core.Conflict):self.claim(c)
    def test_single_flight_across_changes(self):
        a=self.ready();b=self.ready();nested=[];original=self.driver.apply
        def apply(package,ticket):
            try:self.publish(b,request_id='second')
            except core.Conflict as exc:nested.append(str(exc))
            return original(package,ticket)
        self.driver.apply=apply;self.publish(a);self.assertEqual(self.driver.calls,1);self.assertTrue(nested)

class NativeBridgeTests(unittest.TestCase):
    def test_subprocess_protocol_and_actual_readback_required(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'switch.py';p.write_text("import json,sys\nassert sys.argv[2]=='--package'\nprint(json.dumps({'ok':True,'activated':True,'read_back':{'ok':True}}))\n")
            d=native.NativeRelease(p,core.sha(p.read_bytes()));self.assertTrue(d.apply(Path(folder),p)['ok'])
            p.write_text("import json\nprint(json.dumps({'ok':True}))\n");d=native.NativeRelease(p,core.sha(p.read_bytes()));self.assertFalse(d.apply(Path(folder),p)['ok'])
    def test_native_entry_drift_and_non_json_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'switch.py';p.write_text("print('not-json')\n");d=native.NativeRelease(p,core.sha(p.read_bytes()));self.assertFalse(d.preflight(Path(folder))['ok']);p.write_text('# changed')
            with self.assertRaises(ValueError):d.preflight(Path(folder))

if __name__=='__main__':unittest.main()
