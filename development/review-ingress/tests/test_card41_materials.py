"""卡 41 反例：审查材料进节点工作区 + 失败分类。

反例必须先在修复前的代码上跑出失败。假 CLI 只按派工卡列出的材料副本读文件
（模拟“只能读工作区”的无人值守节点）；未声明的工作区外读取照卡 40 用例 B 的
原文模拟成权限拒绝（exit 0、result=SUCCESS、无回执）。
"""
import hashlib
import json
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import test_agy_cli
from xiaoba_review.agent_workspace import WorkspaceError
from xiaoba_review.resource_agy import InteractiveError


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# 卡 40 summary-B.json 原文（stderr_excerpt 与 step_update 错误文本，逐字取用）。
SUMMARY_B_STDERR = ('jetski: no output produced — a tool required the "read_file" '
    'permission that headless mode cannot prompt for, so it was auto-denied. '
    'Add an allow-rule under permissions.allow in settings.json (e.g. read_file(<target>)). ')
SUMMARY_B_ERROR = ('permission check failed for read_file "[redacted]私人助理/工作区/'
    '小八AgentOS-v1.6.6-发版/reports/card40/夹具-外部文件-b.txt": user denied permission '
    'for read_file([redacted]私人助理/工作区/小八AgentOS-v1.6.6-发版/r')

PERMISSION_MESSAGE = ('节点读工作区外的文件或要跑命令被权限系统拒绝，'
    '请把文件作为材料提交，或走 tmux 在场通道。')


class Card41Fixture(test_agy_cli.CliFixture):
    """在既有假 agy 上注入卡 41 行为（flag 不落盘时对旧测试零影响）。"""

    def setUp(self):
        super().setUp()
        script = self.entry.read_text()
        extra = textwrap.dedent(r'''
            if flag('summary-b-permission'):
                sys.stdout.write(__SUMMARY_B_ERROR__+'\n')
                sys.stderr.write(__SUMMARY_B_STDERR__+'\n')
                print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'TEST'}}))
                sys.exit(0)
            if flag('sigterm-exit'):
                os.kill(os.getpid(),15)
            if flag('sigkill-exit'):
                os.kill(os.getpid(),9)
            if flag('read-staged'):
                staged=[]
                if '## 材料副本（只读）' in card:
                    for line in card.split('## 材料副本（只读）\n',1)[1].splitlines():
                        if line.startswith('- '):staged.append(line[2:].strip())
                declared=[]
                for item in (context.get('material_files') or []):
                    if isinstance(item,dict) and item.get('path'):declared.append(item['path'])
                for item in (context.get('evidence_refs') or []):
                    ref=item.get('ref') if isinstance(item,dict) else None
                    if ref and os.path.isabs(ref):declared.append(ref)
                if declared and not staged:
                    sys.stdout.write(__SUMMARY_B_ERROR__+'\n')
                    sys.stderr.write(__SUMMARY_B_STDERR__+'\n')
                    print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'TEST'}}))
                    sys.exit(0)
                parts=[]
                for p in staged:
                    data=Path(p).read_text()
                    parts.append('MATERIAL:'+data.strip())
                    try:
                        with open(p,'w') as writer:writer.write('X')
                        parts.append('WRITABLE')
                    except OSError:
                        parts.append('READONLY_OK')
                body='\n'.join(parts)+'\n'+body
        ''').replace('__SUMMARY_B_STDERR__', repr(SUMMARY_B_STDERR)).replace(
            '__SUMMARY_B_ERROR__', repr(SUMMARY_B_ERROR))
        anchor = "if flag('long-reply'):"
        assert anchor in script, '假 agy 锚点变了'
        linger = "if flag('linger-after-receipt'):"
        assert linger in script, '假 agy 锚点变了'
        script = script.replace(linger, "if flag('rewrite-after-done'):\n"
            "    time.sleep(.3)\n"
            "    text=logpath.read_text()\n"
            "    logpath.write_text(text.replace('\\n<!-- done:','\\nTEST 补充说明\\n<!-- done:',1))\n" + linger, 1)
        append = "with logpath.open('a') as out:"
        assert script.count(append) == 1, '假 agy 锚点变了'
        script = script.replace(append, "if flag('progress-in-block') or flag('progress-then-exit'):\n"
            "    base=logpath.read_text()\n"
            "    logpath.write_text(base+'\\n<!-- task:'+nonce+' -->\\nTEST 进度\\n<!-- done:'+nonce+' -->\\n')\n"
            "    if flag('progress-then-exit'):sys.exit(0)\n"
            "    time.sleep(.3);logpath.write_text(base)\n" + append, 1)
        self.entry.write_text(script.replace(anchor, extra + anchor, 1))
        self.materials = self.root / 'materials'
        self.materials.mkdir()

    def material(self, name, content):
        path = self.materials / name
        path.write_text(content)
        return path

    def run_task(self, node, declared, task='TASK-C41', step='WORK-01', name='用户自定名字'):
        adapter = self.adapter(node)
        return adapter.run_task(task, step, 'TEST 材料进节点工作区',
            {'material_files': declared, 'goal': 'TEST card41'})


