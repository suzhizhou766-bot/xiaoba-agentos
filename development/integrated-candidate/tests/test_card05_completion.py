"""Card 05 public completion and recovery cases, isolated from production."""
import hashlib
import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_card37 as fixture
from fixtures_review import _event_count
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.memory import MemoryBridge
from xiaoba_agentos.store import EventStore
from test_pool_dispatch import _FakePoolResource


class Card05CompletionTests(unittest.TestCase):
    setUp = fixture.Card37Tests.setUp
    submit = fixture.Card37Tests.submit
    host = fixture.Card37Tests.host
    receipt = fixture.Card37Tests.receipt

    def cli(self, *args):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env["PYTHONPATH"] = os.pathsep.join([
            str(Path(__file__).resolve().parents[1] / "src"),
            str(Path(__file__).resolve().parents[2] / "review-ingress"),
        ])
        run = subprocess.run(
            [sys.executable, "-B", "-m", "xiaoba_agentos.cli", "--config", str(self.config.path), *args],
            capture_output=True, text=True, timeout=20, env=env,
        )
        self.assertEqual(0, run.returncode, run.stderr + run.stdout)
        return json.loads(run.stdout)

    def test_public_host_fallback_marks_no_independent_review_and_keeps_answer(self):
        task = self.submit("method", host=self.host())
        receipt_path = self.root / "host.json"
        receipt_path.write_text(json.dumps(self.receipt(task)), encoding="utf-8")
        result = self.cli("task-host-complete", task, "--receipt", str(receipt_path), "--user-chose-host")
        self.assertEqual("pending_delivery_review", result["candidate_status"])
        self.assertEqual("host_fallback_no_independent_review", result["value_assessment"]["review_mode"])
        self.assertEqual(1, result["value_assessment"]["samples"])
        stored = self.store.get_task(task)
        self.assertIn("无独立审查", stored["result"])
        self.assertEqual(hashlib.sha256(stored["result"].encode()).hexdigest(),
                         json.loads(stored["provenance_json"])["result_sha256"])

    def test_public_host_fail_and_needs_info_do_not_create_candidate(self):
        for verdict in ("FAIL", "NEEDS_INFO"):
            with self.subTest(verdict=verdict):
                task = self.submit("method", host=self.host())
                receipt = dict(self.receipt(task), verdict=verdict)
                receipt_path = self.root / (verdict + ".json")
                receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                result = self.cli("task-host-complete", task, "--receipt", str(receipt_path), "--user-chose-host")
                self.assertEqual("none", result["candidate_status"])
                self.assertIsNone(self.store.get_memory_candidate(task))
                self.assertEqual([], self.cli("memory-value-list")["items"])
                self.assertIn(verdict, self.store.get_task(task)["result"])

    def test_committed_value_preparation_is_recognized_for_recovery(self):
        task = self.store.submit("TEST deferred completion", 2, snapshot={
            "experience_policy": {"version": 1, "kind": "method"},
        })
        path = self.config.runtime_dir / "memory-outbox" / f"{task}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("TEST deferred candidate", encoding="utf-8")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        self.store.complete_with_memory_candidate(task, "TEST answer", str(path), sha, [], expected_attempt=0)
        self.assertTrue(Orchestrator(self.config, self.store)._completion_committed(task))
        self.assertTrue(path.is_file())
        self.assertEqual("awaiting_value_review", self.cli("memory-value-list")["items"][0]["queue_status"])

    def test_recovered_worker_cannot_overwrite_second_workers_candidate(self):
        fixture.Card37Tests.seed(self, 2)
        task = self.submit("method")
        first_at_stage = threading.Event()
        resume_first = threading.Event()
        stage_calls = []
        body_stage_calls = []
        original_settle = EventStore.complete_worker_with_memory_candidate
        original_stage = MemoryBridge.stage_experience

        def delayed_settle(store, task_id, result, staged_path, stage_candidate,
                           source_task_ids, *, expected_attempt, **kwargs):
            stage_calls.append(expected_attempt)
            if expected_attempt == 1:
                first_at_stage.set()
                if not resume_first.wait(5):
                    raise TimeoutError("TEST first Worker was not released")
            return original_settle(store, task_id, result, staged_path, stage_candidate,
                                   source_task_ids, expected_attempt=expected_attempt, **kwargs)

        def marked_stage(bridge, *args, **kwargs):
            path = original_stage(bridge, *args, **kwargs)
            path.write_text(path.read_text() + "\nTEST second worker owns this staged version\n")
            os.chmod(path, 0o600)
            body_stage_calls.append(path)
            return path

        with patch.object(EventStore, "complete_worker_with_memory_candidate", new=delayed_settle), \
                patch.object(MemoryBridge, "stage_experience", new=marked_stage), \
                concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            stale = pool.submit(Orchestrator(self.config, self.store).run_once)
            self.assertTrue(first_at_stage.wait(10), "first Worker did not reach candidate settlement")
            self.assertEqual(1, self.store.expire_running_leases())
            self.assertEqual(1, self.store.recover_expired())
            winner = Orchestrator(self.config, self.store).run_once()
            self.assertEqual("completed", winner["status"], winner)
            candidate = self.store.get_memory_candidate(task)
            self.assertIsNotNone(candidate)
            staged_path = Path(candidate["staged_path"])
            winning_bytes = staged_path.read_bytes()
            winning_hash = hashlib.sha256(winning_bytes).hexdigest()
            self.assertEqual(candidate["evidence_sha256"], winning_hash)
            self.assertIn(b"TEST second worker owns this staged version", winning_bytes)

            resume_first.set()
            stale_result = stale.result(timeout=20)

        self.assertEqual("completed", stale_result["status"], stale_result)
        self.assertEqual([1, 2], stage_calls)
        self.assertEqual(1, len(body_stage_calls), "stale attempt must fail before generating candidate bytes")
        self.assertEqual(winning_bytes, staged_path.read_bytes())
        current = self.store.get_memory_candidate(task)
        self.assertEqual(winning_hash, current["evidence_sha256"])
        self.assertEqual("completed", self.store.get_task(task)["status"])
        with self.store.read_only() as db:
            self.assertEqual(2, db.execute("SELECT attempts FROM tasks WHERE id=?", (task,)).fetchone()[0])
            self.assertEqual(1, db.execute(
                "SELECT count(*) FROM events WHERE task_id=? AND event_type='task.completed'", (task,)
            ).fetchone()[0])
            self.assertEqual(1, db.execute(
                "SELECT count(*) FROM events WHERE task_id=? AND event_type='memory.candidate_staged'", (task,)
            ).fetchone()[0])
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_discard_cleanup_recovers_after_worker_commit_crash(self):
        fixture.Card37Tests.seed(self, 2)
        task = self.store.submit("TEST discarded worker candidate", 2, snapshot={
            "needs_review": True,
            "experience_policy": {"version": 1, "kind": "method"},
        })
        staged = self.config.runtime_dir / "memory-outbox" / f"{task}.md"
        committed_result = self.root / "committed-result.txt"
        script = '''
import os,sys
import json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
sys.path.insert(0,sys.argv[2])
from test_pool_dispatch import _FakePoolResource
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.store import EventStore
class LowScoreResource(_FakePoolResource):
    def run_task(resource, task_id, step_code, instruction, materials, **kwargs):
        value=super().run_task(task_id,step_code,instruction,materials)
        value['body']=value['body'].replace('评分：8','评分：5')
        return value
def factory(source,workspace=None,**kwargs):
    return LowScoreResource(source,workspace)
def crash_after_commit(store,task_id,staged_path,candidate_sha256):
    Path(sys.argv[4]).write_text(store.get_task(task_id)['result'],encoding='utf-8')
    os._exit(9)
config=AppConfig.load(Path(sys.argv[3]))
store=EventStore(config.database_path)
with patch('xiaoba_agentos.orchestrator.release_binding_status',return_value={'ok':True}), \\
     patch('xiaoba_agentos.adapters._pool_cli_factory',return_value=factory), \\
     patch.object(EventStore,'cleanup_discarded_candidate',new=crash_after_commit):
    result=Orchestrator(config,store).run_once()
print(json.dumps(result,ensure_ascii=False))
'''
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        source = Path(__file__).resolve().parents[3]
        env["PYTHONPATH"] = os.pathsep.join([
            str(Path(__file__).resolve().parents[1] / "src"),
            str(Path(__file__).resolve().parents[1] / "tests"),
            str(source / "development" / "review-ingress"),
        ])
        result = subprocess.run(
            [sys.executable, "-B", "-c", script,
             str(Path(__file__).resolve().parents[1] / "src"),
             str(Path(__file__).resolve().parents[1] / "tests"), str(self.config.path),
             str(committed_result)],
            capture_output=True, text=True, timeout=20, env=env,
        )
        self.assertEqual(9, result.returncode, result.stderr + result.stdout)
        self.assertTrue(staged.is_file())
        committed = self.store.get_task(task)
        self.assertTrue(committed_result.is_file())
        self.assertEqual(committed_result.read_text(encoding="utf-8"), committed["result"])
        self.assertIn("评分：5", committed["result"])
        self.assertEqual("value_discarded", json.loads(committed["provenance_json"])["memory_disposition"])

        self.store.initialize()

        self.assertFalse(staged.exists())
        recovered = self.store.get_task(task)
        self.assertEqual(committed_result.read_text(encoding="utf-8"), recovered["result"])
        self.assertEqual(hashlib.sha256(recovered["result"].encode()).hexdigest(),
                         json.loads(recovered["provenance_json"])["result_sha256"])
        self.assertEqual(1, _event_count(self.store, task, "memory.value_assessed"))
        self.assertEqual(1, _event_count(self.store, task, "task.completed"))
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_public_host_incomplete_value_receipt_is_needs_info_and_can_be_corrected(self):
        for field in ("score", "applicability", "evidence_refs"):
            with self.subTest(field=field):
                task = self.submit("method", host=self.host())
                receipt = self.receipt(task)
                if field == "evidence_refs":
                    receipt[field] = ["task:unrelated"]
                else:
                    del receipt[field]
                path = self.root / (field + ".json")
                path.write_text(json.dumps(receipt), encoding="utf-8")
                result = self.cli("task-host-complete", task, "--receipt", str(path), "--user-chose-host")
                self.assertEqual("NEEDS_INFO", result["verdict"])
                self.assertEqual("pending", self.store.get_task(task)["status"])
                self.assertIsNone(self.store.get_task(task)["result"])
                self.assertIsNone(self.store.get_memory_candidate(task))
                self.assertEqual([], self.cli("memory-value-list")["items"])
                # A rejected receipt has no accepted ID binding. Correcting
                # that same receipt ID is allowed and settles exactly once.
                corrected = self.receipt(task)
                path.write_text(json.dumps(corrected), encoding="utf-8")
                result = self.cli("task-host-complete", task, "--receipt", str(path), "--user-chose-host")
                self.assertEqual("completed", result["status"])
                with self.store.read_only() as db:
                    self.assertEqual(1, db.execute("SELECT count(*) FROM events WHERE task_id=? AND event_type='host.receipt_needs_info'", (task,)).fetchone()[0])
                    self.assertEqual(1, db.execute("SELECT count(*) FROM events WHERE task_id=? AND event_type='task.completed'", (task,)).fetchone()[0])

    def test_pool_needs_info_preserves_receipts_and_only_reruns_incomplete_side(self):
        fixture.Card37Tests.seed(self, 2)
        calls = []
        repaired = []

        class Resource(_FakePoolResource):
            def run_task(resource, task_id, step_code, instruction, materials, **kwargs):
                calls.append(resource.source['reviewer_id'])
                value = super().run_task(task_id, step_code, instruction, materials)
                if resource.source['reviewer_id'] == '0' and not repaired:
                    value['body'] = value['body'].replace('适用条件：TEST fixtures only\n', '')
                return value

        self.enterContext(patch('xiaoba_agentos.adapters._pool_cli_factory', return_value=Resource))
        task = self.submit("method")
        result = Orchestrator(self.config, self.store).run_once()
        self.assertEqual("NEEDS_INFO", result["verdict"])
        self.assertEqual("frozen_paused", self.store.get_task(task)["status"])
        self.assertIsNone(self.store.get_memory_candidate(task))
        self.assertEqual([], self.store.value_review_queue())
        with self.store.read_only() as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM events WHERE task_id=? AND event_type IN ('task.completed','task_scoring.assessed')", (task,)).fetchone()[0])
            self.assertEqual(2, db.execute("SELECT count(*) FROM agent_runs WHERE task_id=? AND output IS NOT NULL", (task,)).fetchone()[0])
        Orchestrator(self.config, self.store).run_once()
        self.assertEqual(2, len(calls))
        repaired.append(True)
        self.store.resume_task(task, result['freeze_hash'], enabled_names=['0', '1'],
                               health_max_age_seconds=86400,
                               receipt_root=self.config.runtime_dir / 'receipts')
        result = Orchestrator(self.config, self.store).run_once()
        self.assertEqual('completed', result['status'], result)
        self.assertEqual(2, calls.count('0'))
        self.assertEqual(1, calls.count('1'))
        self.assertEqual('pending_delivery_review', self.store.get_memory_candidate(task)['status'])

    def test_historical_receipt_keeps_old_contract_but_new_contract_rejects_missing_fields(self):
        from xiaoba_agentos.task_scores import accepted
        old = self.store.submit('TEST old receipt', 2, snapshot={
            'experience_policy': {'version': 1, 'kind': 'method'},
        })
        run = {'status': 'completed', 'output': '审查结论：PASS\n评分：8\nTEST old receipt'}
        self.assertTrue(accepted(run, 'review', require_review_vote=True, task=self.store.get_task(old)))
        new = self.submit('method', host=self.host())
        self.assertFalse(accepted(run, 'review', require_review_vote=True, task=self.store.get_task(new)))

    def test_pool_missing_score_and_unbound_evidence_never_settle(self):
        fixture.Card37Tests.seed(self, 2)
        for field in ('score', 'evidence_refs'):
            with self.subTest(field=field):
                class Resource(_FakePoolResource):
                    def run_task(resource, task_id, step_code, instruction, materials):
                        value = super().run_task(task_id, step_code, instruction, materials)
                        if resource.source['reviewer_id'] == '0':
                            if field == 'score':
                                value['body'] = value['body'].replace('评分：8\n', '')
                            else:
                                value['body'] = value['body'].replace('task:' + task_id, 'task:unrelated')
                        return value
                with patch('xiaoba_agentos.adapters._pool_cli_factory', return_value=Resource):
                    task = self.submit('method')
                    result = Orchestrator(self.config, self.store).run_once()
                self.assertEqual('NEEDS_INFO', result['verdict'])
                self.assertIsNone(self.store.get_memory_candidate(task))
                self.assertIsNone(self.store.get_task(task)['result'])
                with self.store.read_only() as db:
                    self.assertEqual(0, db.execute("SELECT count(*) FROM events WHERE task_id=? AND event_type IN ('task.completed','task_scoring.assessed')", (task,)).fetchone()[0])

    def test_structured_pool_evidence_keeps_reference_and_hash(self):
        from xiaoba_agentos.pool_dispatch import pool_receipt_output, review_vote_problem
        task_id = self.submit('method', host=self.host())
        task = self.store.get_task(task_id)
        output = pool_receipt_output({
            'body': 'TEST actual conclusion',
            'metadata': {'verdict': 'PASS', 'review_type': 'new_experience',
                         'score': 8, 'applicability': 'TEST only'},
            'evidence_refs': [{'ref': 'task:' + task_id, 'sha256': task['execution_snapshot_hash']}],
        })
        self.assertEqual('', review_vote_problem(output, task))
