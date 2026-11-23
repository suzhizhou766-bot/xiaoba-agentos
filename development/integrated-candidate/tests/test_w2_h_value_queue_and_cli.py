"""W2-H1~H4 & W1-Bug-2 regression tests (read-only CLI, controlled value auto-dispatch, --json, _sensitive_reason)."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaoba_agentos import cli
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.memory import MemoryBridge
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.value_queue import (
    auto_dispatch_pending_value_reviews,
    defer_tx,
    dispatch_value_review_item,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_config(root: Path, *, auto_dispatch: bool = False) -> tuple[Path, AppConfig, EventStore]:
    rt = root / "runtime"
    vault = root / "vault"
    rt.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    cfg_path = root / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_dir": str(rt),
                "memory": {"obsidian_vault": str(vault)},
                "worker": {
                    "poll_seconds": 1,
                    "lease_seconds": 60,
                    "max_attempts": 1,
                    "parallelism": 2,
                    "minimum_successful_agents": 2,
                    "auto_dispatch_value_review": auto_dispatch,
                },
                "agents": [
                    {"name": "mock-a", "type": "mock", "enabled": True, "role": "review"},
                    {"name": "mock-b", "type": "mock", "enabled": True, "role": "review"},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = AppConfig.load(cfg_path)
    store = EventStore(rt / "agentos.sqlite3")
    store.initialize()
    return cfg_path, cfg, store


def _seed_unscored_candidate(root: Path, store: EventStore, title: str = "TEST value candidate") -> tuple[str, str]:
    body = root / f"{title.replace(' ', '_')}.md"
    body.write_text(f"# {title}\n\nSteps: deterministic verification.\n", encoding="utf-8")
    sha = hashlib.sha256(body.read_bytes()).hexdigest()
    task_id = store.submit(
        prompt=title,
        max_attempts=1,
        snapshot={
            "prompt": title,
            "experience_policy": {"version": 1, "kind": "method", "problem_key": "test-pk"},
        },
    )
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE tasks SET status='completed', result='seeded source done' WHERE id=?", (task_id,))
        defer_tx(
            conn,
            task_id,
            {
                "version": 1,
                "candidate_sha256": sha,
                "staged_path": str(body),
                "source_task_ids": [],
                "reason": "no_scoring_source",
            },
        )
    return task_id, sha


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


class W2HValueQueueAndCliTests(unittest.TestCase):
    def test_h1_memory_value_list_never_calls_store_initialize(self):
        """W2-H1: memory-value-list and read-only subcommands skip store.initialize() write lock."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            cfg_path, _, store = _make_config(root)
            _seed_unscored_candidate(root, store)
            with patch.object(EventStore, "initialize", side_effect=AssertionError("must not call initialize()")):
                rc, out, err = _run_cli(["--config", str(cfg_path), "memory-value-list", "--json"])
                self.assertEqual(rc, 0, err)
                data = json.loads(out)
                self.assertEqual(data["status"], "ok")
                self.assertEqual(data["count"], 1)

                rc2, out2, err2 = _run_cli(["--config", str(cfg_path), "repair-status", "--json"])
                self.assertEqual(rc2, 0, err2)
                self.assertTrue(out2.strip())

    def test_h2_h3_controlled_auto_dispatch_four_scenarios_and_ordinary_task_unaffected(self):
        """W2-H2 & W2-H3: (1) auto once, (2) idempotent, (3) failure isolates & preserves normal tasks, (4) switch off."""
        # Scenario 4: switch OFF (default) -> manual only, never auto-dispatches
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            cfg_path, cfg, store = _make_config(root, auto_dispatch=False)
            tid, sha = _seed_unscored_candidate(root, store, "switch-off")
            calls = []
            res_off = auto_dispatch_pending_value_reviews(
                cfg, store, runner=lambda item, rid: calls.append(item["task_id"]) or {"status": "waiting_receipts"}
            )
            self.assertFalse(res_off["enabled"])
            self.assertEqual(calls, [])
            # Explicit CLI dispatch works on demand when user/host triggers it
            with patch("xiaoba_agentos.value_queue._default_value_review_runner",
                       return_value={"status": "waiting_receipts", "reason": "manual trigger ok"}):
                rc, out, err = _run_cli(["--config", str(cfg_path), "memory-value-dispatch", "--task-id", tid, "--json"])
                self.assertEqual(rc, 1, err)  # waiting_receipts -> not completed yet
                payload = json.loads(out)
                self.assertEqual(payload["results"][0]["status"], "waiting_receipts")

        # Scenarios 1 & 2: switch ON -> auto-dispatches ONCE per SHA-256, never repeats
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            _, cfg, store = _make_config(root, auto_dispatch=True)
            tid, sha = _seed_unscored_candidate(root, store, "auto-once")
            calls = []

            def fake_runner(item, rid):
                calls.append((item["task_id"], item["candidate_sha256"], rid))
                return {"status": "waiting_receipts", "reason": "dispatched once"}

            orch = Orchestrator(cfg, store)
            first = orch.dispatch_pending_value_reviews(runner=fake_runner)
            second = orch.dispatch_pending_value_reviews(runner=fake_runner)
            third = orch.dispatch_pending_value_reviews(runner=fake_runner)
            self.assertTrue(first["enabled"])
            self.assertEqual(len(first["dispatched"]), 1)
            self.assertEqual(len(second["dispatched"]), 0)
            self.assertEqual(len(third["dispatched"]), 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][:2], (tid, sha))

        # Scenario 3: node failure/exception -> falls back to manual todo, never retries & never breaks ordinary tasks
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            _, cfg, store = _make_config(root, auto_dispatch=True)
            tid, sha = _seed_unscored_candidate(root, store, "failing-sidechannel")
            # Seed a completed ordinary task to prove side-channel failure never touches it
            normal_tid = store.submit(prompt="ordinary completed task", max_attempts=1)
            with store.connect() as conn:
                conn.execute("UPDATE tasks SET status='completed', result='normal answer' WHERE id=?", (normal_tid,))

            fail_calls = []

            def exploding_runner(item, rid):
                fail_calls.append(item["task_id"])
                raise RuntimeError("simulated pool node crash")

            orch = Orchestrator(cfg, store)
            r1 = orch.dispatch_pending_value_reviews(runner=exploding_runner)
            r2 = orch.dispatch_pending_value_reviews(runner=exploding_runner)
            self.assertEqual(len(fail_calls), 1, "failed dispatch must not loop or burn quota")
            self.assertEqual(r1["dispatched"][0]["status"], "failed")
            self.assertIn("simulated pool node crash", r1["dispatched"][0]["reason"])
            self.assertEqual(len(r2["dispatched"]), 0)
            # Item stays visible in queue with failed dispatch record for manual handling
            q = store.value_review_queue()
            self.assertEqual(len(q), 1)
            self.assertEqual(q[0]["dispatch"]["status"], "failed")
            # Ordinary completed task is 100% untouched
            normal = store.get_task(normal_tid)
            self.assertEqual(normal["status"], "completed")
            self.assertEqual(normal["result"], "normal answer")
            # Even with switch ON, idle orchestrator.run_once() stays resilient when side-channel fails
            with patch("xiaoba_agentos.value_queue._default_value_review_runner", side_effect=RuntimeError("boom")):
                tid2, _ = _seed_unscored_candidate(root, store, "idle-resilience")
                with patch("xiaoba_agentos.orchestrator.release_binding_status", return_value={"ok": True}):
                    idle = orch.run_once(preflight_when_idle=False)
                self.assertEqual(idle["status"], "idle")
                self.assertIn("value_review_auto_dispatch", idle)
                self.assertEqual(idle["value_review_auto_dispatch"]["dispatched"][0]["task_id"], tid2)
                self.assertEqual(idle["value_review_auto_dispatch"]["dispatched"][0]["status"], "failed")

    def test_h4_readonly_subcommands_accept_json_flag_and_skill_clarifies_verify_release(self):
        """W2-H4: common read-only subcommands accept --json; SKILL.md clarifies verify-release."""
        parser = cli.build_parser()
        for argv in (
            ["doctor", "--json"],
            ["status", "--json"],
            ["memory-value-list", "--json"],
            ["review-list", "--json"],
            ["memory-retrieve", "test", "--json"],
            ["memory-retained-list", "--json"],
            ["memory-trial-status", "tid-1", "--json"],
            ["memory-trial-match", "--query", "test", "--json"],
            ["memory-pending-index", "--json"],
            ["task-delivery-view", "--json"],
            ["show", "tid-1", "--json"],
            ["skill-list", "--json"],
            ["knowledge-list", "--json"],
            ["repair-status", "--json"],
            ["capability-verify", "--json"],
            ["workday-read", "--json"],
        ):
            parsed = parser.parse_args(argv)
            self.assertTrue(getattr(parsed, "json", False), f"--json not parsed for {argv}")

        skill_md = (REPO_ROOT / "development/interface/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("verify-release", skill_md)
        self.assertIn("独立脚本", skill_md)

    def test_w1_bug2_sensitive_reason_avoids_auth_prose_false_positives_while_blocking_real_secrets(self):
        """W1-Bug-2 (← 2026-11-23): block real credentials, do not false-positive auth prose/code."""
        # Legitimate technical prose / code / synthetic demo fixtures MUST pass
        benign = [
            "key, use_x_api_key = self.client_key()",
            "use_x_api_key = self.client.get_key()",
            "Authorization: 凭据头鉴权测试说明，非真实密钥",
            "Authorization: Bearer <master-key>",
            "Authorization: Bearer demo-pool-cred-43ead088",
            "Authorization: Bearer wrong-cred",
            "x-api-key: demo-pool-cred-43ead088",
            "api_key: 填写 Key 池主密钥即可",
            "password = load_from_vault()",
        ]
        for sample in benign:
            with self.subTest(benign=sample):
                self.assertIsNone(MemoryBridge._sensitive_reason(sample))

        # Real-shaped secrets MUST remain blocked (including prefixed YAML fields & symbol passwords)
        must_block = [
            ("authorization", "Authorization: Bearer abcdefghijklmnop"),
            ("authorization", "Authorization: Bearer P@ss!tok#99"),
            ("authorization", "Authorization: Basic dXNlcjpwYXNzMTIzNA=="),
            ("secret_field", "api_key=RealProdKey99887766"),
            ("secret_field", "x-api-key: prodLiveKey987654321"),
            ("secret_field", "password: MySuperSecret123"),
            ("secret_field", "password: P@ss!23456"),
            ("secret_field", "db_password: S3cr$t#99"),
            ("secret_field", "stripe_api_key: live_98a7b6c5d4e3"),
            ("known_token_prefix", "Authorization: Bearer demo-sk-real1234567890abcdef"),
            ("oauth_token", "refresh_token=FAKE_ONLY_12345678901234567890"),
            ("aws_access_key_id", "AKIAIOSFODNN7EXAMPLE"),
        ]
        for expected, sample in must_block:
            with self.subTest(secret=sample):
                self.assertEqual(expected, MemoryBridge._sensitive_reason(sample))


if __name__ == "__main__":
    unittest.main()
