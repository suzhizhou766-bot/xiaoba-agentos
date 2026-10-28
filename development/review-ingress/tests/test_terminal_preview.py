"""Real PTY subprocess fixtures; none of these are real model acceptance."""
from concurrent.futures import ThreadPoolExecutor
import json
import time
import unittest

from test_agy_interactive import InteractiveFixture
from xiaoba_review.agent_workspace import AgentWorkspace, WorkspaceError
from xiaoba_review.resource_agy import AgyInteractiveResource
from xiaoba_review.terminal_preview import TerminalPreview
from xiaoba_review.terminal_screen import TerminalScreen


class ProjectionTests(unittest.TestCase):
    def test_screen_repaint_replaces_fragments_and_hides_wrapped_secrets(self):
        screen=TerminalScreen();view=TerminalPreview()
        screen.feed(b'old partial result\r\x1b[2Knew result')
        view.set_screen(screen.text())
        self.assertEqual(view.snapshot()['text'],'new result')
        self.assertNotIn('partial',view.snapshot()['text'])
        view.set_screen('api_key=\nTEST-wrapped-sensitive-value')
        self.assertEqual(view.snapshot()['text'],'[敏感内容已隐藏]')
        view.set_screen('current screen user@example.org')
        self.assertEqual(view.snapshot()['text'],'current screen [账号已隐藏]')

    def test_split_utf8_and_escape_strings_never_become_links_or_commands(self):
        view=TerminalPreview()
        data=('第一行'+chr(27)+'[31m红色'+chr(27)+']52;c;PRIVATE_CLIPBOARD'+chr(7)+'<script>TEST</script>').encode()
        for byte in data:view.feed(bytes([byte]))
        snap=view.snapshot()
        self.assertEqual(snap['text'],'第一行红色<script>TEST</script>')
        self.assertTrue(snap['read_only'])
        self.assertNotIn('PRIVATE_CLIPBOARD',snap['text'])

    def test_sensitive_values_and_account_are_hidden_even_on_partial_lines(self):
        view=TerminalPreview();view.feed(b'api_key=TEST')
        self.assertNotIn('TEST',view.snapshot()['text'])
        view.feed(b'-secret-value\nuser@example.org\n')
        self.assertNotIn('secret-value',view.snapshot()['text'])
        self.assertNotIn('user@example.org',view.snapshot()['text'])

    def test_window_and_single_line_are_bounded(self):
        view=TerminalPreview(lines=3,columns=32)
        view.feed(('x'*1000+'\n') .encode()*10)
        snap=view.snapshot();self.assertTrue(snap['trimmed'])
        self.assertLessEqual(len(snap['text']),100)

    def test_truncation_does_not_remove_the_sensitive_field_marker(self):
        view=TerminalPreview(columns=32);view.feed(('password='+'TEST-secret-'*100).encode())
        self.assertEqual(view.snapshot()['text'],'[敏感内容已隐藏]')


