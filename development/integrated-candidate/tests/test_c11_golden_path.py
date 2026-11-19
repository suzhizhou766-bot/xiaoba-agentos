"""C11-03: 20-second real-subprocess public-CLI golden path.

Every command below is launched as a real ``sys.executable -m xiaoba_agentos.cli``
(or release_preflight.py) subprocess against a *temporary* config/DB/source
tree — no helper or main() is called in-process, no production path, model,
backend or fixed port is touched. A shared 20-second wall-clock deadline bounds
the whole path. At least one case asserts a rejection that leaves no retrieval
event and no persisted task (副作用反例).
"""
import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

CANDIDATE = Path(__file__).resolve().parents[1]
PY = sys.executable
CLI_ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(CANDIDATE / "src"))
GOLDEN_DEADLINE_SECONDS = 20


def _run(args, env, cwd, timeout):
    return subprocess.run(
        [PY, "-B", *args], env=env, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )


class GoldenPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.deadline = time.monotonic() + GOLDEN_DEADLINE_SECONDS
        self.results = {}

    def _remaining(self):
        return max(1.0, self.deadline - time.monotonic())

    def _clean_source_tree(self):
        """A self-contained, fresh source tree whose preflight passes."""
        src = self.root / "source"
        (src / "docs").mkdir(parents=True)
        (src / "scripts").mkdir()
        (src / "src/xiaoba_agentos/review_ui_assets").mkdir(parents=True)
        (src / "README.md").write_text("golden path fixture", encoding="utf-8")
        (src / "pyproject.toml").write_text('version = "1.6.15"\n', encoding="utf-8")
        (src / ".gitignore").write_text("", encoding="utf-8")
        (src / "CAPABILITY.json").write_text("{}", encoding="utf-8")
        (src / "docs/DEPLOYMENT.json").write_text(json.dumps(
            {"schema_version": 1, "from_source_manifest_sha256": "a" * 64, "steps": []}), encoding="utf-8")
        shutil.copytree(
            CANDIDATE / "src/xiaoba_agentos/review_ui_assets",
            src / "src/xiaoba_agentos/review_ui_assets", dirs_exist_ok=True,
        )
        for script in ("submit_release_acceptance.py", "release_preflight.py", "create_manifest.py"):
            shutil.copyfile(CANDIDATE / "scripts" / script, src / "scripts" / script)
        for mod in ("config.py", "release.py", "review_ui_install.py"):
            shutil.copyfile(CANDIDATE / "src/xiaoba_agentos" / mod,
                            src / "src/xiaoba_agentos" / mod)
        (src / "src/xiaoba_agentos/__init__.py").write_text("", encoding="utf-8")
        # Generate a fresh manifest using the same tool the real build uses.
        env = dict(CLI_ENV, PYTHONPATH=str(CANDIDATE / "src") + ":" + str(CANDIDATE / "scripts"))
        gen = _run(["-c",
                    "import json,sys; from pathlib import Path; sys.path.insert(0,'scripts');"
                    "from create_manifest import collect_entries;"
                    "print(json.dumps({'algorithm':'sha256','files':collect_entries(Path('.'))}))"],
                   env, src, self._remaining())
        self.assertEqual(0, gen.returncode, gen.stderr)
        (src / "SOURCE_MANIFEST.json").write_text(gen.stdout.strip() + "\n", encoding="utf-8")
        return src

    def _init_temp_db(self):
        from xiaoba_agentos.config import AppConfig
        from xiaoba_agentos.store import EventStore
        sys.path.insert(0, str(CANDIDATE / "src"))
        sys.path.insert(0, str(CANDIDATE / "tests"))
        from fixtures_review import _raw_config
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(_raw_config(self.root)), encoding="utf-8")
        cfg = AppConfig.load(config_path)
        EventStore(cfg.database_path).initialize()
        return config_path

    def test_golden_path_within_20_seconds(self):
        started = time.monotonic()
        config_path = self._init_temp_db()

        # 1) Public read-only query entry: memory-pending-index (empty store).
        p = _run(["-m", "xiaoba_agentos.cli", "--config", str(config_path), "memory-pending-index"],
                 CLI_ENV, CANDIDATE, self._remaining())
        self.assertEqual(0, p.returncode, p.stderr)
        payload = json.loads(p.stdout)
        self.assertEqual(1, payload["version"])
        self.assertEqual([], payload["entries"])
        self.results["pending_index"] = True

        # 2) Second read-only entry: memory-trial-match (suggest-only, no execution).
        p = _run(["-m", "xiaoba_agentos.cli", "--config", str(config_path),
                  "memory-trial-match", "--problem-key", "nonexistent"],
                 CLI_ENV, CANDIDATE, self._remaining())
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertEqual("suggest_only", json.loads(p.stdout)["action"])
        self.results["trial_match"] = True

        # 3) Deterministic preflight entry on a clean temp source tree: passes.
        src = self._clean_source_tree()
        pf_env = dict(CLI_ENV, PYTHONPATH=str(CANDIDATE / "src") + ":" + str(CANDIDATE / "scripts"))
        p = _run(["scripts/release_preflight.py", "--agentos-root", str(src),
                  "--expected-version", "1.6.15", "--predecessor-sha256", "a" * 64,
                  "--reviewer", "rev-1"], pf_env, CANDIDATE, self._remaining())
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertTrue(json.loads(p.stdout)["ok"])
        self.results["preflight"] = True

        # 4) Rejection with no side effect: release-acceptance submit without a
        #    work-task declaration is blocked before any retrieval; no task row
        #    and no memory.formal_retrieved event are written.
        p = _run(["-m", "xiaoba_agentos.cli", "--config", str(config_path), "submit",
                  "TEST golden rejection", "--release-acceptance", "--memory-kind", "audit"],
                 CLI_ENV, CANDIDATE, self._remaining())
        self.assertNotEqual(0, p.returncode, p.stdout)
        rejected = json.loads(p.stdout)
        self.assertEqual("blocked", rejected["status"])
        self.assertIn("来源工作任务", rejected["error"])
        from xiaoba_agentos.store import EventStore
        from xiaoba_agentos.config import AppConfig
        store = EventStore(AppConfig.load(config_path).database_path)
        with store.read_only() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
            formal = db.execute(
                "SELECT COUNT(*) FROM events WHERE event_type LIKE '%formal_retrieved%'"
            ).fetchone()[0]
        self.assertEqual(0, formal)
        self.results["rejection_no_side_effect"] = True

        # 5) The release-acceptance wrapper itself fails closed when the
        #    operator has not declared every preflight identity/binding/root.
        with store.read_only() as db:
            task_count_before = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            event_count_before = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        p = _run(["scripts/submit_release_acceptance.py", "--config", str(config_path),
                  "TEST release acceptance"], CLI_ENV, CANDIDATE, self._remaining())
        self.assertNotEqual(0, p.returncode, p.stdout)
        wrapper_report = json.loads(p.stderr)
        self.assertIn("dispatch_preflight_context_missing",
                      [issue["code"] for issue in wrapper_report["issues"]])
        with store.read_only() as db:
            self.assertEqual(task_count_before, db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
            self.assertEqual(event_count_before, db.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        self.results["release_wrapper_requires_context"] = True

        # 6) A fresh bundle passes against two real temporary install roots;
        # drifting one target then blocks the actual wrapper before CLI import.
        from xiaoba_agentos.review_ui_install import TARGETS
        roots = {"project": self.root / "install-project", "assistant": self.root / "install-assistant"}
        for asset, (base, relative) in TARGETS.items():
            target = roots[base] / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((src / "src/xiaoba_agentos/review_ui_assets" / asset).read_bytes())
        args = [str(src / "scripts/submit_release_acceptance.py"), "--config", str(config_path),
                "--preflight-expected-version", "1.6.15",
                "--preflight-predecessor-sha256", "a" * 64,
                "--preflight-project-root", str(roots["project"]),
                "--preflight-assistant-root", str(roots["assistant"]),
                "--preflight-reviewer", "rev-1", "--preflight-no-author", "--preflight-no-executor"]
        p = _run([*args, "--preflight-only"], CLI_ENV, src, self._remaining())
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertTrue(json.loads(p.stderr)["ok"])
        (roots["assistant"] / TARGETS["xiaoba-monitor.html"][1]).write_text("TEST unexpected drift", encoding="utf-8")
        p = _run([*args, "TEST drift must stop dispatch", "--no-work-task"], CLI_ENV, src, self._remaining())
        self.assertEqual(1, p.returncode, p.stderr)
        drift_report = json.loads(p.stderr)
        self.assertIn("ui_target_drift", [item["code"] for item in drift_report["issues"]])
        claimed_sha = drift_report.pop("report_sha256")
        self.assertEqual(claimed_sha, hashlib.sha256(json.dumps(
            drift_report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        with store.read_only() as db:
            self.assertEqual(task_count_before, db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
            self.assertEqual(event_count_before, db.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        self.results["dual_root_drift_stops_dispatch"] = True

        elapsed = time.monotonic() - started
        self.assertLess(elapsed, GOLDEN_DEADLINE_SECONDS,
                        f"golden path exceeded 20s: {elapsed:.2f}s")
        self.assertEqual(
            {"pending_index", "trial_match", "preflight", "rejection_no_side_effect",
             "release_wrapper_requires_context", "dual_root_drift_stops_dispatch"},
            set(self.results),
        )


if __name__ == "__main__":
    unittest.main()
