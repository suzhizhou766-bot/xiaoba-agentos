"""Card07 projections against the real EventStore, native settlement and revisions."""
import datetime
import json
import os
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

def _find_repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "development/integrated-candidate/src").is_dir():
            return parent
    return Path("/Users/suzhizhou/Documents/小八AgentOS/xiaoba-agentos")


ROOT = _find_repo_root()
CORE = ROOT / "development/integrated-candidate"
sys.path[:0] = [str(CORE / "src"), str(CORE / "tests"), str(ROOT / "development/review-ingress"), str(Path(__file__).resolve().parents[1])]
import test_card51_scoring as fixtures
from xiaoba_agentos import task_scores
from xiaoba_review.store import ReviewStore
from xiaoba_review.task_scoring import TaskScoring
from monitor_core.modules import overview as ov


class OverviewProjectionTests(unittest.TestCase):
    setUp = fixtures.Card51ScoringTests.setUp
    run_node = fixtures.Card51ScoringTests.run_node
    settle = fixtures.Card51ScoringTests.settle
    rows = fixtures.Card51ScoringTests.rows

    def view(self, function, **kwargs):
        with self.store.read_only() as db:
            before = db.total_changes
            result = function(db, **kwargs)
            self.assertEqual(before, db.total_changes)
            return result

    def correct(self, task, outcome):
        row = next(v for v in self.rows() if v['task_id'] == task)
        scoring = TaskScoring(SimpleNamespace(store=ReviewStore(self.store, self.root/'outbox')))
        return scoring.assess('A', row['date'], task, 'review', outcome,
            ['TEST verified correction'], 1, 'TEST-correct-'+task, role='review',
            void_reason='TEST defect' if outcome == 'void' else None)

    def test_empty_store_and_absent_schema_are_distinct(self):
        self.assertEqual([], self.view(ov.weekly_agent_model_scores)['items'])
        self.assertTrue(self.view(ov.weekly_agent_model_scores)['available'])
        db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        for function in (ov.today_ledger, ov.weekly_agent_model_scores,
                         ov.permanent_total_ranking, ov.b19_consecutive_failures, ov.dynamic_pool_nodes):
            self.assertFalse(function(db)['available'])

    def test_latest_revision_replaces_and_void_removes_both_outcomes(self):
        changed = self.settle()
        self.correct(changed, 'not_good')
        ranking = self.view(ov.weekly_agent_model_scores)['items']
        self.assertEqual((0, 1), (ranking[0]['points'], ranking[0]['tasks']))
        self.assertEqual(1, self.view(ov.b19_consecutive_failures)['items'][0]['consecutive'])
        for status in ('completed', 'permanent_error'):
            task = self.settle(status=status)
            self.correct(task, 'void')
        ranking = self.view(ov.weekly_agent_model_scores)['items']
        self.assertEqual((0, 1), (ranking[0]['points'], ranking[0]['tasks']))
        self.assertEqual(1, self.view(ov.b19_consecutive_failures)['items'][0]['total'])

    def test_b19_and_weekly_share_only_valid_latest_rows(self):
        for status in ('permanent_error','completed','permanent_error','permanent_error'):
            self.settle(status=status)
        result = self.view(ov.b19_consecutive_failures)['items'][0]
        self.assertEqual((2, 3, 4), (result['consecutive'], result['total_unqualified'], result['total']))
        self.assertEqual(1, self.view(ov.weekly_agent_model_scores)['items'][0]['points'])

    def test_broken_chain_disables_scores(self):
        self.settle()
        with self.store.connect() as db:
            db.execute("UPDATE events SET payload_json='{}' WHERE sequence=1")
        for function in (ov.weekly_agent_model_scores, ov.permanent_total_ranking, ov.b19_consecutive_failures):
            result = self.view(function)
            self.assertFalse(result['available']); self.assertEqual([], result['items'])
            self.assertTrue(result['problems'])

    def test_ledger_drift_isolates_only_dirty_sample(self):
        self.settle(); other = self.settle(status='permanent_error')
        with self.store.connect() as db:
            db.execute("UPDATE ri_task_assessments SET revision=99 WHERE json_extract(data,'$.task_id')=?", (other,))
        result = self.view(ov.weekly_agent_model_scores)
        self.assertEqual((1, 1), (result['items'][0]['points'], result['items'][0]['tasks']))
        self.assertTrue(result['available']); self.assertTrue(result['problems'])

    def test_malformed_event_and_invalid_points_never_reset_b19(self):
        self.settle(status='permanent_error')
        self.store.append_event(None, 'task_scoring.assessed', [])
        with self.store.connect() as db:
            # A syntactically valid, hash-bound event may still have bad semantics.
            value = dict(self.rows()[0], points=5, outcome='good', task_id='TEST-invalid')
            key='TEST-bad'; value.pop('id',None)
            task_scores.ensure_table(db)
            db.execute('INSERT INTO ri_task_assessments VALUES (?,?,?,?,?)',
                       (key, task_scores.canonical(value), 1, key, time.time()))
            self.store._append_event_tx(db, None, 'task_scoring.assessed',
                {'key':key,'revision':1,'assessment_id':key,'current':value,'previous':None,'delta':5})
        self.assertEqual(1, self.view(ov.b19_consecutive_failures)['items'][0]['consecutive'])
        self.assertTrue(self.view(ov.b19_consecutive_failures)['problems'])
        self.assertEqual(1, self.view(ov.weekly_agent_model_scores)['items'][0]['tasks'])

    def test_reuse_scores_require_task_used_at_and_scalar_objects(self):
        good={'source_task_id':'S1','real_task_id':'T1','score':1,'used_at':1.0}
        for value in (good, good, dict(good,real_task_id=''), dict(good,used_at='bad'),
                      dict(good,score=True), [], dict(good, real_task_id='T2', score=0)):
            self.store.append_event(None,'memory.reuse_scored',value)
        result=self.view(ov.permanent_total_ranking)
        self.assertEqual([{'source_task_id':'S1','score':1,'used':2}],result['items'])
        self.assertEqual(5,len(result['problems']))

    def test_today_ledger_uses_shanghai_even_with_utc_process(self):
        start=datetime.datetime(2026,11,17,tzinfo=ov.SHANGHAI_ZONE).timestamp()
        task=self.store.submit('TEST Shanghai boundary',2)
        outside=self.store.submit('TEST previous day',2)
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET created_at=? WHERE id=?',(start,task))
            db.execute('UPDATE tasks SET created_at=? WHERE id=?',(start-1,outside))
        with patch.dict(os.environ, {'TZ':'UTC'}):
            if hasattr(time,'tzset'):time.tzset()
            try:
                result=self.view(ov.today_ledger,day='2026-11-17')
            finally:
                pass
        if hasattr(time,'tzset'):time.tzset()
        self.assertEqual([task[:8]],[v['task_id'] for v in result['items']])

    def test_dynamic_state_reads_real_agent_pool_node_structure(self):
        from xiaoba_review.agent_pool import AgentPool
        from xiaoba_review.backends import BackendRegistry
        rs=ReviewStore(self.store,self.root/'outbox');pool=AgentPool(rs,BackendRegistry(rs))
        source=pool.add({'node_id':'TEST-new','name':'TEST node','agent':'TEST agent','kind':'review',
                         'interface':'cli','entry':'/TEST/c07','model':'TEST model','agent_type':'other'})
        pool.change('TEST-new',1,'toggle',True)
        source=pool.change('TEST-new',2,'remove')
        with self.store.connect() as db:
            raw=json.loads(db.execute("SELECT data FROM ri_sources WHERE id='B'").fetchone()[0])
            raw['node']['test']={'status':'failed'}
            db.execute("UPDATE ri_sources SET data=? WHERE id='B'",(json.dumps(raw),))
            db.execute("INSERT INTO ri_sources VALUES ('TEST-bad','[]')")
        result=self.view(ov.dynamic_pool_nodes)
        self.assertEqual((3,2),(result['enabled'],result['healthy']))
        item=next(v for v in result['nodes'] if v['node_id']=='TEST-new')
        self.assertTrue(item['removed']);self.assertFalse(item['enabled'])
        self.assertTrue(result['problems'])
