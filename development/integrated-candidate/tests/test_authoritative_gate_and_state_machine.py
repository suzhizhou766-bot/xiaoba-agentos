"""Authoritative validation: real Seatbelt execution; synthetic independent reviewers."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from fixtures_review import _config, _run_cli
from fixtures_validation import native_trial, make_execution, bound_diff
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.experience_workflow import (
    verify_final_qualification, advance_validation_round, resume_paused_validation,
    reserve_validation_attempt, pause_validation_attempt, migrate_legacy_validation_records,
    complete_audit, prepare_trial,
)
from xiaoba_agentos.validation_migration import apply_validation_schema_migration

class AuthoritativeGateAndStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve(); self.cfg=_config(self.root)
        self.store=EventStore(self.cfg.database_path); self.store.initialize(); self.store.apply_validation_extension()
        self.outbox=self.cfg.runtime_dir/'memory-outbox'

    def create_candidate(self):
        c=self.store.create_authored_candidate('TEST方法','仅用于测试的实现步骤',self.outbox,author='test',experience_kind='method')
        self.store.review_memory(c['task_id'],'delivery','approve','TEST初审',c['evidence_sha256'])
        return c

    def gate(self,c):
        with self.store.read_only() as db: return verify_final_qualification(db,c['task_id'])

    def advance(self,c,value):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE'); return advance_validation_round(self.store,db,c['task_id'],value)

    def resume(self,c):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE'); return resume_paused_validation(self.store,db,c['task_id'])

    def two_rounds(self):
        c=self.create_candidate(); first=native_trial(self.store,c,False); second=native_trial(self.store,c)
        return c,first,second

    def test_round1_pass_advances_to_pending_final_review(self):
        c=self.create_candidate(); native_trial(self.store,c)
        self.assertTrue(self.gate(c)['qualified']); self.assertEqual(self.store.get_validation_lineage(c['task_id'])['rounds_used'],1)
        self.assertEqual(self.store.get_memory_candidate(c['task_id'])['status'],'pending_final_review')

    def test_round1_fail_advances_to_revalidation_required(self):
        c=self.create_candidate(); native_trial(self.store,c,False)
        self.assertFalse(self.gate(c)['qualified']); self.assertEqual(self.store.get_memory_candidate(c['task_id'])['status'],'revalidation_required')

    def test_round2_pass_with_valid_diff_advances_to_pending_final(self):
        c,_,_=self.two_rounds(); materials=bound_diff(self.store,c['task_id'])
        self.store.submit_validation_diff_materials(c['task_id'],materials)
        self.assertTrue(self.gate(c)['qualified']); self.assertEqual(self.gate(c)['rounds_used'],2)
        self.store.submit_validation_diff_materials(c['task_id'],materials)
        self.assertEqual(len(self.store.get_validation_records(c['task_id'])),2)

    def test_round2_pass_without_valid_diff_blocks_qualification_without_retiring(self):
        c,_,_=self.two_rounds(); self.assertEqual(self.gate(c)['state'],'diff_materials_required')
        with self.assertRaises(ValueError): self.store.submit_validation_diff_materials(c['task_id'],{'remediation':'关键词拼凑整改'})
        self.assertEqual(self.store.get_validation_lineage(c['task_id'])['rounds_used'],2)
        self.assertEqual(self.store.get_memory_candidate(c['task_id'])['status'],'revalidation_required')

    def test_round2_fail_retires_candidate(self):
        c=self.create_candidate(); native_trial(self.store,c,False); native_trial(self.store,c,False)
        self.assertEqual(self.store.get_memory_candidate(c['task_id'])['status'],'retired'); self.assertFalse(self.gate(c)['qualified'])

    def test_no_third_round_allowed(self):
        c=self.create_candidate(); native_trial(self.store,c,False); native_trial(self.store,c,False)
        card,_=make_execution(self.cfg.runtime_dir)
        with self.assertRaises(ValueError): reserve_validation_attempt(self.store,c['task_id'],card)
        self.assertEqual(self.store.get_validation_lineage(c['task_id'])['rounds_used'],2)

    def test_external_pause_failure_preserves_round_without_consuming(self):
        c=self.create_candidate(); self.advance(c,{'failure_class':'tool_unavailable','pause_reason':'TEST工具不可用'})
        self.assertEqual(self.gate(c)['state'],'validation_paused'); self.assertEqual(self.store.get_validation_lineage(c['task_id'])['rounds_used'],0)
        self.assertEqual(self.resume(c)['resumed_round'],1); native_trial(self.store,c); self.assertTrue(self.gate(c)['qualified'])

    def test_old_failure_replay_after_round_two_pause_and_restart_is_noop(self):
        c=self.create_candidate(); old=native_trial(self.store,c,False)
        self.advance(c,{'failure_class':'tool_unavailable'})
        self.assertEqual(self.resume(c)['resumed_round'],2)
        self.store=EventStore(self.cfg.database_path)
        before=self.store.get_validation_lineage(c['task_id'])
        self.assertTrue(self.advance(c,old)['idempotent_replay'])
        self.assertEqual(self.store.get_validation_lineage(c['task_id']),before)
        native_trial(self.store,c); self.assertEqual(self.store.get_validation_lineage(c['task_id'])['rounds_used'],2)

    def test_terminal_replay_is_noop_after_retirement(self):
        c=self.create_candidate(); first=native_trial(self.store,c,False); native_trial(self.store,c,False)
        self.assertTrue(self.advance(c,first)['idempotent_replay']); self.assertEqual(self.store.get_memory_candidate(c['task_id'])['status'],'retired')

    def test_content_drift_invalidates_prior_pass_evidence(self):
        c=self.create_candidate(); native_trial(self.store,c); Path(c['staged_path']).write_text('未审查修改')
        self.assertEqual(self.gate(c)['state'],'content_drift')

    def test_substantive_fail_or_reasoning_leak_blocks_qualification(self):
        for output in ('<think>内部思考</think>\nTRIAL_VALIDATION: PASS','TRIAL_VALIDATION: PASS\nTRIAL_VALIDATION: FAIL'):
            c=self.create_candidate(); native_trial(self.store,c,output=output)
            self.assertFalse(self.gate(c)['qualified'])

    def test_final_review_requires_independent_user_action(self):
        c=self.create_candidate(); native_trial(self.store,c)
        with self.store.read_only() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM memory_reviews WHERE task_id=? AND stage='final'",(c['task_id'],)).fetchone()[0],0)
        self.assertIsNone(self.store.get_memory_candidate(c['task_id'])['published_path'])

    def test_human_final_rejection_cannot_be_rewritten_by_materials(self):
        c,_,_=self.two_rounds(); materials=bound_diff(self.store,c['task_id'])
        self.store.submit_validation_diff_materials(c['task_id'],materials)
        self.store.review_memory(c['task_id'],'final','reject','TEST拒绝',c['evidence_sha256'])
        with self.assertRaises(ValueError): self.store.submit_validation_diff_materials(c['task_id'],materials)
        with self.assertRaises(ValueError): reserve_validation_attempt(self.store,c['task_id'],{})
        self.assertEqual(self.store.get_memory_candidate(c['task_id'])['status'],'final_rejected'); self.assertFalse(self.gate(c)['qualified'])

    def test_fabricated_diff_ids_or_hashes_rejected(self):
        c,_,_=self.two_rounds(); materials=bound_diff(self.store,c['task_id'])
        materials['evidence_changes']['round_two']['execution_digest']='0'*64
        with self.assertRaises(ValueError): self.store.submit_validation_diff_materials(c['task_id'],materials)
        self.assertFalse(self.gate(c)['qualified'])

    def test_diff_submission_rechecks_revoked_review(self):
        c,_,second=self.two_rounds(); r=self.store.start_agent_run(second['task_id'],'dsh_review')
        self.store.finish_agent_run(r,'completed','TRIAL_VALIDATION: FAIL',None,1)
        with self.assertRaises(ValueError): self.store.submit_validation_diff_materials(c['task_id'],bound_diff(self.store,c['task_id']))

    def test_pending_attempt_blocks_concurrent_and_queued_reexecution(self):
        c=self.create_candidate(); card,_=make_execution(self.cfg.runtime_dir)
        def reserve(_):
            try: return reserve_validation_attempt(self.store,c['task_id'],card)
            except ValueError: return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(reserve,range(4)))
        self.assertEqual(sum(bool(x) for x in results),1)
        self.assertEqual(self.store.get_validation_lineage(c['task_id'])['rounds_used'],0)
        for _ in range(2):
            with self.assertRaises(ValueError): reserve_validation_attempt(self.store,c['task_id'],card)

    def test_paused_bound_task_resumes_original_without_new_execution(self):
        c=self.create_candidate(); task=native_trial(self.store,c,settle=False)
        rec=self.store.get_validation_records(c['task_id'])[0]
        pause_validation_attempt(self.store,c['task_id'],rec['id'],'TEST暂停')
        result=self.resume(c); self.assertEqual(result['validation_task_id'],task['task_id'])
        with self.assertRaises(ValueError): reserve_validation_attempt(self.store,c['task_id'],task['trial']['card'])
        complete_audit(self.store,task['task_id'],'TEST恢复后结算'); self.assertTrue(self.gate(c)['qualified'])

    def test_missing_task_or_wrong_execution_binding_cannot_pass(self):
        c=self.create_candidate()
        for data in ({'task_id':'不存在','plan_digest':'a'*64,'execution_digest':'b'*64},):
            with self.assertRaises(ValueError): self.advance(c,data)
        task=native_trial(self.store,c)
        with self.assertRaises(ValueError): self.advance(c,dict(task,execution_digest='0'*64))

    def test_missing_controller_receipt_revokes_qualification(self):
        c=self.create_candidate(); task=native_trial(self.store,c)
        from xiaoba_agentos.experience_workflow import digest
        value={'source_task_id':c['task_id'],'card':task['trial']['card'],'execution':task['execution']}
        path=self.cfg.runtime_dir/'trial-receipts'/(digest(value)+'.json')
        path.write_text('{}'); self.assertFalse(self.gate(c)['qualified'])

    def test_latest_native_failure_invalidates_pass(self):
        c=self.create_candidate(); task=native_trial(self.store,c)
        run=self.store.start_agent_run(task['task_id'],'dsh_review'); self.store.finish_agent_run(run,'completed','TRIAL_VALIDATION: FAIL',None,1)
        self.assertFalse(self.gate(c)['qualified'])

    def test_event_chain_damage_blocks_reservation(self):
        c=self.create_candidate()
        with self.store.connect() as db: db.execute("UPDATE events SET payload_json='{}' WHERE sequence=(SELECT min(sequence) FROM events)")
        with self.assertRaisesRegex(ValueError,'事件链'): reserve_validation_attempt(self.store,c['task_id'],{})
        self.assertIsNone(self.store.get_validation_lineage(c['task_id']))

    def test_legacy_records_migration_flags_ambiguous_data(self):
        c=self.create_candidate(); self.store.append_event(c['task_id'],'memory.trial_linked',{'task_id':'旧1'}); self.store.append_event(c['task_id'],'memory.trial_linked',{'task_id':'旧2'})
        with self.store.connect() as db: result=migrate_legacy_validation_records(db)
        self.assertEqual(result['flagged_for_human_check'],1); self.assertEqual(self.gate(c)['state'],'awaiting_check')

    def test_legacy_real_success_and_failure_migrate_without_initial_review_drift(self):
        self.store.rollback_validation_extension()
        good=self.create_candidate(); bad=self.create_candidate(); native_trial(self.store,good); native_trial(self.store,bad,False)
        self.store.apply_validation_extension()
        with self.store.connect() as db: result=migrate_legacy_validation_records(db)
        self.assertEqual(result['migrated'],2); self.assertTrue(self.gate(good)['qualified']); self.assertFalse(self.gate(bad)['qualified'])
        self.assertEqual(self.store.get_validation_records(bad['task_id'])[0]['status'],'failed')

    def test_legacy_migration_preserves_final_rejected(self):
        self.store.rollback_validation_extension(); c=self.create_candidate(); native_trial(self.store,c)
        self.store.review_memory(c['task_id'],'final','reject','TEST拒绝',c['evidence_sha256'])
        self.store.apply_validation_extension()
        with self.store.connect() as db: result=migrate_legacy_validation_records(db)
        self.assertEqual(result['preserved_terminal'],1); self.assertEqual(self.store.get_memory_candidate(c['task_id'])['status'],'final_rejected')

    def test_validation_extension_migration_and_rollback(self):
        raw=EventStore(self.root/'raw.sqlite'); raw.initialize(); self.assertFalse(raw.is_validation_extension_installed())
        self.assertTrue(raw.apply_validation_extension(dry_run=True)['dry_run']); self.assertFalse(raw.is_validation_extension_installed())
        raw.apply_validation_extension(); self.assertTrue(raw.is_validation_extension_installed()); self.assertTrue(raw.apply_validation_extension()['already_installed'])
        raw.rollback_validation_extension(); self.assertFalse(raw.is_validation_extension_installed())

    def test_future_version_and_missing_columns_fail_closed(self):
        for mutation in ("UPDATE experience_validation_meta SET value='999' WHERE key='version'",'ALTER TABLE experience_validation_records DROP COLUMN execution_digest'):
            with self.subTest(mutation=mutation):
                with self.store.connect() as db: db.execute(mutation)
                self.assertFalse(self.store.is_validation_extension_installed())
                with self.assertRaises(ValueError): self.store.apply_validation_extension()
                self.store.rollback_validation_extension(); self.store.apply_validation_extension()

    def test_malformed_meta_returns_false(self):
        with self.store.connect() as db: db.execute('ALTER TABLE experience_validation_meta RENAME COLUMN value TO wrong_value')
        self.assertFalse(self.store.is_validation_extension_installed())

    def test_schema_migration_ddl_failure_rolls_back_all_tables(self):
        path=self.root/'atomic.sqlite'; db=sqlite3.connect(path,isolation_level=None)
        def authorize(action,arg1,arg2,*rest):
            return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_CREATE_TABLE and arg1=='experience_validation_records' else sqlite3.SQLITE_OK
        db.set_authorizer(authorize)
        with self.assertRaises(sqlite3.DatabaseError): apply_validation_schema_migration(db)
        self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'experience_validation_%'").fetchone()[0],0); db.close()

    def test_dry_run_missing_path_creates_nothing(self):
        path=self.root/'nonexistent'/'dry.sqlite'; apply_validation_schema_migration(path,dry_run=True); self.assertFalse(path.parent.exists())

    def test_duplicate_first_failure_does_not_consume_round_two(self):
        c=self.create_candidate(); first=native_trial(self.store,c,False)
        before=self.store.get_validation_lineage(c['task_id'])
        for _ in range(3): self.assertTrue(self.advance(c,first)['idempotent_replay'])
        self.assertEqual(self.store.get_validation_lineage(c['task_id']),before)

    def test_paused_round_requires_explicit_resume(self):
        c=self.create_candidate();task=native_trial(self.store,c,settle=False)
        rec=self.store.get_validation_records(c['task_id'])[0]
        pause_validation_attempt(self.store,c['task_id'],rec['id'],'TEST暂停')
        with self.assertRaises(ValueError):complete_audit(self.store,task['task_id'],'TEST禁止隐式恢复')
        self.assertEqual(self.store.get_validation_lineage(c['task_id'])['rounds_used'],0)
        self.assertEqual(self.store.get_task(task['task_id'])['status'],'pending')

    def test_stale_timedout_truncated_fabricated_execution_is_rejected(self):
        c=self.create_candidate()
        data={'execution':{'passed':True,'started_at':1,'finished_at':0,'cases':[{'passed':True,'returncode':0,'timed_out':True,'truncated':True,'expect_stdout':'','output':'PASS'}]},'dsh_verdict':'PASS','peer_verdict':'PASS'}
        with self.assertRaises(ValueError):self.advance(c,data)
        self.assertFalse(self.gate(c)['qualified']);self.assertIsNone(self.store.get_validation_lineage(c['task_id']))

    def test_missing_constraints_unique_index_and_foreign_keys_fail_closed(self):
        with self.store.read_only() as db:
            schema=[r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name LIKE 'experience_validation_%' ORDER BY name")]
        changes=(
            ("CHECK(round IN (1, 2))",''),
            ("CHECK(status IN ('pending', 'running', 'passed', 'failed', 'paused'))",''),
            (',\n                UNIQUE(candidate_id, round)',''),
            (' REFERENCES memory_candidates(task_id)',''),
            ('candidate_id TEXT NOT NULL','candidate_id TEXT'),
        )
        from xiaoba_agentos.validation_migration import is_validation_extension_installed
        for before,after in changes:
            with self.subTest(mutation=before):
                db=sqlite3.connect(':memory:');self.addCleanup(db.close)
                for sql in schema:db.execute(sql.replace(before,after))
                db.execute("INSERT INTO experience_validation_meta VALUES ('version','1',0)")
                self.assertFalse(is_validation_extension_installed(db))

    def test_rollback_failure_is_atomic(self):
        db=sqlite3.connect(self.store.path,isolation_level=None);self.addCleanup(db.close)
        db.set_authorizer(lambda action,name,*args:sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_DROP_TABLE and name=='experience_validation_lineages' else sqlite3.SQLITE_OK)
        from xiaoba_agentos.validation_migration import rollback_validation_schema_migration
        with self.assertRaises(sqlite3.DatabaseError):rollback_validation_schema_migration(db)
        self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name LIKE 'experience_validation_%'").fetchone()[0],3)

    def test_dry_run_existing_database_preserves_bytes(self):
        path=self.root/'dry-existing.sqlite';raw=EventStore(path);raw.initialize()
        before=hashlib.sha256(path.read_bytes()).hexdigest();apply_validation_schema_migration(path,dry_run=True)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)

    def test_unrelated_pending_task_with_same_reviewer_failure_never_qualifies(self):
        c=self.create_candidate();tid=self.store.submit('TEST无关任务',1,snapshot={'experience_policy':{'kind':'audit','version':1}})
        for _ in range(2):
            r=self.store.start_agent_run(tid,'same-reviewer');self.store.finish_agent_run(r,'failed','TRIAL_VALIDATION: FAIL','TEST失败',1)
        with self.assertRaises(ValueError):self.advance(c,{'task_id':tid,'execution_digest':'a'*64,'dsh_verdict':'PASS','peer_verdict':'PASS'})
        self.assertFalse(self.gate(c)['qualified'])

    def test_broken_schema_status_reports_blocked_without_crashing(self):
        c=self.create_candidate();native_trial(self.store,c)
        with self.store.connect() as db:db.execute('ALTER TABLE experience_validation_records DROP COLUMN execution_digest')
        from xiaoba_agentos.experience_workflow import trial_summary
        with self.store.read_only() as db:
            result=trial_summary(db,c)
        self.assertEqual(result['state'],'invalid_schema');self.assertFalse(result['ready'])

    def test_new_retry_attempt_fences_old_controller(self):
        c=self.create_candidate();card,execution=make_execution(self.cfg.runtime_dir)
        old=reserve_validation_attempt(self.store,c['task_id'],card)
        pause_validation_attempt(self.store,c['task_id'],old,'TEST控制器中断');self.resume(c)
        new=reserve_validation_attempt(self.store,c['task_id'],card);self.assertNotEqual(old,new)
        with self.assertRaises(ValueError):prepare_trial(self.store,c['task_id'],card,execution,attempt_id=old)
        self.assertEqual(self.store.get_validation_lineage(c['task_id'])['rounds_used'],0)
