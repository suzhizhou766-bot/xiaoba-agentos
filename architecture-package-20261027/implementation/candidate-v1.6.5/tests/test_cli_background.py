"""Real local subprocesses exercise waiting, visibility and lease ownership.

No upstream, desktop, credentials or production service is used.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaoba_agentos.adapters import (AgentResult, BaseAdapter, run_cli, _parse_qoder_result,
                                    AgyAdapter, DshAdapter, QoderAdapter, PermanentAdapterError)
from xiaoba_agentos.adapters import CLI_IDLE_TIMEOUT_SECONDS, NoProgressTimeout
from xiaoba_agentos.receipts import ExecutionReceiptStore, ReceiptError
from xiaoba_agentos.recovery import classify_failure, _probe_one
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.orchestrator import Orchestrator
import test_standby_substitution as standby_fixtures


class BackgroundCliTest(unittest.TestCase):
    def test_shared_inactivity_limit_is_300_without_a_total_deadline(self):
        import inspect
        self.assertEqual(300, CLI_IDLE_TIMEOUT_SECONDS)
        self.assertIsNone(inspect.signature(run_cli).parameters['timeout'].default)
        self.assertEqual(300, inspect.signature(run_cli).parameters['idle_timeout'].default)

    def test_real_progress_refreshes_idle_clock_beyond_multiple_idle_periods(self):
        updates = []
        code = ('import json,time; '
                '[(print(json.dumps({"type":"text","text":"token"}),flush=True),time.sleep(.15)) for _ in range(8)]')
        started = time.monotonic()
        result = run_cli([sys.executable, '-B', '-c', code], idle_timeout=.6,
                         progress=updates.append, progress_interval=.08)
        self.assertEqual(0, result.returncode)
        self.assertGreater(time.monotonic() - started, .3)
        self.assertEqual(8, updates[-1]['activity_events'])

    def test_mid_run_stall_is_failure_only_after_progress_stops_and_process_is_killed(self):
        updates = []
        code = ('import json,time; '
                '[(print(json.dumps({"type":"text","text":"token"}),flush=True),time.sleep(.15)) for _ in range(5)]; '
                'time.sleep(30)')
        started = time.monotonic()
        with self.assertRaises(NoProgressTimeout) as caught:
            run_cli([sys.executable, '-B', '-c', code], idle_timeout=.6,
                    progress=updates.append, progress_interval=.08)
        self.assertGreater(time.monotonic() - started, .3)
        self.assertEqual('external_upstream', classify_failure(caught.exception).name)
        self.assertTrue(caught.exception.no_progress_timeout)
        self.assertGreaterEqual(updates[-1]['no_progress_seconds'], .15)
        with self.assertRaises(ProcessLookupError):
            os.kill(updates[0]['pid'], 0)

    def test_heartbeats_repeated_init_empty_deltas_and_stderr_do_not_refresh_idle_clock(self):
        updates = []
        code = ('import json,time,sys; '
                '[(print(json.dumps({"type":"init"}),flush=True),'
                'print(json.dumps({"type":"text","text":""}),flush=True),'
                'print(json.dumps({"type":"ping"}),flush=True),'
                'print("still alive",file=sys.stderr,flush=True),time.sleep(.02)) for _ in range(100)]')
        with self.assertRaises(NoProgressTimeout):
            run_cli([sys.executable, '-B', '-c', code], idle_timeout=.6,
                    progress=updates.append, progress_interval=.08)
        self.assertEqual(0, updates[-1]['activity_events'])
        self.assertEqual('cli_ready', updates[-1]['stage'])
        self.assertLess(updates[-1]['elapsed_seconds'], 1)

    def test_wrapped_reasoning_delta_counts_as_progress_without_persisting_reasoning(self):
        updates = []
        event = {'type': 'stream_event', 'event': {'type': 'content_block_delta',
                 'delta': {'type': 'thinking_delta', 'thinking': 'private thought'}}}
        code = ('import time; '
                '[(print(' + repr(json.dumps(event)) + ',flush=True),time.sleep(.15)) for _ in range(6)]')
        result = run_cli([sys.executable, '-B', '-c', code], idle_timeout=.6,
                         progress=updates.append, progress_interval=.08)
        self.assertEqual(0, result.returncode)
        self.assertEqual(6, updates[-1]['activity_events'])
        self.assertNotIn('private thought', json.dumps(updates, ensure_ascii=False))

    def test_quiet_live_process_waits_and_does_not_claim_model_activity(self):
        updates = []
        result = run_cli([sys.executable, '-B', '-c',
            'import time; time.sleep(.15); print("done")'],
            progress=updates.append, progress_interval=.03)
        self.assertEqual('done', result.stdout.strip())
        self.assertGreater(len(updates), 2)
        self.assertTrue(all(row['stage'] == 'process_running_unconfirmed' for row in updates))

    def test_stream_activity_visible_before_completion_without_raw_text(self):
        updates = []
        code = ('import json,time,sys; '
                'print(json.dumps({"type":"init","token":"secret"}),flush=True); '
                'time.sleep(.08); '
                'print(json.dumps({"type":"text","text":"private reasoning"}),flush=True); '
                'time.sleep(.12); print("finished",flush=True)')
        result = run_cli([sys.executable, '-B', '-c', code],
                         progress=updates.append, progress_interval=.03)
        self.assertIn('finished', result.stdout)
        self.assertIn('cli_ready', [row['stage'] for row in updates])
        self.assertIn('model_activity', [row['stage'] for row in updates])
        self.assertEqual(1, updates[-1]['activity_events'])
        self.assertNotIn('secret', json.dumps(updates, ensure_ascii=False))
        self.assertNotIn('private reasoning', json.dumps(updates, ensure_ascii=False))

    def test_both_output_pipes_are_drained_and_nonzero_exit_preserved(self):
        code = 'import sys; sys.stdout.write("x"*150000); sys.stderr.write("y"*150000); sys.exit(7)'
        result = run_cli([sys.executable, '-B', '-c', code], progress_interval=.08)
        self.assertEqual(7, result.returncode)
        self.assertEqual(150000, len(result.stdout))
        self.assertEqual(150000, len(result.stderr))

    def test_progress_failure_terminates_process_group(self):
        pid = []
        def lost_lease(update):
            pid.append(update['pid'])
            raise RuntimeError('lease lost')
        with self.assertRaisesRegex(RuntimeError, 'lease lost'):
            run_cli([sys.executable, '-B', '-c', 'import time; time.sleep(30)'], progress=lost_lease)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid[0], 0)

    def test_qoder_stream_requires_one_terminal_result(self):
        init = json.dumps({'type': 'system', 'subtype': 'init'})
        partial = json.dumps({'type': 'assistant', 'message': {'text': 'partial'}})
        final = json.dumps({'type': 'result', 'result': 'answer', 'modelUsage': {'model': {}}})
        self.assertIsNone(_parse_qoder_result(init + '\n' + partial)[0])
        self.assertEqual('answer', _parse_qoder_result(init + '\n' + partial + '\n' + final)[0]['result'])
        self.assertIsNone(_parse_qoder_result(final + '\n' + final)[0])

    def test_probe_deadline_is_explicit_and_separate_from_real_tasks(self):
        from types import SimpleNamespace
        agent = SimpleNamespace(timeout_seconds=12, driver='qoder')
        adapter = BaseAdapter(agent)
        self.assertIsNone(adapter.execution_timeout)
        def reply(*args):
            self.assertEqual(12, adapter.execution_timeout)
            return AgentResult('q', 'XIAOBA_AGENT_HEALTH_OK', 1)
        with patch('xiaoba_agentos.recovery.build_adapter', return_value=adapter), \
             patch.object(adapter, 'run', side_effect=reply):
            _probe_one(agent)

    def test_transport_failure_vocabulary_is_shared(self):
        from xiaoba_agentos.adapters import _is_transient_cli_error
        for message in ('connect ETIMEDOUT', 'socket hang up', 'read ECONNRESET',
                        'HTTP 500', 'HTTP 502', 'HTTP 503', 'HTTP 504',
                        'TLS handshake failed', 'server disconnected'):
            with self.subTest(message=message):
                self.assertTrue(_is_transient_cli_error(message))
                self.assertEqual('external_upstream', classify_failure(RuntimeError(message)).name)
        # "HTTP 503" carries its context ("HTTP"); a bare number does not, and the
        # two layers agree on that too (release notes, section 15).
        for message in ('waited 503 seconds', 'line 502 of the log', 'item 500'):
            with self.subTest(bare=message):
                self.assertFalse(_is_transient_cli_error(message))
                self.assertNotEqual('external_upstream', classify_failure(RuntimeError(message)).name)

    def test_all_three_adapters_wait_past_configured_second(self):
        from types import SimpleNamespace
        app = SimpleNamespace(integrations={})
        agent = SimpleNamespace(name='review', role='', timeout_seconds=1, raw={}, app_config=app)
        outputs = [
            (AgyAdapter, {'event': 'result', 'result': {'status': 'SUCCESS', 'response': 'review complete'}}),
            (DshAdapter, {'type': 'final', 'text': 'review complete'}),
            (QoderAdapter, {'type': 'result', 'result': 'review complete', 'modelUsage': {'model': {}}}),
        ]
        for adapter_type, output in outputs:
            with self.subTest(adapter=adapter_type.__name__):
                adapter = adapter_type(agent)
                # Only Qoder reads its selection in _command/run; AGY and DSH have
                # _model/_command patched below, so they get no selection here.
                if adapter_type is QoderAdapter:
                    adapter._selection = {'model': 'model', 'model_source': 'qodercli_settings'}
                code = 'import time; time.sleep(1.1); print(' + repr(json.dumps(output)) + ')'
                command = [sys.executable, '-B', '-c', code]
                with patch.object(adapter, '_model', return_value='model'), \
                     patch.object(adapter, '_command', return_value=command), \
                     patch('xiaoba_agentos.adapters.command_environment', return_value=dict(os.environ)):
                    self.assertEqual('review complete', adapter.run('review', '').output)


class StandbyFailureMatrixTest(unittest.TestCase):
    def test_actual_stalled_cli_is_reaped_before_either_primary_is_replaced(self):
        helper = standby_fixtures.StandbySubstitutionTest()
        for failed in ('a', 'b'):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                config = helper._config(Path(directory), standby_fixtures.PRIMARIES + [standby_fixtures.STANDBY])
                store = helper._store(config); helper._probe(store, a='healthy', b='healthy', s='healthy')
                task_id = store.submit('review', 3)
                calls = {}; pids = []
                class ActualStall(BaseAdapter):
                    def run(self, prompt, context):
                        calls[self.config.name] = calls.get(self.config.name, 0) + 1
                        def progress(update):
                            pids.append(update['pid'])
                            self.progress_callback(update)
                        run_cli([sys.executable, '-B', '-c',
                                 'import json,time; print(json.dumps({"type":"text","text":"started"}),flush=True); time.sleep(30)'],
                                idle_timeout=.6, progress=progress, progress_interval=.08)
                def adapter_for(agent):
                    if agent.name == failed:
                        return ActualStall(agent)
                    if agent.name == 's':
                        self.assertTrue(pids)
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pids[0], 0)
                    return standby_fixtures.Scripted(agent, {}, calls)
                with patch('xiaoba_agentos.orchestrator.build_adapter', side_effect=adapter_for):
                    result = Orchestrator(config, store).run_once()
                self.assertEqual('completed', result['status'])
                self.assertEqual({'a': 1, 'b': 1, 's': 1}, calls)
                self.assertEqual(failed, result['standby_substitution']['replaced'])
                self.assertIn('无实际进展', result['standby_substitution']['error'])
                self.assertEqual(1, len(helper._substitutions(store, task_id)))
                self.assertTrue(store.verify_event_chain()['ok'])

    def test_either_primary_can_be_replaced_for_real_external_failures(self):
        # These are actual terminal CLI error texts. A quiet live process does
        # not enter this path. Every case preserves the other primary's answer.
        errors = ['AGY connect timed out', 'dsh: read ECONNRESET', 'socket hang up',
                  'unexpected EOF', 'HTTP 502 Bad Gateway', 'HTTP 503',
                  'HTTP 504 Gateway Timeout', '429 rate limit', 'quota exhausted',
                  '401 Unauthorized', '403 Forbidden', 'TLS handshake failed']
        helper = standby_fixtures.StandbySubstitutionTest()
        for primary in ('a', 'b'):
            for error in errors:
                with self.subTest(primary=primary, error=error), tempfile.TemporaryDirectory() as directory:
                    config = helper._config(Path(directory), standby_fixtures.PRIMARIES + [standby_fixtures.STANDBY])
                    store = helper._store(config)
                    helper._probe(store, a='healthy', b='healthy', s='healthy')
                    task_id = store.submit('independent review', 3)
                    result, calls = helper._run(config, store, {primary: PermanentAdapterError(error)})
                    self.assertEqual('completed', result['status'])
                    self.assertEqual({'a': 1, 'b': 1, 's': 1}, calls)
                    self.assertEqual(primary, result['standby_substitution']['replaced'])
                    self.assertEqual(1, len(helper._substitutions(store, task_id)))
                    self.assertTrue(store.verify_event_chain()['ok'])


class BackgroundLeaseTest(unittest.TestCase):
    def test_renewal_prevents_duplicate_recovery_and_rejects_old_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / 'queue.sqlite3'); store.initialize()
            task_id = store.submit('review', 3)
            task = store.lease_next(1)
            lease = store.get_task(task_id)['lease_until']
            # Move time past the original lease after a valid heartbeat.
            with patch('xiaoba_agentos.store.time.time', return_value=lease - .1):
                self.assertTrue(store.renew_lease(task_id, task['attempts'], 5))
            with patch('xiaoba_agentos.store.time.time', return_value=lease + .1):
                self.assertEqual(0, store.recover_expired())
                self.assertIsNone(store.lease_next(1))
            with patch('xiaoba_agentos.store.time.time', return_value=lease + 6):
                self.assertFalse(store.renew_lease(task_id, task['attempts'], 5))
                self.assertEqual(1, store.recover_expired())
                new = store.lease_next(5)
                self.assertEqual(2, new['attempts'])
                self.assertFalse(store.renew_lease(task_id, task['attempts'], 5))
                self.assertTrue(store.owns_lease(task_id, new['attempts']))
            self.assertTrue(store.verify_event_chain()['ok'])

    def test_renewal_reads_the_clock_after_waiting_for_the_write_lock(self):
        # Round-40 DSH review: the time used to be taken before waiting on
        # another writer, so a lease that expired during the wait still
        # counted as unexpired and was renewed.
        import sqlite3
        import threading
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'queue.sqlite3'
            store = EventStore(path); store.initialize()
            task_id = store.submit('review', 3)
            task = store.lease_next(1)
            lease = store.get_task(task_id)['lease_until']
            clock = [lease - .5]
            holder = sqlite3.connect(str(path), timeout=30)
            holder.execute('BEGIN IMMEDIATE')
            outcome = {}
            with patch('xiaoba_agentos.store.time.time', side_effect=lambda: clock[0]):
                worker = threading.Thread(target=lambda: outcome.setdefault(
                    'renewed', store.renew_lease(task_id, task['attempts'], 5)))
                worker.start()
                time.sleep(.5)  # renew_lease is now waiting on the held write lock
                clock[0] = lease + 1  # the lease expires during that wait
                holder.rollback(); holder.close()
                worker.join(10)
            self.assertFalse(worker.is_alive())
            self.assertIs(False, outcome['renewed'])
            self.assertEqual(lease, store.get_task(task_id)['lease_until'])
            self.assertTrue(store.verify_event_chain()['ok'])

    def test_progress_does_not_complete_receipt_and_ignores_secret_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = ExecutionReceiptStore(Path(directory) / 'receipt.json')
            receipt.record_call({'task_id': 't', 'step_id': 's', 'tool': 'cli',
                                 'tool_call_id': 'r', 'command_hash': 'hash'})
            receipt.record_progress('r', {'stage': 'model_activity', 'pid': 1,
                                         'stdout': 'secret', 'token': 'secret'})
            self.assertFalse(receipt.is_success('r'))
            self.assertIsNone(receipt.get('r')['finished_at'])
            self.assertNotIn('secret', receipt.path.read_text())
            self.assertTrue(json.loads(receipt.path.read_text())['receipts']['r']['heartbeat_at'])

    def test_progress_for_an_unrecorded_call_raises_receipt_error(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = ExecutionReceiptStore(Path(directory) / 'receipt.json')
            with self.assertRaises(ReceiptError):
                receipt.record_progress('never-recorded', {'stage': 'cli_ready', 'pid': 1})
            self.assertFalse(receipt.path.exists())

    def test_real_adapter_heartbeat_renews_lease_beyond_old_task_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = standby_fixtures.StandbySubstitutionTest()
            config = helper._config(root, standby_fixtures.PRIMARIES + [standby_fixtures.STANDBY])
            config.raw['worker']['lease_seconds'] = 1.2
            store = helper._store(config)
            task_id = store.submit('long review', 3)
            # Direct run exercises an actual adapter invocation and short lease;
            # no synthetic result can mask a lost heartbeat.
            task = store.lease_next(1.2)
            observed = []
            class LocalCli(BaseAdapter):
                def run(self, prompt, context):
                    run_cli([sys.executable, '-B', '-c', 'import time; time.sleep(3.0); print("done")'],
                            progress=self.progress_callback, progress_interval=self.progress_interval)
                    observed.append(store.recover_expired())
                    return AgentResult(self.config.name, 'review complete', 3000)
            with patch('xiaoba_agentos.orchestrator.build_adapter', side_effect=LocalCli):
                result = Orchestrator(config, store)._run_agent(task_id, config.agents[0], 'review', '', task['attempts'])
            self.assertEqual('review complete', result.output)
            self.assertEqual([0], observed)
            self.assertTrue(store.owns_lease(task_id, task['attempts']))
            self.assertTrue(store.verify_event_chain()['ok'])


if __name__ == '__main__':
    unittest.main()
