"""构包边界：不带入软链指向的包外内容、中断不留半包、schema 版本如实。

夹具全在临时目录里，run_verify 由替身接管：不做真实构包，不调用模型，不碰发布目录。
"""
import argparse
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('build_release_for_boundary', ROOT / 'scripts/build_release.py')
build_release = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(build_release)

VERSION = 'v9.0.0'
STAGING_MARK = '.staging-'
_SHA256 = re.compile(r'^[0-9a-f]{64}$')


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


class Fixture:
    """一棵能跑通构包的最小源码树，外加包外的软链目标。"""

    def __init__(self, root: Path, schema: str = '7'):
        self.root = root
        self.source = root / 'candidate'
        for name in build_release.SOURCE_DIRECTORIES:
            (self.source / name).mkdir(parents=True, exist_ok=True)
        for name in build_release.SOURCE_ROOT_FILES:
            write(self.source / name, name + '\n')
        write(self.source / 'pyproject.toml', '[project]\nname = "fixture"\nversion = "9.0.0"\n')
        write(self.source / 'SOURCE_MANIFEST.json', json.dumps(
            {'algorithm': 'sha256', 'files': {'src/xiaoba_agentos/store.py': '0' * 64}},
            ensure_ascii=False, indent=2) + '\n')
        write(self.source / 'src/xiaoba_agentos/store.py',
              f'class EventStore:\n    DATABASE_SCHEMA_VERSION = {schema}\n')

        self.interface = write(root / 'interface-card' / 'SKILL.md', 'card\n')
        self.stable = root / 'stable'
        for name in ('xiaoba', 'verify-release'):
            write(self.stable / name, '#!/bin/sh\n')
        self.brain = root / 'second-brain-source'
        write(self.brain / '00_root.md', 'brain\n')
        self.published = root / 'published'
        self.published.mkdir(parents=True, exist_ok=True)

        self.outside = root / 'outside'
        write(self.outside / 'secret.md', 'content that must never enter the package\n')
        write(self.outside / 'dir' / 'inner.md', 'inner\n')

        self.target = root / 'releases' / VERSION

    def args(self, **extra) -> argparse.Namespace:
        values = dict(
            agentos_root=self.source, interface=self.interface, stable_scripts=self.stable,
            second_brain=self.brain, published_agentos_root=self.published, target=self.target,
            version=VERSION, verified_date='2026-10-31', test_count=1, test_result='not_run',
            acceptance_complete=False, acceptance_evidence=None, acceptance_database=None,
            acceptance_task_id=None, previous_release=None, known_external_issue=[],
        )
        values.update(extra)
        return argparse.Namespace(**values)

    def staging_leftovers(self) -> list[str]:
        parent = self.target.parent
        if not parent.exists():
            return []
        return sorted(path.name for path in parent.iterdir() if STAGING_MARK in path.name)

    def release(self) -> dict:
        return json.loads((self.target / 'RELEASE.json').read_text(encoding='utf-8'))


class BuildBoundaryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fx = Fixture(Path(temp.name).resolve())

    def build(self, **extra):
        with patch.object(build_release, 'run_verify'):
            return build_release.build(self.fx.args(**extra))

    def test_normal_fixture_still_produces_the_same_package(self):
        result = self.build()
        self.assertTrue(result['ok'], result)
        self.assertEqual(str(self.fx.target), result['target'])
        self.assertEqual((VERSION, 1), (result['version'], result['source_files']))
        self.assertEqual(
            ['FILES.sha256', 'INTERFACE_MANIFEST.sha256', 'RELEASE.json',
             'SECOND_BRAIN_MANIFEST.sha256', 'SKILL.md', 'agentos', 'interface', 'second-brain'],
            sorted(path.name for path in self.fx.target.iterdir()))
        release = self.fx.release()
        self.assertEqual('release_candidate', release['status'])
        self.assertEqual(7, release['database_schema_version'])
        self.assertEqual((VERSION, 1, 1), (release['version'], release['source_files'],
                                           release['second_brain_markdown_files']))
        self.assertRegex(release['code_fingerprint'], _SHA256)
        self.assertNotIn('release_tier', release)
        listed = (self.fx.target / 'FILES.sha256').read_text(encoding='utf-8')
        self.assertIn('./RELEASE.json', listed)
        self.assertIn('./agentos/src/xiaoba_agentos/store.py', listed)
        self.assertNotIn('./FILES.sha256', listed)
        self.assertEqual(result['release_files'], sum(1 for _ in build_release.files(self.fx.target)))
        self.assertEqual([], self.fx.staging_leftovers())

    def test_symlink_at_any_depth_of_the_source_is_refused_and_named(self):
        package = self.fx.source / 'src' / 'xiaoba_agentos'
        shallow = self.fx.source / 'src' / 'link.md'
        shallow.symlink_to(self.fx.outside / 'secret.md')
        (package / 'nested').mkdir(parents=True)
        deep = package / 'nested' / 'deep.md'
        deep.symlink_to(self.fx.outside / 'secret.md')
        dirlink = self.fx.source / 'src' / 'dirlink'
        dirlink.symlink_to(self.fx.outside / 'dir')
        with self.assertRaises(RuntimeError) as caught:
            self.build()
        message = str(caught.exception)
        self.assertIn('release input contains symlink', message)
        for path in (shallow, deep, dirlink):
            self.assertIn(str(path), message)
        self.assertFalse(self.fx.target.exists())
        self.assertEqual([], self.fx.staging_leftovers())

    def test_symlink_in_any_packed_directory_is_refused(self):
        for relative in ('config/link.md', '.agents/skills/link.md', 'tests/link.md'):
            with self.subTest(relative=relative):
                link = self.fx.source / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(self.fx.outside / 'secret.md')
                try:
                    with self.assertRaisesRegex(RuntimeError, re.escape(str(link))):
                        self.build()
                    self.assertFalse(self.fx.target.exists())
                finally:
                    link.unlink()

    def test_symlinked_source_does_not_leak_outside_content_into_the_package(self):
        (self.fx.source / 'config' / 'link.md').symlink_to(self.fx.outside / 'secret.md')
        (self.fx.source / 'docs' / 'dirlink').symlink_to(self.fx.outside / 'dir')
        with self.assertRaises(RuntimeError):
            self.build()
        leaked = [path for path in self.fx.root.rglob('*')
                  if path.is_file() and not path.is_symlink()
                  and self.fx.outside not in path.parents
                  and 'must never enter' in path.read_text(encoding='utf-8')]
        self.assertEqual([], leaked)

    def test_symlink_in_the_second_brain_input_is_refused(self):
        (self.fx.brain / 'linked.md').symlink_to(self.fx.outside / 'secret.md')
        with self.assertRaisesRegex(RuntimeError, 'release input contains symlink'):
            self.build()
        self.assertFalse(self.fx.target.exists())
        self.assertEqual([], self.fx.staging_leftovers())

    def test_symlinked_interface_card_is_refused(self):
        link = self.fx.root / 'card-link.md'
        link.symlink_to(self.fx.interface)
        with self.assertRaisesRegex(RuntimeError, 'interface card unavailable'):
            self.build(interface=link)
        self.assertFalse(self.fx.target.exists())

    def test_interrupted_build_leaves_no_half_package_at_the_target(self):
        seen = {}

        def interrupt(command, cwd):
            staging = Path(cwd).parent
            seen['staging'] = staging
            seen['populated'] = (staging / 'agentos' / 'src' / 'xiaoba_agentos' / 'store.py').is_file()
            raise RuntimeError('verification interrupted')

        with patch.object(build_release, 'run_verify', side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, 'verification interrupted'):
                build_release.build(self.fx.args())
        self.assertTrue(seen['populated'], '反例要在源码树已经复制进暂存目录之后才中断')
        self.assertFalse(self.fx.target.exists(), '中断后目标位置不得出现半包')
        self.assertFalse(seen['staging'].exists(), '暂存目录要随失败一起清掉')
        self.assertEqual([], self.fx.staging_leftovers())

    def test_failed_build_after_a_successful_one_leaves_the_previous_package_alone(self):
        self.build()
        before = {path.relative_to(self.fx.target): build_release.sha256(path)
                  for path in build_release.files(self.fx.target)}
        (self.fx.source / 'config' / 'link.md').symlink_to(self.fx.outside / 'secret.md')
        with self.assertRaises(RuntimeError):
            self.build(target=self.fx.root / 'releases' / 'v9.0.1')
        self.assertEqual(before, {path.relative_to(self.fx.target): build_release.sha256(path)
                                  for path in build_release.files(self.fx.target)})
        self.assertEqual([], self.fx.staging_leftovers())

    def test_build_into_an_existing_empty_target_directory_still_works(self):
        self.fx.target.mkdir(parents=True)
        self.assertTrue(self.build()['ok'])
        self.assertTrue((self.fx.target / 'RELEASE.json').is_file())

    def test_non_empty_target_is_still_refused_without_touching_it(self):
        self.fx.target.mkdir(parents=True)
        write(self.fx.target / 'keep.json', 'keep\n')
        with self.assertRaisesRegex(RuntimeError, 'target must be absent or empty'):
            self.build()
        self.assertEqual(['keep.json'], sorted(path.name for path in self.fx.target.iterdir()))
        self.assertEqual([], self.fx.staging_leftovers())


class SchemaVersionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.fx = Fixture(self.root)

    def declared(self, schema: str) -> int:
        write(self.fx.source / 'src/xiaoba_agentos/store.py',
              f'class EventStore:\n    DATABASE_SCHEMA_VERSION = {schema}\n')
        return build_release.packaged_database_schema_version(self.fx.source)

    def test_declared_version_is_read_from_the_tree_being_packed(self):
        self.assertEqual(7, self.declared('7'))
        self.assertEqual(999, self.declared('999'))

    def test_unusable_declarations_are_refused(self):
        for schema in ('"7"', 'None', 'True', '0', '-1', 'SEVEN'):
            with self.subTest(schema=schema), self.assertRaises(RuntimeError):
                self.declared(schema)
        write(self.fx.source / 'src/xiaoba_agentos/store.py', 'class EventStore:\n    pass\n')
        with self.assertRaisesRegex(RuntimeError, 'does not declare a usable database schema'):
            build_release.packaged_database_schema_version(self.fx.source)

    def test_missing_schema_source_is_refused(self):
        (self.fx.source / 'src' / 'xiaoba_agentos' / 'store.py').unlink()
        with self.assertRaisesRegex(RuntimeError, 'database schema source is unavailable'):
            build_release.packaged_database_schema_version(self.fx.source)

    def test_source_schema_of_999_is_recorded_as_999_not_as_a_hardcoded_number(self):
        write(self.fx.source / 'src/xiaoba_agentos/store.py',
              'class EventStore:\n    DATABASE_SCHEMA_VERSION = 999\n')
        with patch.object(build_release, 'run_verify'):
            build_release.build(self.fx.args())
        self.assertEqual(999, self.fx.release()['database_schema_version'])

    def test_package_that_differs_from_its_own_source_is_refused(self):
        with patch.object(build_release, 'run_verify'), \
                patch.object(build_release, 'packaged_database_schema_version', side_effect=[9, 7]):
            with self.assertRaisesRegex(RuntimeError, 'differs from the source declaration'):
                build_release.build(self.fx.args())
        self.assertFalse(self.fx.target.exists())
        self.assertEqual([], self.fx.staging_leftovers())


if __name__ == '__main__':
    unittest.main()
