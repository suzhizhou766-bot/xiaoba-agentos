import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from xiaoba_agentos.skills import SkillRegistry


class SkillRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "skills"
        self.skill_dir = self.root / "assets"
        self.skill_dir.mkdir(parents=True)
        self.skill_file = self.skill_dir / "SKILL.md"
        self.skill_file.write_text(
            "---\nname: assets\ndescription: Verify reusable assets.\n---\n\n# Assets\n",
            encoding="utf-8",
        )
        self.index = self.root / "index.json"
        self._write_index()
        self.config = SimpleNamespace(
            skills_root=self.root.resolve(),
            skills_index=self.index.resolve(),
            skills={"max_selected": 3},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _write_index(self):
        digest = hashlib.sha256(self.skill_file.read_bytes()).hexdigest()
        self.index.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "entries": [
                        {
                            "id": "assets",
                            "name": "assets",
                            "directory": "assets",
                            "description": "asset verification",
                            "category": "asset-management",
                            "capabilities": ["asset-audit"],
                            "triggers": ["素材", "asset"],
                            "status": "active",
                            "callable": True,
                            "entrypoint_sha256": digest,
                            "files_sha256": {"SKILL.md": digest},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_verified_skill_can_be_matched_and_loaded(self):
        registry = SkillRegistry(self.config)
        self.assertTrue(registry.verify()["ok"])
        selected = registry.select_context("请审核这批素材和资产")
        self.assertEqual("assets", selected["selected"][0]["id"])
        self.assertIn("# Assets", selected["context"])

    def test_modified_skill_fails_closed(self):
        self.skill_file.write_text("tampered", encoding="utf-8")
        result = SkillRegistry(self.config).verify()
        self.assertFalse(result["ok"])
        self.assertTrue(any("哈希不匹配" in item for item in result["problems"]))

    def test_historical_context_is_rejected(self):
        self.skill_file.write_text(
            "---\nname: assets\ndescription: Verify assets.\n---\n\nHistorical memory: retired route\n",
            encoding="utf-8",
        )
        self._write_index()
        result = SkillRegistry(self.config).verify()
        self.assertFalse(result["ok"])
        self.assertTrue(any("非当前态内容" in item for item in result["problems"]))

    def test_unlisted_file_fails_closed(self):
        (self.skill_dir / "extra.txt").write_text("extra", encoding="utf-8")
        result = SkillRegistry(self.config).verify()
        self.assertFalse(result["ok"])
        self.assertTrue(any("文件集合不匹配" in item for item in result["problems"]))


if __name__ == "__main__":
    unittest.main()
