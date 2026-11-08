"""Card 51: role selection, corrections and initial value admission."""
from datetime import datetime
import json
import subprocess
import time
import types
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import test_review as fx
from xiaoba_review.agent_pool import AgentPool
from xiaoba_review.backends import BackendRegistry


class Card51IngressTests(fx.FixtureMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.pool=AgentPool(self.store,BackendRegistry(self.store))

    def add_node(self,node,kind,model=None):
        source=self.pool.add({'node_id':node,'name':node,'agent':node,'kind':kind,
            'interface':'cli','entry':'/TEST/'+node,'model':model or 'TEST '+node,'agent_type':'other'})
        self.pool.change(node,source['node']['revision'],'toggle',True)
        return node

    def score(self,node,role='review',success=True):
        task=self.native.submit('TEST actual task',2)
        run=self.native.start_agent_run(task,node,{'purpose':role})
        self.native.finish_agent_run(run,'completed' if success else 'permanent_error',
            '审查结论：PASS\nTEST receipt' if success else None,None,1)
        self.native.complete_manually(task,'TEST settled')
        return task

    def prepare(self,kinds,**kw):
        # Four-field historical fixtures in FixtureMixin have no runnable entry.
        with patch.object(self.pool.runtime,'readiness',side_effect=lambda db,s:{'ready':bool(s.get('node'))}):
            return self.pool.runtime.prepare(kinds,**kw)

    def test_legacy_labels_are_not_exact_vendor_filters_and_both_uses_review_score(self):
        self.add_node('old-dsh','dsh');self.add_node('modern','review');self.add_node('new','both')
        for _ in range(4):self.score('modern')
        for _ in range(3):self.score('old-dsh')
        for _ in range(5):self.score('new','collaboration')
        result=self.prepare(['dsh','peer'])
        self.assertEqual('run',result['route'],result)
        self.assertEqual({'dsh':'modern','peer':'new'},result['selections'])
        result=self.prepare(['review','review_2'])
        self.assertEqual({'review':'modern','review_2':'new'},result['selections'])

    def test_collaboration_selects_by_its_role_and_reserves_second_seat(self):
        for node,kind in [('work','collaborate'),('both','both'),('review-only','review')]:self.add_node(node,kind)
        for _ in range(4):self.score('both','collaboration')
        for _ in range(9):self.score('work','review')
        result=self.prepare(['collaborate','collaborate_2'])
        self.assertEqual({'collaborate':'both','collaborate_2':'work'},result['selections'])

    def test_recent_dispatch_rotates_low_sample_nodes(self):
        for node in ['winner','new-a','new-b']:self.add_node(node,'review')
        for _ in range(3):self.score('winner')
        first=self.prepare(['review','review_2'])
        self.assertEqual('new-a',first['selections']['review_2'])
        task=self.native.submit('TEST assigned not settled',2)
        self.native.start_agent_run(task,'new-a',{'purpose':'review'})
        second=self.prepare(['review','review_2'])
        self.assertEqual('new-b',second['selections']['review_2'])

    def test_correction_preserves_role_week_and_one_sample_with_compare_and_swap(self):
        node=self.add_node('correct','both');task=self.score(node)
        scorer=self.pool.runtime.scoring;day=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        args=dict(node_id=node,day=day,task_id=task,task_type='ignored correction label',
            outcome='not_good',evidence_refs=['TEST corrected verified receipt'],expected_revision=1,
            assessment_id='TEST-correction',role='review')
        self.assertEqual(0,scorer.assess(**args)['points'])
        self.assertEqual(2,scorer.assess(**args)['revision'])
        row=scorer.weekly(day)['ranking'][0]
        self.assertEqual(('review',1,0),(row['role'],row['tasks'],row['success_rate']))
        with self.assertRaises(ValueError):scorer.assess(**dict(args,assessment_id='stale'))

    def unapproved(self):
        return self.native.create_authored_candidate('TEST new experience','TEST full draft to discard',
            self.root/'memory-outbox',author='TEST author',experience_kind='method')

    def rate(self,candidate,scores,duplicate=None):
        req=self.store.create(candidate['task_id'],'fixture-dsh','fixture-peer','TEST new value',
            [{'ref':'TEST evidence','sha256':'a'*64}],review_type='new_experience')
        for assignment,score in zip(req['assignments'],scores):
            received=self.store.receive(self.receipt(assignment,verdict='FAIL',score=score,duplicate_of=duplicate))
            self.store.attest(received['id'],received['digest'],'TEST actual source verified')
        self.store.deliver(self.inbox)
        return self.store.get(req['id'])

    def test_low_value_retires_unapproved_candidate_not_just_packet_field(self):
        c=self.unapproved();req=self.rate(c,[5,5])
        self.assertEqual('value_discarded',req['state'])
        current=self.native.get_memory_candidate(c['task_id'])
        self.assertEqual('retired',current['status'])
        from pathlib import Path
        self.assertNotIn('TEST full draft to discard',Path(current['staged_path']).read_text())
        self.assertNotIn(c['task_id'],{r['task_id'] for r in self.native.review_queue('delivery')})
        self.assertEqual(0,len(req['deliveries']))
        self.assertTrue(self.native.verify_event_chain()['ok'])

    def test_value_disagreement_and_duplicate_reach_initial_review_with_scores(self):
        for scores,duplicate,reason in [([6,6],None,'average'),([1,5],None,'disagreement'),([1,1],'TEST-known','duplicate')]:
            with self.subTest(reason=reason):
                c=self.unapproved();req=self.rate(c,scores,duplicate)
                self.assertEqual('submitted',req['state'])
                item=next(r for r in self.native.review_queue('delivery') if r['task_id']==c['task_id'])
                self.assertEqual(reason,item['value_assessment']['reason'])
                self.assertEqual(scores, list(item['value_assessment']['scores'].values()))

    def test_value_discard_failure_rolls_back_note_hash_candidate_and_event(self):
        c=self.unapproved()
        from pathlib import Path
        before=Path(c['staged_path']).read_bytes()
        with self.native.connect() as db:
            db.execute("CREATE TRIGGER fail_value BEFORE INSERT ON events WHEN NEW.event_type='open_review.value_discarded' BEGIN SELECT RAISE(ABORT,'TEST rollback'); END")
        with self.assertRaises(Exception):self.rate(c,[4,4])
        self.assertEqual(before,Path(c['staged_path']).read_bytes())
        current=self.native.get_memory_candidate(c['task_id'])
        self.assertEqual(c['evidence_sha256'],current['evidence_sha256'])
        self.assertEqual('pending_delivery_review',current['status'])


if __name__=='__main__':unittest.main()
