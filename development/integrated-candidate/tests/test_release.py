import hashlib
import importlib.util
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from xiaoba_agentos.config import AppConfig, ConfigError
from xiaoba_agentos.release import _source_manifest_payload, release_binding_status
import os
import subprocess
import sys
from fixtures_review import _sha


BUILD_RELEASE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_release.py"
BUILD_RELEASE_SPEC = importlib.util.spec_from_file_location("build_release", BUILD_RELEASE_SCRIPT)
BUILD_RELEASE_MODULE = importlib.util.module_from_spec(BUILD_RELEASE_SPEC)
assert BUILD_RELEASE_SPEC and BUILD_RELEASE_SPEC.loader
BUILD_RELEASE_SPEC.loader.exec_module(BUILD_RELEASE_MODULE)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReleaseBindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.releases = self.root / "releases"
        self.release_dir = self.releases / "v1.3"
        (self.workspace / "src" / "xiaoba_agentos").mkdir(parents=True)
        (self.workspace / "bin").mkdir()
        (self.workspace / "config").mkdir()
        (self.workspace / "src" / "xiaoba_agentos" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.workspace / "bin" / "agentos").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.workspace / "config" / "agentos.json").write_text("{}\n", encoding="utf-8")
        (self.workspace / "README.md").write_text("test\n", encoding="utf-8")
        (self.workspace / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
        (self.workspace / ".gitignore").write_text(".runtime/\n", encoding="utf-8")
        (self.workspace / "CAPABILITY.json").write_text("{}\n", encoding="utf-8")
        adapter = self.workspace / ".agents" / "skills" / "xiaoba-agentos"
        adapter.mkdir(parents=True)
        (adapter / "SKILL.md").write_text(
            "---\nname: xiaoba-agentos\ndescription: adapter\n---\n",
            encoding="utf-8",
        )
        self._write_source_manifest(self.workspace)

        self.release_dir.mkdir(parents=True)
        shutil.copytree(self.workspace, self.release_dir / "agentos")
        source_hash = _sha256(self.release_dir / "agentos" / "SOURCE_MANIFEST.json")
        (self.release_dir / "RELEASE.json").write_text(
            json.dumps(
                {
                    "product": "小八 AgentOS",
                    "version": "v1.3",
                    "status": "verified_immutable_release",
                    "agentos_source_manifest_sha256": source_hash,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self._write_files_manifest()
        self.index = self.releases / "index.json"
        self._write_index("v1.3")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_source_manifest(self, root: Path) -> None:
        problems = []
        payload = _source_manifest_payload(root, problems)
        self.assertEqual([], problems)
        self.assertIsNotNone(payload)
        (root / "SOURCE_MANIFEST.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _write_files_manifest(self) -> None:
        target = self.release_dir / "FILES.sha256"
        lines = []
        for path in sorted(self.release_dir.rglob("*")):
            if path.is_file() and path != target:
                lines.append(f"{_sha256(path)}  ./{path.relative_to(self.release_dir).as_posix()}")
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_index(self, current: str) -> None:
        self.index.write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "current_release": current,
                    "releases": [
                        {
                            "version": "v1.3",
                            "path": "v1.3",
                            "status": "verified_immutable",
                            "files_manifest_sha256": _sha256(self.release_dir / "FILES.sha256"),
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _config(self) -> AppConfig:
        return AppConfig(
            self.root / "config.json",
            {
                "schema_version": 2,
                "project_dir": str(self.workspace),
                "release": {"index_path": str(self.index), "expected_version": "v1.3"},
                "agents": [{"name": "real", "type": "cc_switch_cli"}],
            },
        )

    def test_verified_runtime_is_bound_to_indexed_release(self):
        result = release_binding_status(self._config())
        self.assertTrue(result["ok"], result)
        self.assertEqual("v1.3", result["current_release"])
        self.assertEqual(
            result["release_source_manifest_sha256"], result["runtime_source_manifest_sha256"]
        )

    def test_current_release_mismatch_fails_closed(self):
        self._write_index("v1.2")
        result = release_binding_status(self._config())
        self.assertFalse(result["ok"])
        self.assertTrue(any("current_release" in item for item in result["problems"]))

    def test_runtime_change_is_detected_even_when_manifest_file_is_unchanged(self):
        (self.workspace / "src" / "xiaoba_agentos" / "core.py").write_text("VALUE = 2\n", encoding="utf-8")
        result = release_binding_status(self._config())
        self.assertFalse(result["ok"])
        self.assertTrue(any("runtime source manifest mismatch" in item for item in result["problems"]))

    def test_antigravity_skill_adapter_is_part_of_runtime_manifest(self):
        adapter = self.workspace / ".agents" / "skills" / "xiaoba-agentos"
        adapter.mkdir(parents=True, exist_ok=True)
        (adapter / "SKILL.md").write_text(
            "---\nname: xiaoba-agentos\ndescription: adapter\n---\n",
            encoding="utf-8",
        )
        self._write_source_manifest(self.workspace)
        result = release_binding_status(self._config())
        self.assertTrue(result["ok"], result)

    def test_finder_metadata_is_ignored_by_runtime_manifest(self):
        metadata = self.workspace / ".DS_Store"
        metadata.write_bytes(b"finder metadata")
        nested = self.workspace / ".agents" / "skills"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / ".DS_Store").write_bytes(b"finder metadata")
        self._write_source_manifest(self.workspace)
        result = release_binding_status(self._config())
        self.assertTrue(result["ok"], result)

    def test_release_file_change_is_detected_by_full_manifest(self):
        (self.release_dir / "agentos" / "README.md").write_text("tampered\n", encoding="utf-8")
        result = release_binding_status(self._config())
        self.assertFalse(result["ok"])
        self.assertTrue(any("release file hash mismatch" in item for item in result["problems"]))

    def test_missing_release_policy_fails_for_non_mock_runtime(self):
        config = self._config()
        config.raw.pop("release")
        result = release_binding_status(config)
        self.assertFalse(result["ok"])
        self.assertIn("release policy is missing", result["problems"])

    def test_schema_one_mock_config_explicitly_skips_binding(self):
        config = AppConfig(
            self.root / "mock.json",
            {
                "schema_version": 1,
                "project_dir": str(self.workspace),
                "agents": [{"name": "mock", "type": "mock"}],
            },
        )
        result = release_binding_status(config)
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])

    def test_candidate_cannot_bind_as_active_production_release(self):
        release = json.loads((self.release_dir / "RELEASE.json").read_text(encoding="utf-8"))
        release["status"] = "release_candidate"
        (self.release_dir / "RELEASE.json").write_text(
            json.dumps(release, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self._write_files_manifest()
        self._write_index("v1.3")
        result = release_binding_status(self._config())
        self.assertFalse(result["ok"])
        self.assertTrue(any("RELEASE.json" in item for item in result["problems"]))

    def test_candidate_acceptance_mode_is_temp_only_and_cannot_publish(self):
        release = json.loads((self.release_dir / "RELEASE.json").read_text(encoding="utf-8"))
        release["status"] = "release_candidate"
        (self.release_dir / "RELEASE.json").write_text(
            json.dumps(release, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self._write_files_manifest()
        index = json.loads(self.index.read_text(encoding="utf-8"))
        index["releases"][0]["status"] = "release_candidate"
        index["releases"][0]["files_manifest_sha256"] = _sha256(
            self.release_dir / "FILES.sha256"
        )
        self.index.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        config = self._config()
        config.raw["runtime_dir"] = str(self.root / "runtime")
        config.raw["memory"] = {"publish_enabled": False}
        config.raw["release"]["acceptance_mode"] = True
        result = release_binding_status(config)
        self.assertTrue(result["ok"], result)


class ReleaseAcceptanceEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "agentos.json").write_text(
            json.dumps(
                {
                    "worker": {"minimum_successful_agents": 2},
                    "agents": [
                        {"name": "south-node", "enabled": True},
                        {"name": "north-node", "enabled": True},
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.root / "SOURCE_MANIFEST.json").write_text("{}\n", encoding="utf-8")
        self.source_manifest_sha256 = _sha256(self.root / "SOURCE_MANIFEST.json")
        self.evidence = self.root / "evidence.json"

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self, result="本回答：小八 AgentOS 双 Agent 裁判\n\n有效结论"):
        return {
            "task": {
                "id": "task-1",
                "status": "completed",
                "execution_mode": "production",
                "result": result,
                "provenance_json": json.dumps(
                    {"kind": "agentos_multi_adjudicated", "minimum_successful_agents": 2}
                ),
                "execution_snapshot_json": json.dumps(
                    {"source_manifest_sha256": self.source_manifest_sha256}
                ),
            },
            "runs": [
                {"agent_name": "south-node", "status": "completed", "output": "FINAL_ACCEPTANCE: PASS\nA"},
                {"agent_name": "north-node", "status": "completed", "output": "FINAL_ACCEPTANCE: PASS\nB"},
            ],
            "memory_candidate": {"status": "pending_delivery_review"},
            "pool_nodes": [
                {"node_id": "south-node", "enabled": True, "test_passed": True,
                 "independence": ["cli:codex", "alpha"]},
                {"node_id": "north-node", "enabled": True, "test_passed": True,
                 "independence": ["cli:claude", "beta"]},
            ],
            "execution": {"author_ids": [], "executor_ids": [], "checked_idle_ids": []},
        }

    def audit_payload(self):
        payload = self.payload()
        snapshot = {"source_manifest_sha256": self.source_manifest_sha256,
                    "experience_policy": {"version": 1, "kind": "audit"}}
        canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload["task"]["execution_snapshot_json"] = canonical
        payload["task"]["execution_snapshot_hash"] = hashlib.sha256(canonical.encode()).hexdigest()
        result_sha = hashlib.sha256(payload["task"]["result"].encode()).hexdigest()
        payload["task"]["provenance_json"] = json.dumps({"kind": "agentos_multi_adjudicated", "minimum_successful_agents": 2,
                                                       "memory_disposition": "audit_only", "result_sha256": result_sha})
        payload["memory_candidate"] = None
        payload["audit_settlement"] = {"result_sha256": result_sha}
        return payload

    def test_audit_acceptance_keeps_dual_review_and_exact_settlement_without_candidate(self):
        payload = self.audit_payload()
        self.evidence.write_text(json.dumps(payload))
        result = BUILD_RELEASE_MODULE.validate_acceptance_evidence(self.evidence, self.root)
        self.assertEqual(result["candidate_status"], "audit_only")
        for key in ("audit_settlement",):
            broken = self.audit_payload()
            broken.pop(key)
            self.evidence.write_text(json.dumps(broken))
            with self.assertRaisesRegex(RuntimeError, "audit settlement"):
                BUILD_RELEASE_MODULE.validate_acceptance_evidence(self.evidence, self.root)

    def test_audit_acceptance_rejects_changed_result_and_snapshot(self):
        for key in ("result", "execution_snapshot_hash"):
            payload = self.audit_payload()
            payload["task"][key] += "changed"
            self.evidence.write_text(json.dumps(payload))
            with self.assertRaises(RuntimeError):
                BUILD_RELEASE_MODULE.validate_acceptance_evidence(self.evidence, self.root)

    def test_formal_release_requires_real_two_agent_evidence(self):
        self.evidence.write_text(json.dumps(self.payload()), encoding="utf-8")
        result = BUILD_RELEASE_MODULE.validate_acceptance_evidence(self.evidence, self.root)
        self.assertEqual(["north-node", "south-node"], result["successful_agents"])
        self.assertEqual("task-1", result["task_id"])

    def test_tool_call_markup_cannot_be_release_evidence(self):
        payload = self.payload(
            '本回答：小八 AgentOS 双 Agent 裁判\n<invoke name="Read"></invoke>'
        )
        self.evidence.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            BUILD_RELEASE_MODULE.validate_acceptance_evidence(self.evidence, self.root)

    def test_acceptance_rejects_quoted_conflicting_or_markup_verdicts(self):
        for output in (
            "Do not return FINAL_ACCEPTANCE: PASS; this failed.",
            "FINAL_ACCEPTANCE: PASS\nFINAL_ACCEPTANCE: FAIL",
            'FINAL_ACCEPTANCE: PASS\n<invoke name="Read"></invoke>',
        ):
            with self.subTest(output=output):
                payload = self.payload()
                payload["runs"][1]["output"] = output
                self.evidence.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    BUILD_RELEASE_MODULE.validate_acceptance_evidence(self.evidence, self.root)

    def test_negative_agent_verdict_cannot_be_release_evidence(self):
        payload = self.payload()
        payload["runs"][1]["output"] = "FINAL_ACCEPTANCE: FAIL\n存在阻断"
        self.evidence.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            BUILD_RELEASE_MODULE.validate_acceptance_evidence(self.evidence, self.root)

    def test_all_eight_non_answer_markers_rejected_in_task_result(self):
        markers = getattr(BUILD_RELEASE_MODULE, "NON_ANSWER_MARKERS", (
            "<invoke ", "</invoke>", "<parameter ", "</parameter>",
            "<｜dsml｜", "<|dsml|", "tool-call serialization", "tool call serialization",
        ))
        for marker in markers:
            for variant in (f"前缀 {marker} 后缀", f"前缀 {marker.upper()} 后缀"):
                with self.subTest(variant=variant):
                    payload = self.payload(f"本回答：小八 AgentOS 双 Agent 裁判\n{variant}")
                    self.evidence.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, "unexecuted tool-call markup"):
                        BUILD_RELEASE_MODULE.validate_acceptance_evidence(self.evidence, self.root)

    def test_all_eight_non_answer_markers_rejected_in_agent_output(self):
        markers = getattr(BUILD_RELEASE_MODULE, "NON_ANSWER_MARKERS", (
            "<invoke ", "</invoke>", "<parameter ", "</parameter>",
            "<｜dsml｜", "<|dsml|", "tool-call serialization", "tool call serialization",
        ))
        for marker in markers:
            for variant in (f"前缀 {marker} 后缀", f"前缀 {marker.upper()} 后缀"):
                with self.subTest(variant=variant):
                    payload = self.payload()
                    payload["runs"][1]["output"] = f"FINAL_ACCEPTANCE: PASS\n{variant}"
                    self.evidence.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, "unexecuted tool-call markup"):
                        BUILD_RELEASE_MODULE.validate_acceptance_evidence(self.evidence, self.root)

    def test_acceptance_verdict_rules_matrix(self):
        # 1. 唯一独立 PASS 正常通过
        payload = self.payload()
        payload["runs"][1]["output"] = "依据充分，无阻断问题。\nFINAL_ACCEPTANCE: PASS"
        self.evidence.write_text(json.dumps(payload), encoding="utf-8")
        res = BUILD_RELEASE_MODULE.validate_acceptance_evidence(self.evidence, self.root)
        self.assertEqual("task-1", res["task_id"])

        # 2. 无结论、带后缀、双结论、独立 FAIL、正文引用 FAIL 均拒绝
        rejection_cases = (
            ("无结论", "经过全面审查，所有测试全部通过。"),
            ("带后缀", "FINAL_ACCEPTANCE: PASS（附带条件）"),
            ("双结论", "FINAL_ACCEPTANCE: PASS\nFINAL_ACCEPTANCE: FAIL"),
            ("独立FAIL", "FINAL_ACCEPTANCE: FAIL"),
            ("正文引用FAIL", "历史反例记录为 FINAL_ACCEPTANCE: FAIL\nFINAL_ACCEPTANCE: PASS"),
        )
        for label, bad_output in rejection_cases:
            with self.subTest(label=label):
                bad_payload = self.payload()
                bad_payload["runs"][1]["output"] = bad_output
                self.evidence.write_text(json.dumps(bad_payload), encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    BUILD_RELEASE_MODULE.validate_acceptance_evidence(self.evidence, self.root)

    def test_acceptance_database_binds_task_runs_judge_and_candidate(self):
        database = self.root / "acceptance.sqlite3"
        connection = sqlite3.connect(database)
        connection.executescript(
            """
            CREATE TABLE tasks (
                id TEXT, status TEXT, execution_mode TEXT, result TEXT, provenance_json TEXT,
                execution_snapshot_json TEXT
            );
            CREATE TABLE agent_runs (
                task_id TEXT, agent_name TEXT, status TEXT, output TEXT, created_at REAL,
                failure_class TEXT
            );
            CREATE TABLE memory_candidates (
                task_id TEXT, status TEXT, evidence_sha256 TEXT
            );
            CREATE TABLE events (
                task_id TEXT, event_type TEXT, payload_json TEXT, sequence INTEGER
            );
            CREATE TABLE agent_health (
                agent_name TEXT, status TEXT, provider TEXT, model TEXT, last_probe_at REAL
            );
            CREATE TABLE ri_sources (
                id TEXT PRIMARY KEY, data TEXT NOT NULL
            );
            """
        )
        payload = self.payload()
        task = payload["task"]
        connection.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?)",
            (
                task["id"], task["status"], task["execution_mode"], task["result"],
                task["provenance_json"], task["execution_snapshot_json"],
            ),
        )
        for index, run in enumerate(payload["runs"]):
            connection.execute(
                "INSERT INTO agent_runs VALUES (?,?,?,?,?,?)",
                (task["id"], run["agent_name"], run["status"], run["output"], float(index), None),
            )
        connection.execute(
            "INSERT INTO memory_candidates VALUES (?,?,?)",
            (task["id"], "pending_delivery_review", "a" * 64),
        )
        connection.execute(
            "INSERT INTO events VALUES (?,?,?,?)",
            (
                task["id"],
                "judge.decided",
                json.dumps({"winner": "south-node", "successful_agents": 2}),
                1,
            ),
        )
        connection.executemany(
            "INSERT INTO agent_health VALUES (?,?,?,?,?)",
            [
                ("south-node", "healthy", "福利 gpt", "test-model", 1.0),
                ("north-node", "healthy", "福利 gpt", "test-model", 1.0),
            ],
        )
        for node_id, entry, model in (
            ("south-node", "cli:codex", "alpha"),
            ("north-node", "cli:claude", "beta"),
        ):
            connection.execute(
                "INSERT INTO ri_sources VALUES (?,?)",
                (node_id, json.dumps({
                    "reviewer_id": node_id, "kind": "review",
                    "source_ref": "agent-entry:" + node_id,
                    "node": {
                        "enabled": True, "removed": False, "interface": "cli",
                        "entry": entry, "model": model, "agent_type": entry.split(":")[-1],
                        "test": {"status": "passed"},
                    },
                }, ensure_ascii=False)),
            )
        connection.commit()
        connection.close()
        result = BUILD_RELEASE_MODULE.validate_acceptance_database(database, "task-1", self.root)
        self.assertEqual("a" * 64, result["candidate_sha256"])
        self.assertEqual(["north-node", "south-node"], result["successful_agents"])

        connection = sqlite3.connect(database)
        connection.execute("DELETE FROM events WHERE event_type='judge.decided'")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(RuntimeError, "judge"):
            BUILD_RELEASE_MODULE.validate_acceptance_database(database, "task-1", self.root)


ROOT = Path(__file__).resolve().parents[1]


SCRIPTS = ROOT / ".agents" / "skills" / "xiaoba-agentos" / "scripts"


def _load_entry():
    loader = importlib.machinery.SourceFileLoader("xiaoba_entry_under_test", str(SCRIPTS / "xiaoba"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class EntrypointSelfVerificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.entry = _load_entry()
        self.release = self.root / "releases" / "v9"
        source = self.release / "agentos" / "src" / "xiaoba_agentos"
        source.mkdir(parents=True)
        (source / "__init__.py").write_text("", encoding="utf-8")
        (source / "release.py").write_text("VERIFIER = 'release'\n", encoding="utf-8")
        (self.release / "RELEASE.json").write_text("{}", encoding="utf-8")
        self.config = self.root / "config.json"
        self._seal()

    def _seal(self, version="v9", path="v9"):
        files = sorted(p for p in self.release.rglob("*") if p.is_file() and p.name != "FILES.sha256")
        manifest = self.release / "FILES.sha256"
        manifest.write_text(
            "".join(f"{_sha(p)}  ./{p.relative_to(self.release).as_posix()}\n" for p in files), encoding="utf-8")
        index = self.root / "releases" / "index.json"
        index.write_text(json.dumps({"current_release": version, "releases": [
            {"version": version, "path": path, "status": "verified_immutable",
             "files_manifest_sha256": _sha(manifest)}]}), encoding="utf-8")
        self.config.write_text(json.dumps(
            {"release": {"index_path": str(index), "expected_version": version}}), encoding="utf-8")

    def test_intact_release_yields_its_own_source_directory(self):
        self.assertEqual(self.release / "agentos" / "src", self.entry.verified_release_source(self.config))

    def test_any_tampering_with_the_release_is_refused(self):
        def verifier(release):
            return release / "agentos" / "src" / "xiaoba_agentos" / "release.py"

        def rewrite_manifest(release):
            verifier(release).write_text("VERIFIER = 'switched off'\n", encoding="utf-8")
            (release / "FILES.sha256").write_text(
                "".join(f"{_sha(p)}  ./{p.relative_to(release).as_posix()}\n"
                        for p in sorted(release.rglob("*")) if p.is_file() and p.name != "FILES.sha256"),
                encoding="utf-8")

        cases = {
            "modified verifier": lambda r: verifier(r).write_text("VERIFIER = 'switched off'\n", encoding="utf-8"),
            "extra file": lambda r: (r / "agentos" / "extra.py").write_text("x", encoding="utf-8"),
            "missing file": lambda r: (r / "RELEASE.json").unlink(),
            "symlink": lambda r: os.symlink(verifier(r), r / "agentos" / "link.py"),
            # Re-hashing the tree does not help: the index pins the manifest itself.
            "manifest edited to match": rewrite_manifest,
        }
        for label, tamper in cases.items():
            with self.subTest(label):
                self.setUp()
                self.assertTrue(self.entry.verified_release_source(self.config).is_dir())
                tamper(self.release)
                with self.assertRaises(self.entry.ReleaseUnverified):
                    self.entry.verified_release_source(self.config)

    def test_release_path_cannot_escape_the_releases_directory(self):
        outside = self.root / "elsewhere"
        outside.mkdir()
        for path in ("../elsewhere", str(outside), ""):
            with self.subTest(path=path):
                self._seal(path=path)
                with self.assertRaises(self.entry.ReleaseUnverified):
                    self.entry.verified_release_source(self.config)

    def test_scripts_never_import_the_mutable_workspace(self):
        for name in ("xiaoba", "verify-release"):
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            self.assertNotIn("LIVE_SOURCE", text)
            self.assertNotIn("小八AgentOS/src", text)

    def test_tampered_release_is_reported_as_an_invalid_binding(self):
        (self.release / "agentos" / "src" / "xiaoba_agentos" / "release.py").write_text("x = 1\n", encoding="utf-8")
        for name in ("xiaoba", "verify-release"):
            result = subprocess.run([sys.executable, str(SCRIPTS / name), "--config", str(self.config)],
                                    capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
            self.assertEqual(1, result.returncode)
            self.assertIn("release_binding_invalid", result.stdout + result.stderr)


class ReleaseBindingConfigTest(unittest.TestCase):
    """Migrated from tests/test_gateway.py::test_non_mock_config_requires_release_binding."""

    def _load(self, raw):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            return AppConfig.load(path)

    def test_non_mock_config_requires_release_binding(self):
        raw = json.loads((ROOT / "config" / "agentos.json").read_text(encoding="utf-8"))
        self._load(raw)
        raw.pop("release")
        with self.assertRaises(ConfigError):
            self._load(raw)


if __name__ == "__main__":
    unittest.main()
