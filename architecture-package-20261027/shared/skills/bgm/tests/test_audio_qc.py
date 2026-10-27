#!/usr/bin/env python3
"""Tests for scripts/audio_qc.py. Run: python3 tests/test_audio_qc.py"""

import importlib.util
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path

# Keep the hash-bound skill directory free of __pycache__.
sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
_spec = importlib.util.spec_from_file_location("audio_qc", HERE.parent / "scripts" / "audio_qc.py")
audio_qc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audio_qc)

KEYS = {
    "path", "status", "duration", "sample_rate", "channels", "bit_depth",
    "compression", "peak_dbfs", "full_scale_samples", "sample_count", "issues",
}


class AudioQcTest(unittest.TestCase):
    def test_extensible_24bit_stereo_passes(self):
        # ffmpeg writes WAVE_FORMAT_EXTENSIBLE (0xFFFE) for 24-bit stereo PCM.
        path = FIXTURES / "tone_24bit_stereo_48k.wav"
        self.assertEqual(path.read_bytes()[20:22], b"\xfe\xff")
        result = audio_qc.inspect(path)
        self.assertEqual(set(result), KEYS)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["sample_rate"], 48000)
        self.assertEqual(result["channels"], 2)
        self.assertEqual(result["bit_depth"], 24)
        self.assertEqual(result["compression"], "NONE")
        self.assertEqual(result["duration"], 0.1)
        self.assertEqual(result["sample_count"], 4800 * 2)
        self.assertAlmostEqual(result["peak_dbfs"], -6.0, delta=0.1)

    def test_extensible_24bit_clipping_warns(self):
        result = audio_qc.inspect(FIXTURES / "clipped_24bit_stereo_48k.wav")
        self.assertEqual(result["status"], "WARN", result)
        self.assertGreater(result["full_scale_samples"], 0)
        self.assertIn(f"full_scale_samples={result['full_scale_samples']}", result["issues"])
        self.assertEqual(result["peak_dbfs"], 0.0)

    def test_classic_16bit_mono_44k_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "classic.wav"
            with wave.open(str(path), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(44100)
                out.writeframes(struct.pack("<4h", 0, 16384, -16384, 0))
            result = audio_qc.inspect(path)
        self.assertEqual(path.name, Path(result["path"]).name)
        self.assertEqual(result["status"], "WARN", result)
        self.assertEqual(result["issues"], ["sample_rate=44100", "channels=1"])
        self.assertEqual(result["bit_depth"], 16)
        self.assertEqual(result["sample_count"], 4)

    def test_non_wav_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.wav"
            path.write_bytes(b"not a wav file")
            result = audio_qc.inspect(path)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(result["reason"].startswith("decode: "))


if __name__ == "__main__":
    unittest.main()
