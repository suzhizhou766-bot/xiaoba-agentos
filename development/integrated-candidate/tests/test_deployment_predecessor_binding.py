"""发布声明必须绑定已上线的那一版，且已执行步骤逐字节不变。

切换预检 `switch_release.checked_spec` 对 `docs/DEPLOYMENT.json` 有两条硬要求：
新包声明的 `from_source_manifest_sha256` 必须等于活动发布 `RELEASE.json` 的
`agentos_source_manifest_sha256`（第 580 行）；每个已执行步骤的哈希必须等于
`.runtime/release-steps.json` 里记录的哈希（第 616 行）。

下面两个常量是 2026-11-02 只读抄自线上的真实值：清单哈希来自
`.runtime/releases/v1.6.5/agentos/SOURCE_MANIFEST.json`，步骤哈希来自
`.runtime/release-steps.json`。测试现场全部建在临时目录里，不读写线上。
"""
import json
import tempfile
import unittest
from pathlib import Path

from test_switch_release import Fixture, seal, switch, write

ROOT = Path(__file__).resolve().parents[1]

LIVE_V165_SOURCE_MANIFEST_SHA256 = '317505b7b1199de16167d8adb8e11c097e96cc3d750214b5a714c9118c750195'
V164_SOURCE_MANIFEST_SHA256 = 'd27e2c388c6e1e3315d6e41a9486cf303726c6d2d54a85d71a1e2704ced7facd'
RECORDED_STEPS = {
    '0002-skills-root': ('v1.6.1', '71ca23fde7feb9d89db4345f264b728332901c38caaed9d6a4f7101b6b78ed48'),
    '0003-skills-index': ('v1.6.1', '05eecdb2dc836a7e1ab0e95343002b598d209bb5c6cc5cdf2ffa8a20498ded61'),
    '0004-knowledge-index': ('v1.6.1', 'c6113993c5987ae3e289c55502398ddfafc49ad667351a468ebd62e3f30dd972'),
    '0005-agy-configured-model': ('v1.6.5', 'ff4a98e0cb325cefe2b2cf01c3698d3d8564bed98e2747f534918f704cf098b1'),
}


class DeploymentPredecessorBindingTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fx = Fixture(Path(temporary.name).resolve() / 'scene')
        self.layout = self.fx.layout

    def declaration(self):
        return json.loads((ROOT / 'docs/DEPLOYMENT.json').read_text(encoding='utf-8'))

    def scene(self, declaration):
        """把声明装进临时现场，并把四个步骤还原成“已在线上执行过”。

        历史包永远按仓库里未经改动的声明生成——线上那些包是不可变的，被改动的只可能
        是这次要切换过去的新声明。
        """
        write(self.layout.package / 'agentos' / switch.SPEC, switch.encoded(declaration))
        seal(self.layout.package)
        runtime = self.layout.runtime
        index = switch.load(runtime / '.runtime/releases/index.json')
        by_id = {step['id']: step for step in self.declaration()['steps']}
        for version in sorted({recorded for recorded, _ in RECORDED_STEPS.values()}):
            original = runtime / '.runtime/releases' / version
            source = original / 'agentos'
            for name in switch.SOURCE_DIRS:
                (source / name).mkdir(parents=True, exist_ok=True)
            for name in switch.SOURCE_FILES:
                write(source / name, version + '\n')
            owned = [by_id[step_id] for step_id, (owner, _) in RECORDED_STEPS.items() if owner == version]
            write(source / switch.SPEC, switch.encoded({'schema_version': 1, 'steps': owned}))
            write(original / 'second-brain/root.md', version + '\n')
            write(original / 'interface/SKILL.md', version + '\n')
            for name in ('xiaoba', 'verify-release'):
                write(original / 'interface/scripts' / name,
                      '#!/usr/bin/env python3\nfrom pathlib import Path\nDEFAULT_CONFIG = Path("/unbound")\n')
            write(original / 'RELEASE.json', switch.encoded({'version': version, 'database_schema_version': 7}))
            seal(original)
            index['releases'].append({'version': version, 'path': version, 'status': 'verified_immutable',
                                      'files_manifest_sha256': switch.state(original / 'FILES.sha256')['sha256']})
        write(runtime / '.runtime/releases/index.json', switch.encoded(index))
        write(self.layout.history, switch.encoded({'steps': {step_id: {'release': owner, 'sha256': sha256}
                                                             for step_id, (owner, sha256) in RECORDED_STEPS.items()}}))
        assistant = self.layout.assistant
        write(assistant / 'skills/index.json', '{}\n')
        write(assistant / '系统/价值文件索引/index.json', '{}\n')
        config = switch.load(self.layout.config)
        config['skills'] = {'root_dir': str(assistant / 'skills'), 'index_path': str(assistant / 'skills/index.json')}
        config['knowledge'] = {'index_path': str(assistant / '系统/价值文件索引/index.json')}
        write(self.layout.config, switch.encoded(config))
        return ({'version': 'v1.6.5', 'database_schema_version': 7,
                 'agentos_source_manifest_sha256': LIVE_V165_SOURCE_MANIFEST_SHA256},
                {'version': 'v1.6.6', 'database_schema_version': 7})

    def test_repository_declaration_binds_the_live_release_and_keeps_executed_steps(self):
        declaration = self.declaration()
        # 抄录的常量必须仍然等于仓库声明本身，否则这个测试测的是别的东西。
        self.assertEqual(LIVE_V165_SOURCE_MANIFEST_SHA256, declaration['from_source_manifest_sha256'])
        self.assertEqual(sorted(RECORDED_STEPS), sorted(step['id'] for step in declaration['steps']))
        for step in declaration['steps']:
            self.assertEqual(RECORDED_STEPS[step['id']][1], switch.digest(switch.encoded(step)),
                             f"{step['id']} 与线上已执行记录不同，预检会报 executed step has been modified")
        old, new = self.scene(declaration)
        spec = switch.checked_spec(self.layout, old, new)
        self.assertEqual(LIVE_V165_SOURCE_MANIFEST_SHA256, spec['from_source_manifest_sha256'])

    def test_declaration_still_bound_to_the_previous_release_is_rejected(self):
        declaration = self.declaration()
        declaration['from_source_manifest_sha256'] = V164_SOURCE_MANIFEST_SHA256
        old, new = self.scene(declaration)
        with self.assertRaisesRegex(switch.SwitchError, 'deployment predecessor binding mismatch'):
            switch.checked_spec(self.layout, old, new)

    def test_editing_an_executed_step_is_rejected_even_with_the_right_predecessor(self):
        declaration = self.declaration()
        target = next(step for step in declaration['steps'] if step['id'] == '0004-knowledge-index')
        # 只改一个仍然合法的前置值：结构门禁都过得去，能拦住它的只剩历史步骤哈希核对。
        target['check'] = dict(target['check'], value='knowledge/renamed.json')
        old, new = self.scene(declaration)
        with self.assertRaisesRegex(switch.SwitchError, 'executed step has been modified'):
            switch.checked_spec(self.layout, old, new)

    def test_this_release_adds_no_step(self):
        self.assertEqual(sorted(RECORDED_STEPS), sorted(step['id'] for step in self.declaration()['steps']),
                         'v1.6.6 不新增编号步骤：声明里的步骤必须正好是线上已执行的那四个')


if __name__ == '__main__':
    unittest.main()
