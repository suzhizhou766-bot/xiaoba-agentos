"""Controlled mixed-review media derivatives; no live models or production IO."""
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from xiaoba_agentos.capabilities import agent_provider_model, CapabilityRegistry, capable_agents
from xiaoba_agentos.config import AgentConfig
from xiaoba_agentos.media_review_materials import (
    _run_bounded, prepare_video_review_materials, require_media_review_node,
)


def source(kind="agy", model="gemini-3.8-flash-high", interface="cli"):
    return {"reviewer_id": "TEST-media-node", "kind": "review", "source_ref": "agent-entry:TEST",
            "node": {"agent_type": kind, "model": model, "entry": "/TEST/" + kind, "interface": interface}}


class PoolCapabilityIdentityTest(unittest.TestCase):
    def agent(self, node_source, **raw):
        return AgentConfig("TEST", "pool_cli", True, "review", 20, None,
                           dict(pool_source=node_source, **raw))

    def test_pool_cli_frozen_agy_identity_reaches_declared_capability_gate(self):
        agent = self.agent(source())
        self.assertEqual(("Antigravity", "gemini-3.8-flash-high"), agent_provider_model(agent))
        found, missing = capable_agents([agent], CapabilityRegistry(), ["coding"])
        self.assertEqual([agent], found)
        self.assertEqual([], missing)
        self.assertFalse(CapabilityRegistry().has_capability(*agent_provider_model(agent), "vision"))

    def test_explicit_provider_and_model_are_not_overridden(self):
        agent = self.agent(source(), provider="TEST user provider", model="TEST selected model")
        self.assertEqual(("TEST user provider", "TEST selected model"), agent_provider_model(agent))

    def test_unknown_model_or_family_never_inherits_another_node_capability(self):
        for node in (source(model="TEST unknown model"), source(kind="qoder", model="TEST model")):
            found, missing = capable_agents([self.agent(node)], CapabilityRegistry(), ["vision"])
            self.assertFalse(found)
            self.assertTrue(missing)

    def test_dsh_without_configured_model_stays_unknown(self):
        self.assertEqual(("DeepSeekHarness", ""), agent_provider_model(self.agent(source("dsh", None))))


class ReviewMediaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.video = self.root / "TEST clip.mp4"
        self.video.write_bytes(b"TEST frozen video bytes")
        self.ref = {"path": str(self.video), "sha256": hashlib.sha256(self.video.read_bytes()).hexdigest()}
        self.calls = []

    def fake_run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if Path(command[0]).name == "ffprobe":
            return json.dumps({"format": {"duration": "12"}, "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
                 "r_frame_rate": "30/1"}]}).encode()
        target = Path(command[-1])
        target.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 1280, 720))
        return b""

    def prepare(self, **kwargs):
        with patch("xiaoba_agentos.media_review_materials.shutil.which", side_effect=lambda name: "/TEST/" + name), \
                patch("xiaoba_agentos.media_review_materials._run_bounded", side_effect=self.fake_run):
            return prepare_video_review_materials(self.ref, str(self.root), route="combined", **kwargs)

    def test_mixed_packet_contains_hashed_frames_metadata_and_no_raw_video(self):
        # A declared future vision node is simulated; the current registry's
        # AGY/DSH declarations remain false and are explicitly rejected below.
        with patch("xiaoba_agentos.capabilities.CapabilityRegistry.has_capability", return_value=True):
            packet = self.prepare(review_source=source())
        self.assertEqual("ready", packet["status"], packet)
        self.assertTrue(packet["delivery_checked"])
        self.assertFalse(packet["model_read_verified"])
        self.assertEqual(4, len(packet["frames"]))
        self.assertEqual(5, len(packet["material_files"]))
        for ref in packet["material_files"]:
            path = Path(ref["path"])
            self.assertTrue(path.is_relative_to(self.root))
            self.assertNotEqual(path, self.video)
            self.assertEqual(ref["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        metadata = json.loads(Path(packet["metadata"]["path"]).read_text())
        self.assertEqual(self.ref, metadata["source_video"])
        self.assertEqual([0, 4, 8, 11.4], [x["timestamp_seconds"] for x in packet["frames"]])
        self.assertEqual(5, len(self.calls))
        self.assertTrue(all(kwargs["timeout_seconds"] <= 20 for _, kwargs in self.calls))
        self.assertTrue(all("-protocol_whitelist" in cmd for cmd, _ in self.calls))

    def test_preparation_without_node_does_not_claim_delivery_or_readback(self):
        packet = self.prepare()
        self.assertEqual("ready", packet["status"])
        self.assertFalse(packet["delivery_checked"])
        self.assertFalse(packet["model_read_verified"])

    def test_pure_artifact_route_never_invokes_preprocessing(self):
        with patch("xiaoba_agentos.media_review_materials.shutil.which") as tool:
            packet = prepare_video_review_materials(self.ref, str(self.root), route="real_task_acceptance")
        tool.assert_not_called()
        self.assertEqual("not_required", packet["status"])

    def test_unknown_or_attended_entry_is_reported_for_manual_review(self):
        for node in (source(interface="tmux"), source(interface="desktop"), source(model="TEST unknown"), source()):
            packet = self.prepare(review_source=node)
            self.assertEqual("missing_materials", packet["status"])
            self.assertTrue(packet["manual_review"])
            self.assertEqual([], packet["material_files"])
        self.assertEqual([], self.calls)

    def test_missing_tools_refuse_instead_of_passing_video_to_model(self):
        with patch("xiaoba_agentos.media_review_materials.shutil.which", return_value=None):
            packet = prepare_video_review_materials(self.ref, str(self.root), route="combined")
        self.assertEqual("missing_materials", packet["status"])
        self.assertTrue(packet["manual_review"])

    def test_hash_symlink_and_workspace_escape_refuse(self):
        for ref in (dict(self.ref, sha256="0" * 64),):
            with patch("xiaoba_agentos.media_review_materials.shutil.which", return_value="/TEST/tool"):
                packet = prepare_video_review_materials(ref, str(self.root), route="combined")
            self.assertEqual("missing_materials", packet["status"])
        link = self.root / "link.mp4"
        link.symlink_to(self.video)
        with patch("xiaoba_agentos.media_review_materials.shutil.which", return_value="/TEST/tool"):
            for ref, workspace in ((dict(self.ref, path=str(link)), self.root), (self.ref, self.root / "elsewhere")):
                packet = prepare_video_review_materials(ref, str(workspace), route="combined")
                self.assertEqual("missing_materials", packet["status"])

    def test_bad_or_oversized_frames_never_become_materials(self):
        original = self.fake_run
        def fake(command, **kwargs):
            out = original(command, **kwargs)
            if Path(command[0]).name == "ffmpeg":
                Path(command[-1]).write_bytes(b"TEST not an image")
            return out
        self.fake_run = fake
        packet = self.prepare()
        self.assertEqual("missing_materials", packet["status"])
        self.assertEqual([], packet["material_files"])

    def test_subprocess_timeout_and_output_limit_are_enforced(self):
        with self.assertRaisesRegex(ValueError, "timed out"):
            _run_bounded([sys.executable, "-B", "-c", "import time;time.sleep(1)"], timeout_seconds=0.05)
        with self.assertRaisesRegex(ValueError, "output exceeds"):
            _run_bounded([sys.executable, "-B", "-c", "import sys;sys.stdout.write('x'*200000)"], timeout_seconds=2)

    def test_video_mutation_during_preparation_refuses_completed_packet(self):
        original = self.fake_run
        def fake(command, **kwargs):
            out = original(command, **kwargs)
            if len(self.calls) == 5:
                self.video.write_bytes(b"TEST changed")
            return out
        self.fake_run = fake
        packet = self.prepare()
        self.assertEqual("missing_materials", packet["status"])
        self.assertIn("changed", packet["reason"])

    def test_same_bytes_symlink_replacement_during_preparation_is_rejected(self):
        replacement = self.root / 'replacement.mp4'
        replacement.write_bytes(self.video.read_bytes())
        original = self.fake_run
        def fake(command, **kwargs):
            out = original(command, **kwargs)
            if len(self.calls) == 5:
                self.video.unlink()
                self.video.symlink_to(replacement)
            return out
        self.fake_run = fake
        packet = self.prepare()
        self.assertEqual('missing_materials', packet['status'])
        self.assertIn('path changed', packet['reason'])


if __name__ == "__main__":
    unittest.main()
