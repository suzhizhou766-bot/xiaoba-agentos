import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures_review import _config
from xiaoba_agentos import retained as retained_objects
from xiaoba_agentos.memory import MemoryBridge
from xiaoba_agentos.review_flow import execute_review
from xiaoba_agentos.store import EventStore


class PublicationRegressionsTest(unittest.TestCase):
    def fixture(self, root):
        config = _config(root)
        config.raw['project_dir'] = str(Path(__file__).resolve().parents[1])
        vault = root / 'vault'
        config.raw['memory'].update({
            'publish_enabled': True, 'obsidian_subdir': '70_复利规则/经验',
            'catalog_note': '70_复利规则/经验索引.md',
            'knowlp_graph_dir': str(root / 'runtime/graph'),
        })
        catalog = vault / '70_复利规则/经验索引.md'
        catalog.parent.mkdir(parents=True)
        (vault / 'root.md').write_text(
            '---\nid: "root"\nparent: ""\nstatus: "active"\n---\n\n'
            '# Root\n\n- [[70_复利规则/经验索引]]\n')
        catalog.write_text(
            '---\nid: "experiences"\nparent: "root"\nstatus: "active"\n---\n\n# 经验索引\n')
        store = EventStore(config.database_path)
        store.initialize()
        bridge = MemoryBridge(config, store)
        return config, store, bridge, catalog

    def test_headingless_authored_title_survives_both_reviews_and_indexing(self):
        with tempfile.TemporaryDirectory() as directory:
            config, store, bridge, catalog = self.fixture(Path(directory))
            title = '工作搬家要在旧位置留指路，登记页写明版本号'
            body = '搬家后在旧位置留指路；登记页写明版本号。' * 8
            result = store.create_authored_candidate(title, body, bridge.outbox, author='test')
            task_id = result['task_id']
            staged = Path(result['staged_path'])
            self.assertIn('title: ' + json.dumps(title, ensure_ascii=False), staged.read_text())
            self.assertEqual(result['evidence_sha256'], hashlib.sha256(staged.read_bytes()).hexdigest())
            for stage in ('delivery', 'final'):
                code, receipt = execute_review(config, store, task_id, stage, 'approve', '')
                self.assertEqual(0, code, receipt)
            row = store.get_memory_candidate(task_id)
            note = Path(row['published_path'])
            self.assertEqual(title + '-' + task_id[:8] + '.md', note.name)
            self.assertIn('# ' + title + '\n\n' + body, note.read_text())
            self.assertEqual(1, catalog.read_text().count(note.stem))
            index = json.loads((config.runtime_dir / 'graph/meta_index.json').read_text())
            entry = next(item for item in index if item['name'] == note.stem)
            self.assertIn(title, entry['headings'])
            self.assertEqual('published', row['status'])

    def test_changing_only_candidate_title_invalidates_final_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            config, store, bridge, _ = self.fixture(Path(directory))
            result = store.create_authored_candidate('原题', '正文', bridge.outbox, author='test')
            task_id = result['task_id']
            self.assertEqual(0, execute_review(config, store, task_id, 'delivery', 'approve', '')[0])
            staged = Path(result['staged_path'])
            staged.write_text(staged.read_text().replace('title: "原题"', 'title: "改题"'))
            code, receipt = execute_review(config, store, task_id, 'final', 'approve', '')
            self.assertNotEqual(0, code, receipt)
            self.assertNotEqual('published', store.get_memory_candidate(task_id)['status'])

    def test_publication_creates_no_backup_and_keeps_existing_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            config, store, bridge, _ = self.fixture(Path(directory))
            existing = config.runtime_dir / 'backups/already-present.txt'
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b'user-owned backup')
            before = {str(p.relative_to(existing.parent)): p.read_bytes()
                      for p in existing.parent.rglob('*') if p.is_file()}
            result = store.create_authored_candidate('标题', '正文', bridge.outbox, author='test')
            bridge.publish_candidate(result['task_id'], result['evidence_sha256'])
            after = {str(p.relative_to(existing.parent)): p.read_bytes()
                     for p in existing.parent.rglob('*') if p.is_file()}
            self.assertEqual(before, after)

    def test_publication_failure_restores_catalog_without_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            config, store, bridge, catalog = self.fixture(Path(directory))
            result = store.create_authored_candidate('标题', '正文', bridge.outbox, author='test')
            original = catalog.read_bytes()
            with patch.object(bridge, '_rebuild_and_verify_index', side_effect=RuntimeError('index failed')):
                with self.assertRaisesRegex(
                    RuntimeError, 'publication failed and could not be fully rolled back'
                ):
                    bridge.publish_candidate(result['task_id'], result['evidence_sha256'])
            # 受限方案丙：活路径逐字节还原、不建备份目录、证据留在原地；
            # 代价是被认领的原对象以保留物留下，自检必须列出来等人工核对。
            self.assertEqual(original, catalog.read_bytes())
            self.assertFalse((config.runtime_dir / 'backups').exists())
            notes = Path(config.raw['memory']['obsidian_vault']) / '70_复利规则/经验'
            self.assertEqual([], list(notes.glob('*.md')))
            self.assertTrue(Path(result['staged_path']).exists())
            report = retained_objects.collect(config)
            self.assertFalse(report['ok'], report)
            self.assertTrue(report['manual_action_required'], report)
            self.assertEqual(2, report['retained_count'], report)
            self.assertEqual(
                {'memory.note', 'memory.catalog'},
                {item['operation'] for item in report['objects']},
                report,
            )

    def test_index_commit_failure_restores_previous_bytes_and_permissions_in_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _, bridge, _ = self.fixture(Path(directory))
            graph = config.runtime_dir / 'graph'
            graph.mkdir(parents=True)
            previous = graph / 'meta_index.json'
            previous.write_bytes(b'old index bytes')
            previous.chmod(0o640)
            real_run = subprocess.run
            seen = []

            def fail_commit(argv, **kwargs):
                if '--verify' in argv and argv[argv.index('--graph-dir') + 1] == str(graph):
                    seen.extend(str(p) for p in config.runtime_dir.rglob('*'))
                    return subprocess.CompletedProcess(argv, 1, '', 'commit verification failed')
                return real_run(argv, **kwargs)

            with patch('xiaoba_agentos.memory.subprocess.run', side_effect=fail_commit):
                # 回滚后仍有保留物：不得把这次失败说成完全回滚成功。
                with self.assertRaisesRegex(
                    RuntimeError, 'index installation failed and could not be fully rolled back'
                ):
                    bridge._rebuild_and_verify_index(Path(config.raw['memory']['obsidian_vault']))
            # 活文件逐字节还原（含权限）。
            self.assertEqual(b'old index bytes', previous.read_bytes())
            self.assertEqual(0o640, previous.stat().st_mode & 0o777)
            # 受限方案丙：这次调用换掉的每个图输出都留在私有名字下，一个也不删。
            kept = sorted(
                (p for p in graph.iterdir() if p.name != previous.name), key=lambda p: p.name
            )
            self.assertTrue(kept, sorted(p.name for p in graph.iterdir()))
            self.assertTrue(
                all(retained_objects.is_retained_name(p.name) for p in kept),
                sorted(p.name for p in graph.iterdir()),
            )
            by_original = {
                retained_objects.parse_retained_name(p.name)['original']: p for p in kept
            }
            self.assertEqual(
                {'meta_index.json', 'dual_graph.json', 'deep_extraction_prep.json'},
                set(by_original),
                sorted(p.name for p in graph.iterdir()),
            )
            # meta_index.json 原本就有内容：本次写入的那份新字节留在保留物里，
            # 旧字节放回活路径，两者必须是不同对象（不是复制出来的一份）。
            self.assertNotEqual(
                previous.stat().st_ino, by_original['meta_index.json'].stat().st_ino
            )
            self.assertIsInstance(json.loads(by_original['meta_index.json'].read_text()), list)
            for name in ('dual_graph.json', 'deep_extraction_prep.json'):
                self.assertTrue(by_original[name].read_bytes(), name)
            report = retained_objects.collect(config)
            self.assertFalse(report['ok'], report)
            self.assertTrue(report['enumeration_ok'], report)
            self.assertEqual(len(kept), report['retained_count'], report)
            self.assertEqual(
                {'memory.index_output'},
                {item['operation'] for item in report['objects']},
                report,
            )
            self.assertFalse(any('previous-graph' in p or '/backups/' in p for p in seen))


if __name__ == '__main__':
    unittest.main()
