"""发布分档的门禁接入：档位由上一版包里的工具算，构包与切换预检各自重算核对。

这里的分档工具是写进临时“上一版”里的测试替身；分档规则本身的测试在 test_release_policy.py。
"""
import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_switch_release import Fixture, seal, switch, write

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import release_tier  # noqa: E402

_spec = importlib.util.spec_from_file_location('build_release_for_tier', ROOT / 'scripts/build_release.py')
build_release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_release)

STUB = '''import json
from pathlib import Path
def classify_release(baseline_root, candidate_root, *, rules_path=None, human_raise=None):
    answer = json.loads(Path(rules_path).read_text())
    if answer.get("crash"):
        raise ValueError("stub crash")
    changed = sorted(str(p.relative_to(candidate_root)) for p in Path(candidate_root).rglob("*.md"))
    return {"ok": answer.get("ok", True), "calculated_tier": answer.get("calculated", answer["tier"]),
            "effective_tier": answer["tier"], "problems": answer.get("problems", []),
            "changes": [{"path": p, "tier": answer["tier"]} for p in changed],
            "reasons": ["rules=" + Path(rules_path).parent.parent.name]}
'''


def tree(root, answer=None):
    write(root / 'SOURCE_MANIFEST.json', '{}')
    if answer is not None:
        write(root / release_tier.TOOL, STUB)
        write(root / release_tier.RULES, json.dumps(answer))
    return root


class ComputeTierTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def compute(self, answer, candidate_answer=None):
        previous = tree(self.root / 'previous/agentos', answer)
        candidate = tree(self.root / 'candidate/agentos', candidate_answer)
        write(candidate / 'docs/a.md', 'x')
        return release_tier.compute_tier(previous, candidate)

    def test_previous_release_without_the_tool_means_full_acceptance(self):
        record = self.compute(None, candidate_answer={'tier': 'patch'})
        self.assertEqual(record['tier'], 'full')
        self.assertNotIn('tool_sha256', record)

    def test_tier_comes_from_the_previous_release_tool_and_rules_not_the_candidate(self):
        record = self.compute({'tier': 'regular'}, candidate_answer={'tier': 'patch'})
        self.assertEqual((record['tier'], record['calculated_tier']), ('regular', 'regular'))
        self.assertEqual(record['reasons'], ['rules=agentos'])
        self.assertEqual(record['changed_paths']['regular'], ['docs/a.md'])
        self.assertEqual(record['tool_sha256'], switch.digest(STUB.encode()))

    def test_human_raise_is_kept_but_a_lowered_tier_is_refused(self):
        self.assertEqual(self.compute({'tier': 'migration', 'calculated': 'patch'})['tier'], 'migration')
        with self.assertRaises(release_tier.TierError):
            self.compute({'tier': 'patch', 'calculated': 'regular'})

    def test_refused_crashed_or_malformed_tool_results_never_yield_a_tier(self):
        for answer in ({'tier': 'patch', 'ok': False}, {'tier': 'patch', 'crash': True}, {'tier': 'light'}):
            with self.subTest(answer=answer), self.assertRaises(release_tier.TierError):
                self.compute(answer)

    def test_previous_release_directory_is_left_untouched(self):
        self.compute({'tier': 'patch'})
        self.assertEqual(list((self.root / 'previous').rglob('__pycache__')), [])

    def test_missing_previous_manifest_is_refused(self):
        with self.assertRaises(release_tier.TierError):
            release_tier.compute_tier(self.root / 'absent', self.root)


class SwitchTierGateTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fx = Fixture(Path(temp.name))
        self.previous = self.fx.layout.runtime / '.runtime/releases/v9.0.0'
        self.package = self.fx.layout.package

    def install_tool(self, answer):
        """The previous release (and the runtime bound to it) carries the tier tool."""
        for root in (self.previous / 'agentos', self.fx.layout.runtime):
            write(root / release_tier.TOOL, STUB)
            write(root / release_tier.RULES, json.dumps(answer))
        seal(self.previous)
        write(self.fx.layout.runtime / 'SOURCE_MANIFEST.json', (self.previous / 'agentos/SOURCE_MANIFEST.json').read_bytes())
        index = switch.load(self.fx.layout.index)
        index['releases'][0]['files_manifest_sha256'] = switch.state(self.previous / 'FILES.sha256')['sha256']
        write(self.fx.layout.index, switch.encoded(index))
        spec = switch.load(self.package / 'agentos' / switch.SPEC)
        spec['from_source_manifest_sha256'] = switch.state(self.previous / 'agentos/SOURCE_MANIFEST.json')['sha256']
        write(self.package / 'agentos' / switch.SPEC, switch.encoded(spec))
        write(self.package / 'interface/SKILL.md', (self.package / 'agentos/.agents/skills/xiaoba-agentos/SKILL.md').read_bytes()
              if (self.package / 'agentos/.agents/skills/xiaoba-agentos/SKILL.md').exists() else b'card\n')
        write(self.package / 'agentos/.agents/skills/xiaoba-agentos/SKILL.md', (self.package / 'interface/SKILL.md').read_bytes())
        seal(self.package)
        self.fx.system.record_worker(100)

    def record(self, *, reviewers=True, tier=True, edit=None):
        release = switch.load(self.package / 'RELEASE.json')
        release.pop('release_tier', None)
        release['acceptance']['end_to_end_evidence'] = (
            {'successful_agents': ['agy_review', 'dsh_review']} if reviewers else None)
        write(self.package / 'RELEASE.json', switch.encoded(release))
        seal(self.package)
        if tier:
            computed = release_tier.compute_tier(self.previous / 'agentos', self.package / 'agentos')
            computed['baseline_version'] = 'v9.0.0'
            if edit:
                computed.update(edit)
            release = switch.load(self.package / 'RELEASE.json')
            release['release_tier'] = computed
            write(self.package / 'RELEASE.json', switch.encoded(release))
            payload = ''.join(f'{sha}  ./{rel}\n' for rel, sha in switch.files(self.package).items() if rel != 'FILES.sha256')
            write(self.package / 'FILES.sha256', payload)

    def preflight(self):
        return switch.preflight(self.fx.layout, self.fx.system, now=lambda: 1000)

    def test_patch_tier_passes_preflight_without_reviewer_evidence(self):
        self.install_tool({'tier': 'patch'})
        self.record(reviewers=False)
        self.assertTrue(self.preflight()['ok'], self.preflight())

    def test_regular_and_migration_tiers_still_require_both_reviewers(self):
        for tier in ('regular', 'migration'):
            with self.subTest(tier=tier):
                self.install_tool({'tier': tier})
                self.record(reviewers=False)
                self.assertIn('independent acceptance', self.preflight().get('error', ''))
                self.record(reviewers=True)
                self.assertTrue(self.preflight()['ok'], self.preflight())

    def test_package_without_a_tier_record_is_refused_once_the_previous_release_has_the_tool(self):
        self.install_tool({'tier': 'patch'})
        self.record(reviewers=True, tier=False)
        self.assertIn('records no release tier', self.preflight().get('error', ''))

    def test_recorded_tier_that_differs_from_the_recomputed_tier_is_refused(self):
        self.install_tool({'tier': 'regular'})
        for edit in ({'tier': 'patch'}, {'baseline_version': 'v8.0.0'}, {'rules_sha256': '0' * 64}):
            with self.subTest(edit=edit):
                self.record(reviewers=False, edit=edit)
                self.assertIn('differs from the recomputed tier', self.preflight().get('error', ''))

    def test_legacy_previous_release_cannot_be_followed_by_a_self_declared_patch(self):
        self.record(reviewers=False, edit={'tier': 'patch'})
        self.assertIn('differs from the recomputed tier', self.preflight().get('error', ''))
        self.record(reviewers=False)
        self.assertIn('independent acceptance', self.preflight().get('error', ''))

    def test_patch_tier_package_cannot_change_interface_scripts_or_card(self):
        self.install_tool({'tier': 'patch'})
        write(self.package / 'interface/scripts/xiaoba', '#!/usr/bin/env python3\nprint("changed")\n')
        self.record(reviewers=False)
        self.assertIn('changes interface scripts', self.preflight().get('error', ''))
        write(self.package / 'interface/scripts/xiaoba', (self.previous / 'interface/scripts/xiaoba').read_bytes())
        write(self.package / 'interface/SKILL.md', 'unreviewed card\n')
        self.record(reviewers=False)
        self.assertIn('interface card differs', self.preflight().get('error', ''))

    def test_failing_tier_tool_blocks_preflight(self):
        self.install_tool({'tier': 'patch', 'ok': False, 'problems': ['manifest mismatch']})
        release = switch.load(self.package / 'RELEASE.json')
        write(self.package / 'RELEASE.json', switch.encoded(release))
        seal(self.package)
        self.assertIn('cannot be recomputed', self.preflight().get('error', ''))


class BuildTierGateTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = tree(self.root / 'candidate')
        for name in build_release.SOURCE_DIRECTORIES:
            (self.source / name).mkdir(exist_ok=True)
        for name in build_release.SOURCE_ROOT_FILES:
            if not (self.source / name).exists():
                write(self.source / name, name + '\n')

    def test_tier_is_computed_on_the_tree_that_enters_the_package_not_on_extras_beside_it(self):
        write(self.source / 'docs/in-package.md', 'x')
        write(self.source / 'interface/not-packaged.md', 'x')
        write(self.source / 'docs/__pycache__/cached.md', 'x')
        record = build_release.release_tier_record(self.previous({'tier': 'regular'}), self.source)
        seen = record['changed_paths']['regular']
        self.assertIn('docs/in-package.md', seen)
        self.assertEqual([path for path in seen if 'not-packaged' in path or 'cached' in path], [])

    def previous(self, answer, status='verified_immutable_release', bound=True):
        package = self.root / 'previous'
        tree(package / 'agentos', answer)
        digest = build_release.sha256(package / 'agentos/SOURCE_MANIFEST.json')
        write(package / 'RELEASE.json', json.dumps({
            'version': 'v9.0.0', 'status': status,
            'agentos_source_manifest_sha256': digest if bound else '0' * 64}))
        return package

    def args(self, previous, **extra):
        values = dict(agentos_root=self.source, previous_release=previous, acceptance_complete=True,
                      acceptance_database=None, acceptance_task_id=None, test_count=12, test_result='passed')
        values.update(extra)
        return argparse.Namespace(**values)

    def test_record_names_the_baseline_and_refuses_an_unverified_or_unbound_previous_release(self):
        record = build_release.release_tier_record(self.previous({'tier': 'regular'}), self.source)
        self.assertEqual((record['tier'], record['baseline_version']), ('regular', 'v9.0.0'))
        self.assertIsNone(build_release.release_tier_record(None, self.source))
        for kwargs in ({'status': 'release_candidate'}, {'bound': False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RuntimeError):
                build_release.release_tier_record(self.previous({'tier': 'patch'}, **kwargs), self.source)

    def test_patch_tier_build_runs_the_suite_itself_and_rejects_a_declared_count_that_differs(self):
        previous = self.previous({'tier': 'patch'})
        for count, result in ((11, 'passed'), (12, 'not_run')):
            with self.subTest(count=count, result=result), patch.object(build_release, 'run_full_tests', return_value=count):
                with self.assertRaisesRegex(RuntimeError, 'differs from the actual full run'):
                    build_release.build(self.args(previous, test_result=result))

    def test_patch_tier_build_stops_when_the_suite_fails(self):
        with patch.object(build_release, 'run_full_tests', side_effect=RuntimeError('suite failed')):
            with self.assertRaisesRegex(RuntimeError, 'suite failed'):
                build_release.build(self.args(self.previous({'tier': 'patch'})))

    def test_other_tiers_and_legacy_baselines_keep_the_reviewer_evidence_requirement(self):
        for answer in ({'tier': 'regular'}, {'tier': 'migration'}, None):
            with self.subTest(answer=answer), patch.object(build_release, 'run_full_tests') as suite:
                with self.assertRaisesRegex(RuntimeError, 'requires --acceptance-database'):
                    build_release.build(self.args(self.previous(answer)))
                suite.assert_not_called()

    def test_verified_build_requires_a_previous_release(self):
        with patch.object(build_release, 'validate_acceptance_database', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'requires --previous-release'):
                build_release.build(self.args(None, acceptance_database=self.root, acceptance_task_id='t'))

    def test_full_suite_runner_reports_the_real_count_and_refuses_failures(self):
        write(self.source / 'tests/test_sample.py',
              'import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n'
              '    def test_b(self):\n        self.assertTrue(True)\n')
        with patch.object(build_release, 'run_verify'):
            self.assertEqual(build_release.run_full_tests(self.source), 2)
            write(self.source / 'tests/test_bad.py',
                  'import unittest\nclass T(unittest.TestCase):\n    def test_c(self):\n        self.fail("x")\n')
            with self.assertRaisesRegex(RuntimeError, 'full test suite to pass'):
                build_release.run_full_tests(self.source)


if __name__ == '__main__':
    unittest.main()
