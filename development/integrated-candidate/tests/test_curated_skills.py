import csv
import json
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CuratedSkillSmokeTest(unittest.TestCase):
    def test_assets_manifest_is_deterministic_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            original = source / "原文.txt"
            original.write_text("source", encoding="utf-8")
            output = Path(temporary) / "manifest.csv"
            command = [sys.executable, str(ROOT / "tests/fixtures/content/skills/assets/scripts/asset_manifest.py"), str(source), str(output)]
            first = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(0, first.returncode, first.stderr)
            before = output.read_bytes()
            second = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertEqual(before, output.read_bytes())
            self.assertEqual("source", original.read_text(encoding="utf-8"))

    def test_bgm_audio_qc_accepts_delivery_wav(self):
        with tempfile.TemporaryDirectory() as temporary:
            audio = Path(temporary) / "delivery.wav"
            with wave.open(str(audio), "wb") as handle:
                handle.setnchannels(2)
                handle.setsampwidth(2)
                handle.setframerate(48000)
                frame = struct.pack("<hh", 1000, -1000)
                handle.writeframes(frame * 4800)
            result = subprocess.run(
                [sys.executable, str(ROOT / "tests/fixtures/content/skills/bgm/scripts/audio_qc.py"), str(audio)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("PASS", json.loads(result.stdout)["status"])

    def test_project_storyboard_accepts_complete_and_rejects_incomplete(self):
        headers = ["镜号", "时间段", "时长", "叙事任务", "画面内容", "音乐节拍", "补拍备选", "注意事项"]
        with tempfile.TemporaryDirectory() as temporary:
            valid = Path(temporary) / "valid.csv"
            invalid = Path(temporary) / "invalid.csv"
            with valid.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(headers)
                writer.writerow(["1", "0-2", "2", "建立空间", "入口", "铺垫", "静态备份", "安全"])
            with invalid.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(headers)
                writer.writerow(["1", "0-2", "2", "", "入口", "铺垫", "静态备份", "安全"])
            script = str(ROOT / "tests/fixtures/content/skills/project/scripts/check_storyboard.py")
            accepted = subprocess.run([sys.executable, script, str(valid)], text=True, capture_output=True, check=False)
            rejected = subprocess.run([sys.executable, script, str(invalid)], text=True, capture_output=True, check=False)
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            self.assertNotEqual(0, rejected.returncode)


if __name__ == "__main__":
    unittest.main()