class MaterialStagingTests(Card41Fixture, unittest.TestCase):
    def test_declared_external_material_is_staged_and_readable_in_work_task(self):
        (self.root / 'read-staged').touch()
        source = self.material('外部材料.txt', '外部内容-卡41')
        node = self.add()
        result = self.run_task(node, [{'path': str(source), 'sha256': _digest(source)}])
        self.assertEqual(result['status'], 'completed')
        self.assertIn('MATERIAL:外部内容-卡41', result['body'])
        copies = list((self.workspaces / '用户自定名字').rglob('材料/外部材料.txt'))
        self.assertEqual(len(copies), 1)

    def test_declared_material_via_review_card_evidence_refs(self):
        (self.root / 'read-staged').touch()
        source = self.material('审查材料.txt', '审查内容-卡41')
        node = self.add()
        adapter = self.adapter(node)
        card = {'assignment_id': 'ASSIGN-C41', 'review_request_id': 'REQ-C41',
                'step_code': 'REVIEW-01',
                'evidence_refs': [{'ref': str(source), 'sha256': _digest(source)}]}
        value = adapter.run(card)
        self.assertEqual(value['verdict'], 'PASS')
        self.assertIn('MATERIAL:审查内容-卡41', value['findings'])

    def test_frozen_hash_mismatch_refuses_dispatch_without_calling_cli(self):
        source = self.material('被改材料.txt', '冻结后被改动的内容')
        node = self.add()
        with self.assertRaises(WorkspaceError) as error:
            self.run_task(node, [{'path': str(source), 'sha256': 'a' * 64}])
        self.assertEqual(error.exception.code, 'material_hash_mismatch')
        self.assertIn('SHA-256', error.exception.user_message)
        self.assertEqual(self.starts(), [])

    def test_symlink_material_refuses_dispatch(self):
        source = self.material('真实材料.txt', '真身内容')
        link = self.root / '链接材料.txt'
        link.symlink_to(source)
        node = self.add()
        with self.assertRaises(WorkspaceError) as error:
            self.run_task(node, [{'path': str(link), 'sha256': _digest(source)}])
        self.assertEqual(error.exception.code, 'material_symlink')
        self.assertEqual(self.starts(), [])

    def test_symlink_inside_directory_material_refuses_dispatch(self):
        directory = self.root / '目录材料'
        directory.mkdir()
        (directory / 'ok.txt').write_text('ok')
        (directory / 'bad.txt').symlink_to(self.material('真身.txt', 'x'))
        node = self.add()
        with self.assertRaises(WorkspaceError) as error:
            self.run_task(node, [{'path': str(directory), 'sha256': 'a' * 64}])
        self.assertEqual(error.exception.code, 'material_symlink')
        self.assertEqual(self.starts(), [])

    def test_directory_material_over_file_limit_refuses_dispatch(self):
        directory = self.root / '大目录'
        directory.mkdir()
        for index in range(5):
            (directory / ('f%d.txt' % index)).write_text('x')
        node = self.add()
        adapter = self.adapter(node)
        adapter.material_file_limit = 3
        with self.assertRaises(WorkspaceError) as error:
            adapter.run_task('TASK-C41', 'WORK-01', 'TEST',
                {'material_files': [{'path': str(directory), 'sha256': 'a' * 64}]})
        self.assertEqual(error.exception.code, 'material_limit')
        self.assertIn('上限', error.exception.user_message)
        self.assertEqual(self.starts(), [])

    def test_material_over_byte_limit_refuses_dispatch(self):
        source = self.material('大文件.txt', 'x' * 50)
        node = self.add()
        adapter = self.adapter(node)
        adapter.material_byte_limit = 10
        with self.assertRaises(WorkspaceError) as error:
            adapter.run_task('TASK-C41', 'WORK-01', 'TEST',
                {'material_files': [{'path': str(source), 'sha256': _digest(source)}]})
        self.assertEqual(error.exception.code, 'material_limit')
        self.assertEqual(self.starts(), [])

    def test_credential_material_refuses_dispatch_instead_of_skipping(self):
        source = self.material('登录凭据.txt', 'password = FakePass1234567890')
        node = self.add()
        with self.assertRaises(WorkspaceError) as error:
            self.run_task(node, [{'path': str(source), 'sha256': _digest(source)}])
        self.assertEqual(error.exception.code, 'material_sensitive')
        self.assertEqual(self.starts(), [])

    def test_staged_copies_are_readonly_and_source_stays_unchanged(self):
        (self.root / 'read-staged').touch()
        source = self.material('只读材料.txt', '源内容不变')
        before = source.read_bytes()
        node = self.add()
        result = self.run_task(node, [{'path': str(source), 'sha256': _digest(source)}])
        self.assertIn('READONLY_OK', result['body'])
        self.assertNotIn('WRITABLE', result['body'])
        copies = list((self.workspaces / '用户自定名字').rglob('材料/只读材料.txt'))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].stat().st_mode & 0o222, 0)
        self.assertEqual(source.read_bytes(), before)

    def test_two_nodes_get_independent_copies(self):
        (self.root / 'read-staged').touch()
        source = self.material('共享材料.txt', '共享内容')
        declared = [{'path': str(source), 'sha256': _digest(source)}]
        first = self.add(node_id='TEST-cli-1', name='节点甲')
        second = self.add(node_id='TEST-cli-2', name='节点乙')
        one = self.run_task(first, declared, task='TASK-A')
        two = self.run_task(second, declared, task='TASK-B')
        self.assertIn('MATERIAL:共享内容', one['body'])
        self.assertIn('MATERIAL:共享内容', two['body'])
        copy_one = list((self.workspaces / '节点甲').rglob('材料/共享材料.txt'))
        copy_two = list((self.workspaces / '节点乙').rglob('材料/共享材料.txt'))
        self.assertEqual(len(copy_one), 1)
        self.assertEqual(len(copy_two), 1)
        self.assertNotEqual(copy_one[0], copy_two[0])

    def test_work_replay_with_materials_does_not_call_cli_again(self):
        (self.root / 'read-staged').touch()
        source = self.material('重放材料.txt', '重放内容')
        declared = [{'path': str(source), 'sha256': _digest(source)}]
        node = self.add()
        first = self.run_task(node, declared, task='TASK-REPLAY')
        calls = len(self.calls())
        second = self.run_task(node, declared, task='TASK-REPLAY')
        self.assertEqual(first['body'], second['body'])
        self.assertEqual(len(self.calls()), calls)
        self.assertTrue(second['execution_evidence']['replayed'])


