"""Card52b reuses established ingress identity/receipt contracts without schema changes."""
import hashlib
import json
import unittest
from test_review import FixtureMixin
from xiaoba_agentos.formal_review import submit_draft, items

class FormalIngressTests(FixtureMixin,unittest.TestCase):
    def revision(self):
        source=self.candidate['task_id'];path=self.root/'formal.md';path.write_text('TEST formal source')
        sha=hashlib.sha256(path.read_bytes()).hexdigest();real=self.native.submit('TEST real task',1)
        with self.native.connect() as db:
            db.execute("UPDATE memory_candidates SET status='published',published_path=?,published_at=1 WHERE task_id=?",(str(path),source))
            rid=self.native._append_event_tx(db,source,'memory.reuse_revision_requested',{'source_task_id':source,'source_sha256':sha,'real_task_id':real,'application_ids':['TEST-app'],'applications':[{'reuse_assessment':{'changed':'TEST step','added':'TEST boundary','discarded':''}}]})
        draft=self.root/'revision.md';draft.write_text('# TEST revised method\nTEST complete steps and limits')
        ref={'path':str(draft.resolve()),'sha256':hashlib.sha256(draft.read_bytes()).hexdigest()}
        return rid,ref

    def test_missing_body_never_creates_assignments(self):
        rid,ref=self.revision();before=self.count('ri_assignments')
        with self.assertRaises(ValueError):self.store.create(rid,'fixture-dsh','fixture-peer','TEST review',[{'ref':'TEST','sha256':ref['sha256']}],review_type='reuse_change')
        self.assertEqual(before,self.count('ri_assignments'))

    def test_revision_card_has_full_body_source_real_task_and_review_type(self):
        rid,ref=self.revision();submit_draft(self.native,rid,ref)
        req=self.store.create(rid,'fixture-dsh','fixture-peer','TEST review',[{'ref':ref['path'],'sha256':ref['sha256']}],review_type='reuse_change')
        for assignment in req['assignments']:
            self.assertEqual('reuse_change',assignment['card']['review_type'])
            self.assertEqual(ref['sha256'],assignment['card']['candidate_sha256'])
            self.assertIn('TEST complete steps',assignment['card']['body'])
            self.assertIn('source_sha256',assignment['card']);self.assertIn('real_task_id',assignment['card'])
        self.complete(req);self.store.deliver(self.inbox)
        self.assertEqual('pending',next(i for i in items(self.native) if i['task_id']==rid)['status'])
        self.assertEqual(0,self.count('memory_reviews')-1)

    def test_receipt_binding_and_schema_do_not_change(self):
        def schema():
            with self.native.read_only() as db:return [tuple(r) for r in db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name')]
        before=schema();rid,ref=self.revision();submit_draft(self.native,rid,ref)
        req=self.store.create(rid,'fixture-dsh','fixture-peer','TEST review',[{'ref':ref['path'],'sha256':ref['sha256']}],review_type='reuse_change')
        r=self.store.receive(self.receipt(req['assignments'][0],candidate_sha256='0'*64))
        self.assertEqual('invalid',r['status']);self.assertEqual(before,schema())

    def test_wrong_type_or_unbound_revision_evidence_rejected(self):
        rid,ref=self.revision();submit_draft(self.native,rid,ref)
        for review_type,sha in [('new_experience',ref['sha256']),('reuse_change','0'*64)]:
            with self.assertRaises(ValueError):self.store.create(rid,'fixture-dsh','fixture-peer','TEST review',[{'ref':'TEST','sha256':sha}],review_type=review_type)
