import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from xiaoba_agentos.knowledge import KnowledgeCatalog


class KnowledgeCatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "method.md"
        self.source.write_text("# Current method\n\nVerified content.\n", encoding="utf-8")
        self.index = self.root / "index.json"
        self._write_index()
        self.config = SimpleNamespace(
            knowledge_index=self.index,
            knowledge_allowed_roots=[self.root.resolve()],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _write_index(self, path=None, mode="semantic_source"):
        path = path or self.source
        self.index.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "entries": [
                        {
                            "id": "method",
                            "title": "Method",
                            "category": "knowledge",
                            "index_mode": mode,
                            "path": str(path),
                            "summary": "verified",
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_verified_catalog_lists_source(self):
        catalog = KnowledgeCatalog(self.config)
        self.assertTrue(catalog.verify()["ok"])
        self.assertEqual("method", catalog.list()[0]["id"])

    def test_modified_source_fails_closed(self):
        self.source.write_text("changed", encoding="utf-8")
        result = KnowledgeCatalog(self.config).verify()
        self.assertFalse(result["ok"])
        self.assertTrue(any("哈希不匹配" in item for item in result["problems"]))

    def test_secret_file_is_rejected(self):
        secret = self.root / "provider.env"
        secret.write_text("TOKEN=hidden", encoding="utf-8")
        self._write_index(secret)
        result = KnowledgeCatalog(self.config).verify()
        self.assertFalse(result["ok"])
        self.assertTrue(any("敏感排除边界" in item for item in result["problems"]))

    def test_runtime_binding_must_be_executable(self):
        script = self.root / "pipeline.sh"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(script, 0o600)
        self._write_index(script, "runtime_binding")
        result = KnowledgeCatalog(self.config).verify()
        self.assertFalse(result["ok"])
        self.assertTrue(any("不可执行" in item for item in result["problems"]))

    def test_retrieve_with_receipt_canonical_match(self):
        catalog = KnowledgeCatalog(self.config)
        receipt = catalog.retrieve_with_receipt("verified")
        self.assertEqual("canonical_match", receipt["retrieval_status"])
        self.assertEqual(1, len(receipt["hits"]))
        hit = receipt["hits"][0]
        self.assertEqual("method", hit["id"])
        self.assertEqual(str(self.source), hit["path"])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), hit["source_sha256"])
        self.assertTrue(hit["is_active"])
        self.assertTrue(hit["authority"])
        self.assertEqual(hashlib.sha256(self.index.read_bytes()).hexdigest(), receipt["index_sha256"])
        self.assertTrue(receipt["verified_at"])

    def test_retrieve_with_receipt_miss(self):
        catalog = KnowledgeCatalog(self.config)
        receipt = catalog.retrieve_with_receipt("nonexistent phrase")
        self.assertEqual("miss", receipt["retrieval_status"])
        self.assertEqual([], receipt["hits"])
        self.assertNotIn("problems", receipt)

    def test_retrieve_with_receipt_disabled(self):
        self.config.knowledge_enabled = False
        catalog = KnowledgeCatalog(self.config)
        receipt = catalog.retrieve_with_receipt("verified")
        self.assertEqual("disabled", receipt["retrieval_status"])
        self.assertEqual([], receipt["hits"])

    def test_retrieve_with_receipt_degraded_when_tampered(self):
        self.source.write_text("tampered content", encoding="utf-8")
        catalog = KnowledgeCatalog(self.config)
        receipt = catalog.retrieve_with_receipt("verified")
        self.assertEqual("degraded", receipt["retrieval_status"])
        self.assertEqual([], receipt["hits"])
        self.assertTrue(any("哈希不匹配" in p for p in receipt.get("problems", [])))

    def test_retrieve_with_receipt_excludes_archived(self):
        archived_source = self.root / "archived.md"
        archived_source.write_text("Old archived content\n", encoding="utf-8")
        self.index.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "entries": [
                        {
                            "id": "active_item",
                            "title": "Active Rule",
                            "category": "knowledge",
                            "index_mode": "semantic_source",
                            "path": str(self.source),
                            "summary": "common keyword active",
                            "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                            "status": "active",
                        },
                        {
                            "id": "archived_item",
                            "title": "Archived Rule",
                            "category": "knowledge",
                            "index_mode": "semantic_source",
                            "path": str(archived_source),
                            "summary": "common keyword archived",
                            "sha256": hashlib.sha256(archived_source.read_bytes()).hexdigest(),
                            "status": "archived",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        catalog = KnowledgeCatalog(self.config)
        receipt = catalog.retrieve_with_receipt("common keyword")
        self.assertEqual("canonical_match", receipt["retrieval_status"])
        self.assertEqual(["active_item"], [hit["id"] for hit in receipt["hits"]])

    def test_retrieve_with_receipt_preserves_explicit_authority(self):
        data = json.loads(self.index.read_text(encoding="utf-8"))
        data["entries"][0]["authority"] = False
        self.index.write_text(json.dumps(data), encoding="utf-8")
        receipt = KnowledgeCatalog(self.config).retrieve_with_receipt("verified")
        self.assertEqual("canonical_match", receipt["retrieval_status"])
        self.assertFalse(receipt["hits"][0]["authority"])


if __name__ == "__main__":
    unittest.main()