class FailureClassificationTests(Card41Fixture, unittest.TestCase):
    def test_sigterm_exit_is_call_terminated_not_permission(self):
        (self.root / 'sigterm-exit').touch()
        node = self.add()
        result = self.probe(node, 'TEST-sigterm')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'call_terminated')
        self.assertIn('调用被结束', result.get('message', ''))
        self.assertNotIn('被系统拒绝执行', result.get('message', ''))
        self.assertEqual(len(self.starts()), 1)

    def test_sigkill_exit_is_call_terminated(self):
        (self.root / 'sigkill-exit').touch()
        node = self.add()
        result = self.probe(node, 'TEST-sigkill')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'call_terminated')
        self.assertIn('调用被结束', result.get('message', ''))
        self.assertNotIn('被系统拒绝执行', result.get('message', ''))

    def test_summary_b_permission_markers_beat_receipt_missing(self):
        (self.root / 'summary-b-permission').touch()
        node = self.add()
        result = self.probe(node, 'TEST-summary-b')
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'permission_denied')
        self.assertEqual(result['message'], PERMISSION_MESSAGE)
        stored = json.loads(self.receipts()[-1].read_text())
        text = json.dumps(stored, ensure_ascii=False)
        for leaked in ('permission check failed', 'auto-denied', 'jetski',
                       'cannot prompt', '夹具-外部文件'):
            self.assertNotIn(leaked, text)
        self.assertEqual(len(self.starts()), 1)

    def test_summary_b_markers_classify_every_cli_via_specific_failure(self):
        from xiaoba_review.cli_identity import specific_failure
        failure = specific_failure('codex', 'Codex', 'm', SUMMARY_B_ERROR, SUMMARY_B_STDERR)
        self.assertIsNotNone(failure)
        self.assertEqual(failure.code, 'permission_denied')
        self.assertEqual(failure.user_message, PERMISSION_MESSAGE)

    def test_sigterm_returncode_is_classified_by_specific_failure_too(self):
        from xiaoba_review.cli_identity import specific_failure
        failure = specific_failure('codex', 'Codex', 'm', '', '', returncode=-15)
        self.assertIsNotNone(failure)
        self.assertEqual(failure.code, 'call_terminated')
        self.assertIn('调用被结束', failure.user_message)
        self.assertNotIn('被系统拒绝执行', failure.user_message)