class PreviewTests(InteractiveFixture,unittest.TestCase):
    def test_terminal_animation_does_not_supply_or_change_file_result(self):
        (self.root/'trailing-spinner').touch();node=self.add()
        result=self.probe(node)
        self.assertEqual(result['status'],'passed',result)
        self.assertEqual(self.probe(node,'TEST-second')['status'],'passed')
        self.assertEqual(len({row['pid'] for row in self.calls()}),1)

    def test_terminal_repaint_does_not_change_file_result(self):
        (self.root/'repaint').touch();node=self.add()
        result=self.probe(node)
        self.assertEqual(result['status'],'passed',result)
        self.assertEqual([row['event'] for row in self.calls()],['start','turn'])

    def test_next_turn_waits_for_input_readiness_after_file_receipt(self):
        (self.root/'busy-footer').touch();node=self.add();started=time.monotonic()
        result=self.probe(node)
        self.assertEqual(result['status'],'passed',result)
        self.assertEqual(self.probe(node,'TEST-second')['status'],'passed')
        self.assertGreaterEqual(time.monotonic()-started,.15)

    def test_view_does_not_create_workspace_process_test_or_event(self):
        node=self.add();events=self.count('events')
        before=set(self.workspaces.rglob('*'))
        for _ in range(3):
            snap=self.pool.runtime.preview(node)
            self.assertEqual(snap['state'],'not_connected')
            self.assertTrue(snap['read_only'])
        self.assertEqual(self.calls(),[])
        self.assertEqual(set(self.workspaces.rglob('*')),before)
        self.assertEqual(self.count('events'),events)
        self.assertFalse(self.node(node)['readiness']['ready'])

    def test_preview_uses_only_existing_adapter_not_factory(self):
        self.assertFalse(self.pool.runtime.preview('fixture-peer')['supported'])
        self.assertIsNone(self.backends.peek_resource('fixture-peer'))

    def test_live_snapshot_does_not_take_execution_lock_or_consume_result(self):
        node=self.add();hold=self.root/'hold';hold.touch()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future=executor.submit(self.probe,node)
            deadline=time.monotonic()+5
            while len(self.calls())<2 and time.monotonic()<deadline:time.sleep(.02)
            snap=self.pool.runtime.preview(node)
            self.assertEqual(snap['state'],'busy')
            self.assertIn('TEST fixture',snap['text'])
            # Stop observing entirely while the owned child finishes.
            session=snap['session_id'];hold.unlink()
            result=future.result(5)
        self.assertEqual(result['status'],'passed',result)
        self.assertEqual(self.pool.runtime.preview(node)['session_id'],session)
        self.assertEqual(self.calls()[0]['pid'],self.calls()[1]['pid'])
        self.assertTrue(self.node(node)['readiness']['ready'])

    def test_native_plan_prompt_is_ready_and_second_turn_reuses_process(self):
        (self.root/'plan-prompt').touch();node=self.add()
        self.assertEqual(self.probe(node)['status'],'passed')
        self.assertEqual(self.probe(node,'TEST-second')['status'],'passed')
        self.assertEqual(len({row['pid'] for row in self.calls()}),1)
        self.assertEqual(self.pool.runtime.preview(node)['turn'],2)

    def test_fixed_named_workspace_and_receipts_are_verifiable_and_not_approval(self):
        node=self.add();self.assertEqual(self.probe(node)['status'],'passed')
        adapter=self.backends.peek_resource(node);root=self.workspaces/'用户自定名字'
        self.assertEqual(self.calls()[0]['cwd'],str(root.resolve()))
        self.assertEqual(adapter.workspace.verify()[1],2)
        records=list((root/'任务记录').iterdir());self.assertEqual(len(records),1)
        receipt=json.loads(next(records[0].glob('执行回执-*.json')).read_text())
        self.assertFalse(receipt['user_delivery_accepted']);self.assertFalse(receipt['experience_approved'])
        self.assertEqual(receipt['status'],'execution_completed')
        self.assertEqual(self.count('memory_reviews'),1)  # Authored fixture setup only.
        self.backends.close();self.assertTrue(root.is_dir());self.assertTrue((root/'记录.jsonl').is_file())

    def test_tampered_task_file_blocks_next_dispatch_and_closes_owned_process(self):
        node=self.add();self.probe(node);adapter=self.backends.peek_resource(node)
        card=next((adapter.workspace.path/'任务记录').glob('*/任务卡.json'));card.write_text('TEST changed')
        result=self.probe(node,'TEST-next')
        self.assertEqual(result['reason'],'workspace_journal_invalid',result)
        self.assertEqual([r['event'] for r in self.calls()],['start','turn'])
        self.assertIsNone(adapter._process)

    def test_conflicting_existing_directory_is_not_adopted_or_overwritten(self):
        node=self.add();path=self.workspaces/'用户自定名字';path.mkdir();file=path/'用户材料';file.write_text('TEST keep')
        result=self.probe(node)
        self.assertEqual(result['reason'],'workspace_conflict',result)
        self.assertEqual(file.read_text(),'TEST keep');self.assertEqual(self.calls(),[])

    def test_second_owner_cannot_start_another_session_in_same_workspace(self):
        node=self.add();self.probe(node)
        other=AgyInteractiveResource(self.source(node),self.workspaces)
        with self.assertRaises(WorkspaceError) as error:other.test_resource({},'TEST-other')
        self.assertEqual(error.exception.code,'workspace_session_busy')
        self.assertEqual(len({r['pid'] for r in self.calls()}),1)

    def test_directory_traversal_and_symlinks_are_rejected(self):
        node=self.add();source=self.source(node);source['node']['name']='../escape'
        with self.assertRaises(WorkspaceError):AgentWorkspace(self.workspaces,source).ensure()
        source['node']['name']='linked';(self.workspaces/'linked').symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(WorkspaceError):AgentWorkspace(self.workspaces,source).ensure()
