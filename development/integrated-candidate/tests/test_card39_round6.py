"""第六轮演练：切换后没有审查节点时，已发布技能的审查记录仍然有效。"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from xiaoba_agentos.content_index import review_problems
from xiaoba_agentos.memory import MemoryBridge
from xiaoba_agentos.task_context import freeze_materials


class DeclaredSymlinkTests(unittest.TestCase):
    # 第十一轮：提交时先解析路径，声明的符号链接被换成目标，“符号链接拒绝”没有生效。
    def test_declared_symlink_is_refused_at_submit(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'target.txt'
            target.write_text('TEST')
            link = Path(root) / 'link.txt'
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, '符号链接'):
                freeze_materials([str(link)])
            self.assertEqual(1, len(freeze_materials([str(target)])))


class PrivateKeyRuleTests(unittest.TestCase):
    # 第十轮：AGY 审查正文引用了规则里的 PEM 头，整票被当成泄露私钥拒收。
    HEADER = '-----BEGIN ' + 'PRIVATE KEY-----'

    def test_quoting_the_header_is_not_a_leak(self):
        self.assertIsNone(MemoryBridge._sensitive_reason('代码严格拦截私钥（`' + self.HEADER + '`）。'))

    def test_header_with_key_body_is_refused(self):
        body = 'MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7'
        self.assertEqual('private_key', MemoryBridge._sensitive_reason(self.HEADER + '\n' + body + '\n'))
        self.assertEqual('private_key', MemoryBridge._sensitive_reason(self.HEADER + '\r\n' + body))


class PublishedSkillReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / 'a.sqlite3'
        connection = sqlite3.connect(self.database)
        connection.executescript("""
            CREATE TABLE tasks (id TEXT, status TEXT, prompt TEXT);
            CREATE TABLE agent_runs (task_id TEXT, agent_name TEXT, status TEXT, output TEXT, created_at REAL);
        """)
        connection.execute("INSERT INTO tasks VALUES ('t','completed','PROMPT')")
        connection.commit()
        connection.close()

    def run_row(self, agent, output='FINAL_ACCEPTANCE: PASS', status='completed'):
        connection = sqlite3.connect(self.database)
        connection.execute('INSERT INTO agent_runs VALUES (?,?,?,?,?)', ('t', agent, status, output, 1.0))
        connection.commit()
        connection.close()

    def test_history_stays_valid_when_old_backends_are_no_longer_active(self):
        self.run_row('agy_review')
        self.run_row('dsh_review')
        self.assertEqual([], review_problems(self.database, 't', 'PROMPT'))

    def test_history_needs_two_passing_backends(self):
        self.run_row('agy_review')
        self.assertIn('通过审查的后台不足两个', review_problems(self.database, 't', 'PROMPT'))

    def test_history_refuses_a_failing_vote(self):
        self.run_row('agy_review')
        self.run_row('dsh_review', 'FINAL_ACCEPTANCE: FAIL')
        self.assertTrue(review_problems(self.database, 't', 'PROMPT'))

    def test_history_refuses_changed_content(self):
        self.run_row('agy_review')
        self.run_row('dsh_review')
        self.assertTrue(review_problems(self.database, 't', 'OTHER'))

    def test_new_publish_only_counts_current_nodes_and_not_all_must_run(self):
        self.run_row('node-a')
        self.run_row('node-b')
        self.assertEqual([], review_problems(self.database, 't', 'PROMPT', ['node-a', 'node-b', 'node-c']))
        self.assertTrue(review_problems(self.database, 't', 'PROMPT', ['node-a', 'node-c']))
        self.assertTrue(review_problems(self.database, 't', 'PROMPT', []))


if __name__ == '__main__':
    unittest.main()