class MaterialReviewPassTests(Card41Fixture, unittest.TestCase):
    """核对卡 41 时补的反例：真实源码目录能当材料，复制后校验，重派可复用。"""

    def declared(self, source):
        from xiaoba_agentos.task_context import material_digest
        return [{'path': str(source), 'sha256': material_digest(source)}]

    def test_source_tree_with_login_module_and_fake_keys_is_dispatched(self):
        tree = self.materials / 'source'
        tree.mkdir()
        (tree / 'workbuddy_login.py').write_text('def login(): pass\n')
        (tree / 'test_fake.py').write_text("KEY = 'sk-abcdefghijklmnop1234'\n")
        (tree / 'change.diff').write_text("+password = 'FakePass1234567890'\n")
        node = self.add()
        try:
            self.run_task(node, self.declared(tree))
        except WorkspaceError as error:
            self.fail(error.code)
        self.assertTrue(self.starts())

    def test_private_key_inside_code_file_is_still_refused(self):
        source = self.material('leak.py', '-----BEGIN ' + 'PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEF\n')
        node = self.add()
        with self.assertRaises(WorkspaceError) as error:
            self.run_task(node, self.declared(source))
        self.assertEqual(error.exception.code, 'material_sensitive')
        self.assertEqual(self.starts(), [])

    def test_secret_after_first_64k_of_document_is_refused(self):
        source = self.material('说明.md', 'x\n' * 40000 + 'password = FakePass1234567890\n')
        node = self.add()
        with self.assertRaises(WorkspaceError) as error:
            self.run_task(node, self.declared(source))
        self.assertEqual(error.exception.code, 'material_sensitive')
        self.assertEqual(self.starts(), [])

    def test_copy_that_differs_from_frozen_hash_refuses_dispatch(self):
        source = self.material('材料.txt', '原文')
        declared = self.declared(source)
        node = self.add()

        def corrupt(_source, target):
            Path(target).write_text('被换掉')

        with mock.patch('xiaoba_review.resource_cli.shutil.copy2', corrupt):
            with self.assertRaises(WorkspaceError) as error:
                self.run_task(node, declared)
        self.assertEqual(error.exception.code, 'material_hash_mismatch')
        self.assertEqual(self.starts(), [])

    def test_staging_same_record_again_reuses_verified_copy(self):
        source = self.material('材料.txt', '原文')
        materials = {'material_files': self.declared(source)}
        adapter = self.adapter(self.add())
        record = adapter.workspace.begin('work', 'R-41', materials)
        first = adapter._stage_materials(record, materials)
        second = adapter._stage_materials(record, materials)
        self.assertEqual(first, second)
        self.assertEqual(first[0].read_text(), '原文')

    def test_output_over_one_mib_is_not_stopped_by_default_limit(self):
        # 流式 CLI 会带上读过的文件内容。假 CLI 输出约 2 MiB 后不再有进展：
        # 默认上限下应由无进展时限结束；test_stdout_flood 用 1 MiB 上限证明同样输出会撞线。
        (self.root / 'stdout-flood').touch()
        adapter = self.adapter(self.add())
        adapter.no_progress_seconds = 1
        with self.assertRaises(InteractiveError) as error:
            adapter.test_resource({}, 'TEST-challenge')
        self.assertEqual(error.exception.code, 'cli_no_progress')

    def test_node_rewriting_its_receipt_before_exit_does_not_brick_workspace(self):
        # 第八轮：Qoder 写完 done 行后又改了回执正文，下一次派活整个节点被判记录链损坏。
        (self.root / 'rewrite-after-done').touch()
        adapter = self.adapter(self.add())
        result = adapter.run_task('TASK-C41', 'WORK-01', 'TEST 回执重写', {'goal': 'TEST card41'})
        self.assertIn('TEST 补充说明', result['body'])
        adapter.workspace.verify()

    def test_health_probe_with_rewritten_body_fails_without_bricking_workspace(self):
        (self.root / 'rewrite-after-done').touch()
        adapter = self.adapter(self.add())
        with self.assertRaises(InteractiveError) as error:
            adapter.test_resource({}, 'TEST-challenge')
        self.assertEqual(error.exception.code, 'challenge_mismatch')
        adapter.workspace.verify()

    def test_malformed_block_while_running_is_not_final(self):
        # 第九轮：Qoder 把进度写进回执块并写了 done，小八当场判回执无效，审查还没开始就结束。
        (self.root / 'progress-in-block').touch()
        adapter = self.adapter(self.add())
        result = adapter.run_task('TASK-C41', 'WORK-01', 'TEST 进度块', {'goal': 'TEST card41'})
        self.assertEqual(result['status'], 'completed')
        adapter.workspace.verify()

    def test_malformed_block_left_at_exit_is_still_refused(self):
        (self.root / 'progress-then-exit').touch()
        adapter = self.adapter(self.add())
        with self.assertRaises(WorkspaceError) as error:
            adapter.run_task('TASK-C41', 'WORK-01', 'TEST 进度块', {'goal': 'TEST card41'})
        self.assertEqual(error.exception.code, 'worklog_receipt_invalid')

    def test_ticket_says_progress_stays_outside_receipt_block(self):
        self.run_task(self.add(), [])
        tickets = [path.read_text() for path in self.root.rglob('*.md')
                   if path.read_text(errors='replace').startswith('# 本次工作任务卡')]
        self.assertTrue(tickets)
        for text in tickets:
            self.assertIn('进度写在回执块之外', text)

    def test_ticket_tells_node_commands_will_end_the_turn(self):
        source = self.material('材料.txt', '原文')
        self.run_task(self.add(), self.declared(source))
        tickets = [path.read_text() for path in self.root.rglob('*') if path.is_file()
                   and path.suffix == '.md' and path.read_text(errors='replace').startswith('# 本次工作任务卡')
                   and '## 材料副本（只读）' in path.read_text(errors='replace')]
        self.assertTrue(tickets)
        for text in tickets:
            self.assertIn('只用文件读取工具', text)
            self.assertIn('不要抄录密钥', text)

    def test_ticket_lists_only_local_copy_not_external_path(self):
        source = self.material('材料.txt', '原文')
        node = self.add()
        self.run_task(node, self.declared(source))
        tickets = [path.read_text() for path in self.root.rglob('*') if path.is_file()
                   and path.suffix in ('', '.md') and '# 本次工作任务卡' in path.read_text(errors='replace')]
        self.assertTrue(tickets)
        for text in tickets:
            self.assertNotIn(str(source), text)
            self.assertIn('## 材料副本（只读）', text)


if __name__ == '__main__':
    unittest.main()
