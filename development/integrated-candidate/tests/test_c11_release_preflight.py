"""C11-02 tests for the read-only派审前 preflight. All fixtures are isolated
temp dirs; no production file, manifest, DB or model is touched.
"""
import hashlib
import importlib.util
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pf = _load("release_preflight")
release_submit = _load("submit_release_acceptance")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DeploymentBindingTest(unittest.TestCase):
    """The predecessor hash is NOT a self-hash: never assert an impossible equation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / "docs").mkdir()

    def test_predecessor_hashes_do_not_have_to_match_self(self):
        # Simulate a real tree: the self manifest exists with its own hash, and
        # DEPLOYMENT declares the *live predecessor* hash (different from self).
        self_manifest_bytes = b"self manifest bytes"
        (self.root / pf.MANIFEST_NAME).write_bytes(self_manifest_bytes)
        (self.root / "docs/DEPLOYMENT.json").write_text(
            json.dumps({"schema_version": 1, "from_source_manifest_sha256": "a" * 64, "steps": []}),
            encoding="utf-8",
        )
        report = pf._new_report()
        pf.check_deployment(report, self.root, predecessor_sha256="a" * 64)
        self.assertTrue(report["ok"])  # predecessor == declared, both differ from self
        self.assertFalse(report["checks"]["deployment"]["is_self_reference"])
        self.assertEqual(_sha(self_manifest_bytes), report["checks"]["deployment"]["self_manifest_sha256"])

    def test_predecessor_mismatch_is_flagged(self):
        (self.root / "docs/DEPLOYMENT.json").write_text(
            json.dumps({"from_source_manifest_sha256": "a" * 64}), encoding="utf-8"
        )
        report = pf._new_report()
        pf.check_deployment(report, self.root, predecessor_sha256="c" * 64)
        self.assertFalse(report["ok"])
        self.assertIn("deployment_predecessor_mismatch", [i["code"] for i in report["issues"]])

    def test_self_reference_is_flagged_not_satisfied(self):
        manifest_sha = _sha(b"whatever the source manifest bytes are")
        (self.root / pf.MANIFEST_NAME).write_bytes(b"whatever the source manifest bytes are")
        (self.root / "docs/DEPLOYMENT.json").write_text(
            json.dumps({"from_source_manifest_sha256": manifest_sha}), encoding="utf-8"
        )
        report = pf._new_report()
        pf.check_deployment(report, self.root, predecessor_sha256=None)
        self.assertFalse(report["ok"])
        self.assertIn("deployment_self_reference", [i["code"] for i in report["issues"]])

    def test_malformed_hash_is_flagged(self):
        (self.root / "docs/DEPLOYMENT.json").write_text(
            json.dumps({"from_source_manifest_sha256": "stuck-at-v1.6.14"}), encoding="utf-8"
        )
        report = pf._new_report()
        pf.check_deployment(report, self.root, predecessor_sha256=None)
        self.assertFalse(report["ok"])
        self.assertIn("deployment_binding_malformed", [i["code"] for i in report["issues"]])


class VersionAndReviewerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_version_mismatch_is_flagged(self):
        (self.root / "pyproject.toml").write_text('version = "1.6.15"\n', encoding="utf-8")
        report = pf._new_report()
        pf.check_pyproject_version(report, self.root, expected_version="1.6.16")
        self.assertFalse(report["ok"])
        self.assertEqual("1.6.15", report["checks"]["pyproject"]["version"])
        self.assertIn("pyproject_version_mismatch", [i["code"] for i in report["issues"]])

    def test_reviewer_author_conflict_is_flagged(self):
        report = pf._new_report()
        pf.check_reviewer_conflict(report, reviewers=["rev-a", "bob"], authors=["bob"], executors=[])
        self.assertFalse(report["ok"])
        self.assertEqual(["bob"], report["checks"]["reviewer_conflict"]["conflicts"])
        self.assertIn("reviewer_author_conflict", [i["code"] for i in report["issues"]])

    def test_no_conflict_when_reviewers_declared_and_clean(self):
        report = pf._new_report()
        pf.check_reviewer_conflict(report, reviewers=["rev-a"], authors=["alice"], executors=["bob"])
        self.assertTrue(report["ok"])


class ReleaseDispatchContextTest(unittest.TestCase):
    def test_missing_reviewer_value_cannot_swallow_preflight_only(self):
        parser = unittest.mock.Mock()
        parser.error.side_effect = SystemExit(2)
        with self.assertRaises(SystemExit):
            release_submit._extract_preflight(
                parser, ["--preflight-reviewer", "--preflight-only"]
            )
        parser.error.assert_called_once()

    def test_boolean_preflight_flags_reject_inline_values(self):
        for flag in ("--preflight-only=false", "--preflight-no-author=false",
                     "--preflight-no-executor=false"):
            with self.subTest(flag=flag):
                parser = unittest.mock.Mock()
                parser.error.side_effect = SystemExit(2)
                with self.assertRaises(SystemExit):
                    release_submit._extract_preflight(parser, [flag])
                parser.error.assert_called_once()

    def test_release_dispatch_blocks_when_context_is_missing(self):
        stub = SimpleNamespace(preflight=Mock(return_value=pf._new_report()))
        values = {
            "expected_version": None, "predecessor_sha256": None,
            "project_root": None, "assistant_root": None,
            "reviewer": [], "author": [], "executor": [],
            "no_author": False, "no_executor": False,
        }
        with unittest.mock.patch.dict(sys.modules, {"release_preflight": stub}):
            report = release_submit.run_preflight(values, require_dispatch_context=True)
        self.assertFalse(report["ok"])
        self.assertIn("dispatch_preflight_context_missing", [i["code"] for i in report["issues"]])
        self.assertEqual(7, len(report["checks"]["dispatch_context"]["missing"]))

    def test_no_author_and_executor_require_explicit_declarations(self):
        values = {
            "expected_version": "1.6.16", "predecessor_sha256": "a" * 64,
            "project_root": "/tmp/project", "assistant_root": "/tmp/assistant",
            "reviewer": ["reviewer-1"], "author": [], "executor": [],
            "no_author": True, "no_executor": True,
        }
        self.assertEqual([], release_submit._dispatch_context_gaps(values))

    def test_dispatch_preflight_passes_declared_roots_and_identities(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            values = {
                "expected_version": "1.6.16", "predecessor_sha256": "a" * 64,
                "project_root": str(root / "project"), "assistant_root": str(root / "assistant"),
                "reviewer": ["reviewer-1"], "author": ["author-1"], "executor": ["executor-1"],
                "no_author": False, "no_executor": False,
            }
            stub = SimpleNamespace(preflight=Mock(return_value=pf._new_report()))
            with unittest.mock.patch.dict(sys.modules, {"release_preflight": stub}):
                report = release_submit.run_preflight(values, require_dispatch_context=True)
            self.assertTrue(report["ok"])
            self.assertTrue(report["checks"]["dispatch_context"]["complete"])
            args, kwargs = stub.preflight.call_args
            self.assertEqual(root / "project", kwargs["project"])
            self.assertEqual(root / "assistant", kwargs["assistant"])
            self.assertEqual(["reviewer-1"], kwargs["reviewers"])
            self.assertEqual(["author-1"], kwargs["authors"])
            self.assertEqual(["executor-1"], kwargs["executors"])


class SourceManifestFreshnessTest(unittest.TestCase):
    def test_stale_manifest_is_flagged_with_changed_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "docs").mkdir()
            (root / "pyproject.toml").write_text('version = "1.6.15"\n', encoding="utf-8")
            (root / "README.md").write_text("x", encoding="utf-8")
            (root / ".gitignore").write_text("", encoding="utf-8")
            (root / "CAPABILITY.json").write_text("{}", encoding="utf-8")
            # Declare a file that does not match disk.
            (root / "README.md").write_text("changed", encoding="utf-8")
            (root / pf.MANIFEST_NAME).write_text(
                json.dumps({"algorithm": "sha256", "files": {"README.md": _sha(b"old"),
                            "pyproject.toml": _sha(b'version = "1.6.15"\n')}}),
                encoding="utf-8",
            )
            report = pf._new_report()
            pf.check_source_manifest(report, root)
            self.assertFalse(report["ok"])
            self.assertIn("README.md", report["checks"]["source_manifest"]["changed"])


class HostBeforeRetrievalTest(unittest.TestCase):
    """C11-02: invalid host is rejected before retrieval; no formal_retrieved write."""

    def setUp(self):
        from test_card37 import _isolated_config
        from xiaoba_agentos.store import EventStore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = _isolated_config(self.root, [])
        self.store = EventStore(self.config.database_path)
        self.store.initialize()

    def test_invalid_host_rejected_before_memory_retrieval(self):
        from xiaoba_agentos.cli import _submit_task
        invalid_host = {"id": "host-without-entry-or-model"}
        with patch("xiaoba_agentos.cli.MemoryBridge") as memory:
            memory.return_value.agent_contexts.return_value = {
                "hits": [], "contexts": {}, "receipt": None}
            with self.assertRaisesRegex(ValueError, "宿主身份需要 id、entry、model"):
                _submit_task(self.config, self.store, "TEST submit", host=invalid_host)
            memory.return_value.agent_contexts.assert_not_called()
        # No task persisted and no event written on the rejected path.
        with self.store.read_only() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
