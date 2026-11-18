"""C03-03 public review-card/media transport tests; real CLI fixture, no model."""
import hashlib
import json
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

import test_agy_cli
from xiaoba_agentos.experience_loop import record_application, record_delivery
from xiaoba_agentos.delivery import user_decision
from xiaoba_agentos.validation_basis import validate_basis
from xiaoba_review.agent_workspace import WorkspaceError


class MediaIngressTests(test_agy_cli.CliFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.project = self.root / 'media-project'
        self.project.mkdir()
        self.tool_calls = []
        self.candidate = self.make_method_candidate()
        script = self.entry.read_text()
        anchor = "if flag('long-reply'):"
        media_check = (
            "if context.get('media_review'):\n"
            "    staged=[Path(line[2:].strip()) for line in card.split('## 材料副本（只读）\\n',1)[1].splitlines() if line.startswith('- ')]\n"
            "    assert len(staged)==5 and all(p.suffix in ('.png','.json') for p in staged)\n"
            "    assert all(p.is_file() for p in staged)\n"
            "    assert sum(p.suffix=='.png' for p in staged)==4\n"
            "    parameters=json.loads(next(p for p in staged if p.suffix=='.json').read_text())\n"
            "    assert parameters['model_read_verified'] is False\n"
            "    body='TEST derived media bytes accessible; no raw video supplied'\n"
        )
        script = script.replace(anchor, media_check + anchor, 1)
        script = script.replace(
            "meta+='审查结论: PASS\\n证据引用: TEST cli child process file receipt\\n'",
            "meta+='审查结论: PASS\\n证据引用: '+context['evidence_refs'][0]['ref']+'\\n审查类型: new_experience\\n评分: 8\\n适用条件: TEST only\\n' if context.get('media_review') else '审查结论: PASS\\n证据引用: TEST cli child process file receipt\\n'",
            1,
        )
        self.entry.write_text(script)

    def file_ref(self, name, body):
        path = self.project / name
        path.write_bytes(body)
        return {'path': str(path), 'sha256': hashlib.sha256(body).hexdigest()}

    def basis(self, *, artifact_only=False):
        task = self.native.submit('TEST actual video task', 2, snapshot={
            'task_purpose': 'user_task',
            'experience_policy': {'version': 1, 'kind': 'method', 'problem_key': 'TEST-review-ingress'},
        })
        self.native.complete(task, 'TEST actual task completed', expected_attempt=0)
        self.video = self.file_ref('source.mp4', b'TEST frozen source video')
        with self.native.read_only() as db:
            initial = db.execute("SELECT id FROM memory_reviews WHERE task_id=? AND stage='delivery' ORDER BY rowid DESC LIMIT 1", (self.candidate['task_id'],)).fetchone()[0]
        claims = [{'claim': 'TEST inspect video result', 'class': 'artifact'}]
        if not artifact_only:
            claims.append({'claim': 'TEST execute render code', 'class': 'code'})
        record_application(self.native, {
            'candidate_id': self.candidate['task_id'], 'candidate_sha256': self.candidate['evidence_sha256'],
            'delivery_review_id': initial,
            'real_task': {'id': task, 'problem_key': 'TEST-review-ingress', 'task_type': 'TEST video',
                'description': 'TEST actual video task', 'workspace': str(self.project),
                'authorization_ref': 'TEST user instruction'},
            'artifact': dict(self.video, version='v1'),
            'execution_evidence': [self.file_ref('execution.txt', b'TEST render evidence')],
            'conditions_checked': {'applicability': 'TEST input', 'exclusions': 'TEST checked', 'environment': 'TEST local'},
            'application_location': str(self.project), 'steps_applied': 'TEST render and inspect',
            'deviations': 'TEST none', 'method_claims': claims,
        })
        delivery = {
            'origin': 'user', 'user_message': '交付', 'message_id': 'TEST-video-delivery',
            'source_ref': 'TEST direct user event', 'real_task_id': task,
            'artifact': dict(self.video, version='v1'),
            'delivery_record': self.file_ref('accepted.txt', b'TEST original user acceptance'),
        }
        record_delivery(self.native, delivery,
            authority=user_decision(delivery, channel='cli_user_declaration', explicit=True))
        with self.native.read_only() as db:
            return validate_basis(db, self.native.get_memory_candidate(self.candidate['task_id']))

    def request(self, *, video=None, request_id='TEST-media-review'):
        if not any(node['node_id'] == 'TEST-media-a' for node in self.pool.state()['nodes']):
            self.add('TEST-media-a', model='TEST media model a', name='TEST media a')
            self.add('TEST-media-b', model='TEST media model b', name='TEST media b')
        return self.store.create(self.candidate['task_id'], 'TEST-media-a', 'TEST-media-b',
            'TEST independent code and derived-frame review',
            [{'ref': self.video['path'], 'sha256': self.video['sha256']}],
            request_id=request_id, review_type='new_experience',
            dsh_channel='backend_adapter', peer_channel='backend_adapter',
            media_review={'version': 1, 'source_video': video or self.video})

    def fake_tool(self, command, **kwargs):
        self.tool_calls.append(command)
        if Path(command[0]).name == 'ffprobe':
            return json.dumps({'format': {'duration': '12'}, 'streams': [
                {'codec_type': 'video', 'width': 1920, 'height': 1080, 'codec_name': 'h264'}]}).encode()
        Path(command[-1]).write_bytes(b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + struct.pack('>II', 1280, 720))
        return b''

    def run_card(self, card):
        with patch('xiaoba_agentos.capabilities.CapabilityRegistry.has_capability', return_value=True), \
                patch('xiaoba_agentos.media_review_materials.shutil.which', side_effect=lambda name: '/TEST/' + name), \
                patch('xiaoba_agentos.media_review_materials._run_bounded', side_effect=self.fake_tool):
            return self.adapter('TEST-media-a').run(card)

    def test_public_create_freezes_basis_then_cli_receives_only_derived_materials(self):
        basis = self.basis()
        request = self.request()
        card = next(item['card'] for item in request['assignments'] if item['source_id'] == 'TEST-media-a')
        self.assertEqual(basis['basis_id'], card['media_review']['basis_id'])
        self.assertEqual(basis['basis_sha256'], card['media_review']['basis_sha256'])
        self.assertEqual(self.video, card['media_review']['source_video'])
        self.assertEqual('video-sha256:' + self.video['sha256'], card['evidence_refs'][0]['ref'])
        self.assertEqual(request['id'], self.request()['id'])
        result = self.run_card(card)
        self.assertEqual('PASS', result['verdict'])
        self.assertIn('no raw video supplied', result['findings'])
        transport = result['execution_evidence']['media_materials']
        self.assertEqual('staged_hash_verified', transport['status'])
        self.assertFalse(transport['model_read_verified'])
        self.assertEqual(5, len(transport['files']))
        self.assertEqual(5, len(self.tool_calls))
        self.assertEqual(1, len(self.starts()))
        for ref in transport['files']:
            path = Path(ref['path'])
            self.assertTrue(path.is_relative_to(self.workspaces))
            self.assertEqual(ref['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(0, path.stat().st_mode & 0o222)
        self.assertFalse(list(self.workspaces.rglob('*.mp4')))
        self.assertTrue(self.native.verify_event_chain()['ok'])

    def test_public_create_rejects_unrelated_video_and_pure_artifact_route(self):
        self.basis()
        wrong = self.file_ref('other.mp4', b'TEST unrelated')
        with self.assertRaisesRegex(ValueError, 'current validation basis artifact'):
            self.request(video=wrong)
        with self.native.read_only() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM ri_requests WHERE id=?', ('TEST-media-review',)).fetchone())

    def test_pure_artifact_route_has_no_new_media_review_requirement(self):
        self.basis(artifact_only=True)
        with self.assertRaisesRegex(ValueError, 'combined validation basis'):
            self.request()
        self.assertEqual([], self.tool_calls)
        self.assertEqual([], self.starts())

    def test_unknown_capability_blocks_before_tools_or_cli(self):
        self.basis()
        card = self.request()['assignments'][0]['card']
        with patch('xiaoba_agentos.media_review_materials._run_bounded') as tools:
            with self.assertRaises(WorkspaceError) as error:
                self.adapter('TEST-media-a').run(card)
            self.assertEqual('missing_materials', error.exception.code)
            tools.assert_not_called()
        self.assertEqual([], self.starts())

    def test_video_drift_after_request_blocks_before_cli(self):
        self.basis()
        card = self.request()['assignments'][0]['card']
        Path(self.video['path']).write_bytes(b'TEST changed after freeze')
        with self.assertRaises(WorkspaceError) as error:
            self.run_card(card)
        self.assertEqual('missing_materials', error.exception.code)
        self.assertEqual([], self.tool_calls)
        self.assertEqual([], self.starts())

    def test_dispatch_rechecks_basis_after_media_card_creation(self):
        self.basis()
        request = self.request()
        assignment = next(item for item in request['assignments'] if item['source_id'] == 'TEST-media-a')
        media = assignment['card']['media_review']
        from xiaoba_agentos.validation_basis import revoke_basis_tx
        with self.native.connect() as db:
            revoke_basis_tx(self.native, db, self.candidate['task_id'], media['basis_id'],
                            'TEST revoked after card creation')
        with self.assertRaisesRegex(ValueError, 'missing_materials: current validation basis'):
            self.backends.execute(assignment['card']['assignment_id'])
        with self.assertRaises(WorkspaceError) as error:
            self.run_card(assignment['card'])
        self.assertEqual('missing_materials', error.exception.code)
        self.assertEqual([], self.tool_calls)
        self.assertEqual([], self.starts())
        with self.native.read_only() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM events WHERE event_type='open_review.backend_started'").fetchone()[0])

    def test_dispatch_refuses_stdio_and_interactive_media_adapters_before_run(self):
        self.basis()
        request = self.request()
        from xiaoba_review.resource_agy import AgyInteractiveResource
        from xiaoba_review.resource_stdio import JsonStdioResource
        marker = self.root / 'stdio-started'
        entry = self.root / 'media-stdio'
        entry.write_text('#!' + __import__('sys').executable + '\n'
            'from pathlib import Path\n'
            'import json,sys\n'
            'Path(' + repr(str(marker)) + ').write_text("started")\n'
            'value=json.loads(sys.stdin.read())\n'
            'print(json.dumps({"protocol":"xiaoba-resource-v1","card_digest":value["card"]["card_digest"],"verdict":"PASS","findings":"TEST","evidence_refs":[]}))\n')
        entry.chmod(0o700)
        stdio_source = self.source('TEST-media-a')
        stdio_source['node']['entry'] = str(entry)
        self.backends.register('TEST-media-a', JsonStdioResource(stdio_source))
        interactive_source = self.source('TEST-media-b')
        interactive = AgyInteractiveResource(interactive_source, self.workspaces)
        self.backends.register('TEST-media-b', interactive)
        with patch('xiaoba_agentos.capabilities.CapabilityRegistry.has_capability', return_value=True), \
                patch.object(AgyInteractiveResource, 'run', side_effect=AssertionError('interactive run must not start')) as run:
            for assignment in request['assignments']:
                with self.subTest(source=assignment['source_id']), self.assertRaisesRegex(
                        ValueError, 'only the controlled CLI worklog entry stages media'):
                    self.backends.execute(assignment['card']['assignment_id'])
            run.assert_not_called()
        self.assertFalse(marker.exists())
        self.assertEqual([], self.starts())
        with self.native.read_only() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM events WHERE event_type='open_review.backend_started'").fetchone()[0])

    def test_bare_video_is_refused_without_a_frozen_request(self):
        self.video = self.file_ref('bare.mp4', b'TEST bare video')
        node = self.add()
        with self.assertRaises(WorkspaceError) as error:
            self.adapter(node).run({'assignment_id': 'TEST-bare',
                'evidence_refs': [{'ref': self.video['path'], 'sha256': self.video['sha256']}]})
        self.assertEqual('missing_materials', error.exception.code)
        self.assertEqual([], self.starts())


if __name__ == '__main__':
    unittest.main()
