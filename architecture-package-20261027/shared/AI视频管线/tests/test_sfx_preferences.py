#!/usr/bin/env python3
import csv
import json
import tempfile
import unittest
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import sfx_match


class SfxPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.library = root / "library"
        (self.library / "精选音效").mkdir(parents=True)
        (self.library / "普通").mkdir(parents=True)
        self.whoosh = self.library / "精选音效" / "whoosh.wav"
        self.footstep = self.library / "精选音效" / "footstep.wav"
        self.rejected = self.library / "普通" / "rejected.wav"
        for path in (self.whoosh, self.footstep, self.rejected):
            path.write_bytes(b"fixture")
        self.index = self.library / "index.csv"
        with self.index.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["相对路径", "是否精选", "使用次数", "评分", "否定次数", "使用项目"])
            writer.writeheader()
            writer.writerow({"相对路径": "精选音效/whoosh.wav", "是否精选": "是", "使用次数": "3", "评分": "4.5", "否定次数": "0", "使用项目": "A"})
            writer.writerow({"相对路径": "精选音效/footstep.wav", "是否精选": "是", "使用次数": "3", "评分": "4.5", "否定次数": "0", "使用项目": "A"})
        self.log = root / "usage.jsonl"
        self.log.write_text(json.dumps({"type": "feedback", "selected_path": str(self.rejected), "rating": 1, "project": "A", "ts": "2026-09-01T00:00:00Z"}) + "\n", encoding="utf-8")
        self.db = root / "db.csv"
        fields = ["name", "path", "category", "duration", "crest", "zcr", "flatness", "keywords"]
        with self.db.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow({"name": self.whoosh.name, "path": str(self.whoosh), "category": "转场", "duration": 1, "crest": 4, "zcr": .1, "flatness": .01, "keywords": "whoosh|transition"})
            writer.writerow({"name": self.footstep.name, "path": str(self.footstep), "category": "生活", "duration": 1, "crest": 4, "zcr": .1, "flatness": .01, "keywords": "footstep"})
        self.original = (sfx_match.LIBRARY, sfx_match.INDEX, sfx_match.USAGE_LOG, sfx_match.DB)
        sfx_match.LIBRARY = self.library
        sfx_match.INDEX = self.index
        sfx_match.USAGE_LOG = self.log
        sfx_match.DB = self.db
        sfx_match._PREFERENCE_SIGNATURE = None
        sfx_match._PREFERENCE_CACHE = {}

    def tearDown(self):
        sfx_match.LIBRARY, sfx_match.INDEX, sfx_match.USAGE_LOG, sfx_match.DB = self.original
        sfx_match._PREFERENCE_SIGNATURE = None
        sfx_match._PREFERENCE_CACHE = {}
        self.temp.cleanup()

    def test_selected_and_same_project_receive_bounded_bonus(self):
        info = sfx_match.preference_for({"path": str(self.whoosh)}, project="A")
        self.assertEqual(info["tier"], "selected")
        self.assertGreater(info["bonus"], 0)
        self.assertLessEqual(info["bonus"], 35)
        self.assertTrue(info["same_project"])

    def test_rejection_lowers_priority(self):
        info = sfx_match.preference_for({"path": str(self.rejected)}, project="A")
        self.assertLess(info["bonus"], 0)
        self.assertEqual(info["reject_count"], 1)

    def test_curated_but_wrong_semantics_cannot_jump_profile(self):
        matches = sfx_match.query("whoosh", 1, 2, project="A")
        self.assertEqual(matches[0][1]["name"], "whoosh.wav")
        wrong = next(row for _, row in matches if row["name"] == "footstep.wav")
        self.assertLessEqual(wrong["preference_bonus"], 3)


if __name__ == "__main__":
    unittest.main()
