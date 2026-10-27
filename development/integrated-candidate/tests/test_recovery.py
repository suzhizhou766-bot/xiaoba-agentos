import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaoba_agentos.adapters import (
    AgentResult,
    TransientAdapterError,
    _OUTPUT_LIMIT_ERROR,
    _output_limit_failure,
)
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.recovery import (
    HEALTH_MARKER,
    classify_failure,
    live_agent_probe,
    start_session_health,
    sanitize_diagnostic_text,
)
from xiaoba_agentos.store import EventStore


class RecoveryTest(unittest.TestCase):
    def config(self, root, agents=None):
        raw = {
            "schema_version": 1,
            "runtime_dir": str(root / "runtime"),
            "project_dir": str(root),
            "worker": {
                "poll_seconds": 1, "lease_seconds": 60, "max_attempts": 3,
                "retry_base_seconds": 1, "retry_max_seconds": 2, "parallelism": 2,
                "minimum_successful_agents": 2, "max_agent_invocations_per_task": 6,
            },
            "memory": {
                "knowlp_enabled": False, "obsidian_vault": str(root),
                "obsidian_subdir": "AgentOS", "publish_enabled": False,
            },
            "integrations": {},
            "agents": agents or [
                {"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
                {"name": "b", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
            ],
            "recovery": {
                "repair_max_attempts": 2, "repair_retry_seconds": 1,
                "resume_requires_user": True,
                "automatic_actions": ["restart_owned_headroom_and_reprobe"],
            },
            "evolution": {"minimum_samples": 1, "promotion_margin": 0.05, "latency_penalty": 0.01},
        }
        path = root / "config.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return AppConfig.load(path)

    def test_failure_classifier_separates_internal_and_external(self):
        headroom = TransientAdapterError("Headroom unavailable")
        headroom.failure_class = "internal_headroom"
        headroom.repairable = True
        self.assertTrue(classify_failure(headroom).repairable)
        upstream = TransientAdapterError("upstream disconnected")
        upstream.external_upstream = True
        self.assertTrue(classify_failure(upstream).external)
        self.assertFalse(classify_failure(RuntimeError("401 unauthorized")).repairable)

    def test_provider_capacity_errors_are_external_upstream(self):
        for message in ("AGY returned HTTP 503", "service unavailable", "no capacity"):
            result = classify_failure(TransientAdapterError(message))
            self.assertEqual("external_upstream", result.name)
            self.assertTrue(result.external)
            self.assertFalse(result.repairable)

    def test_headroom_readiness_naming_the_backend_stays_internal(self):
        # Raised by the round-17 DSH review: naming AGY/Antigravity in a local
        # Headroom readiness message used to hit the capacity branch first and
        # come out external_upstream, which since v1.6.5 can trigger a standby
        # substitution an internal fault must never get.
        for message in ("agy headroom unavailable", "antigravity readiness unavailable: headroom",
                        "Antigravity headroom startup unavailable"):
            result = classify_failure(RuntimeError(message))
            self.assertEqual("internal_headroom", result.name, message)
            self.assertFalse(result.external, message)
        # Genuine AGY outages without Headroom wording keep their class.
        for message in ("agy unavailable", "Antigravity service unavailable", "AGY returned HTTP 503"):
            self.assertEqual("external_upstream", classify_failure(RuntimeError(message)).name, message)

    def test_a_bare_eof_is_an_external_transport_failure(self):
        # Raised by the round-17 DSH review: the token list only knew " eof" and
        # "eof:", so an error that is just "EOF" fell through to an internal
        # candidate instead of the transport class "unexpected eof" already has.
        for message in ("eof", "EOF", "EOF\n", "  EOF  "):
            result = classify_failure(RuntimeError(message))
            self.assertEqual("external_upstream", result.name, repr(message))
            self.assertTrue(result.external, repr(message))
        # A word that merely contains the letters is not a transport failure --
        # also after a space, which the old " eof" token matched (round 29).
        from xiaoba_agentos.adapters import _is_transient_cli_error
        for message in ("eofmarker missing", "adapter eof_marker missing", "adapter eofs", "geofence: bad"):
            self.assertNotEqual("external_upstream", classify_failure(RuntimeError(message)).name, message)
            self.assertFalse(_is_transient_cli_error(message), message)
        for message in ("read: EOF", "unexpected eof", 'Post "https://x.invalid/v1": EOF'):
            self.assertEqual("external_upstream", classify_failure(RuntimeError(message)).name, message)
            self.assertTrue(_is_transient_cli_error(message), message)

    def test_status_codes_need_context_and_both_layers_agree(self):
        # One shared rule for adapters and classify_failure (v1.6.5 pre-review):
        # a 5xx counts only with context; a bare number is not a status.
        from xiaoba_agentos.adapters import _is_transient_cli_error
        for message in ("HTTP 500", "AGY returned HTTP 503", "status: 502", "upstream 503 unavailable",
                        "503 Service Unavailable", "504 Gateway Timeout", "error 502 bad gateway"):
            with self.subTest(message=message):
                self.assertTrue(_is_transient_cli_error(message))
                self.assertEqual("external_upstream", classify_failure(RuntimeError(message)).name)
        for message in ("waited 503 seconds then gave up", "read 500 lines", "item 504 of 900"):
            with self.subTest(message=message):
                self.assertFalse(_is_transient_cli_error(message))
                self.assertNotEqual("external_upstream", classify_failure(RuntimeError(message)).name)

    def test_the_shared_status_pattern_does_not_depend_on_its_callers_lowercasing(self):
        # Round-38 review: the pattern itself only matched lowercase text.
        from xiaoba_agentos.adapters import HTTP_5XX
        for message in ("SERVER: 503", "503 Service Unavailable", "INVALID_REQUEST: 502 <!DOCTYPE html>",
                        "HTTP 500", "Upstream 504 Gateway Timeout"):
            with self.subTest(message=message):
                self.assertTrue(HTTP_5XX.search(message), message)
        for message in ("WAITED 503 SECONDS", "Read 500 Lines"):
            with self.subTest(message=message):
                self.assertFalse(HTTP_5XX.search(message), message)

    def test_transport_and_resolution_failures_are_external_upstream(self):
        messages = (
            # The real 2026-09-21 AGY failure: a bare Go transport error with no
            # HTTP status.  It froze a task for four days because it classified
            # as an internal release candidate.
            'error: Eligibility check failed: Post "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist": EOF',
            "connection reset by peer",
            "broken pipe",
            # Resolver wording differs per platform: Go on macOS says "no such
            # host", macOS libc says "nodename nor servname provided", glibc
            # says "temporary failure in name resolution".
            "dial tcp: lookup daily-cloudcode-pa.googleapis.com: no such host",
            "nodename nor servname provided, or not known",
            "temporary failure in name resolution",
            "dns server misbehaving",
        )
        for message in messages:
            result = classify_failure(RuntimeError(message))
            self.assertEqual("external_upstream", result.name, message)
            self.assertTrue(result.external, message)
            self.assertFalse(result.repairable, message)
            self.assertFalse(result.user_action_required, message)

    def test_exhausted_output_budget_is_external_and_not_repairable(self):
        error = _output_limit_failure(_OUTPUT_LIMIT_ERROR)
        self.assertIn("上限", str(error))
        result = classify_failure(error)
        self.assertEqual("external_upstream", result.name)
        self.assertTrue(result.external)
        self.assertFalse(result.repairable)

    def test_genuinely_unknown_failures_stay_internal_candidates(self):
        result = classify_failure(RuntimeError("something entirely novel went wrong"))
        self.assertEqual("internal_unknown_candidate", result.name)
        self.assertFalse(result.external)
        self.assertTrue(result.user_action_required)

    def test_bare_401_403_429_inside_other_text_are_not_statuses(self):
        # Round-40 DSH review: these were bare substrings, so a PID, a line
        # number or a task number came out external_auth/external_upstream --
        # both of which can trigger a standby substitution since v1.6.5.
        for message in ("worker pid 4012 crashed", "assertion failed at line 403",
                        "task 4290 stopped", "parsed 1403 rows then crashed"):
            result = classify_failure(RuntimeError(message))
            self.assertEqual("internal_unknown_candidate", result.name, message)
            self.assertFalse(result.external, message)
        # Real statuses keep their classes.
        for message in ("HTTP 401", "status: 403", "error 401", "SERVER: 403",
                        "401 Unauthorized", "403 Forbidden"):
            self.assertEqual("external_auth", classify_failure(RuntimeError(message)).name, message)
        for message in ("HTTP 429", "status code 429", "429 Too Many Requests"):
            self.assertEqual("external_upstream", classify_failure(RuntimeError(message)).name, message)

    def test_headroom_wording_wins_over_a_declared_external_class(self):
        # Round-40 DSH review: the Headroom check was documented as "first" but
        # ran after the declared/attribute external checks, so a Headroom
        # readiness fault flagged external_upstream by its raiser could still
        # trigger a standby substitution.
        flagged = TransientAdapterError("agy headroom unavailable")
        flagged.external_upstream = True
        declared = RuntimeError("headroom readiness failed")
        declared.failure_class = "external_upstream"
        for error in (flagged, declared):
            result = classify_failure(error)
            self.assertEqual("internal_headroom", result.name, str(error))
            self.assertFalse(result.external, str(error))

    def test_missing_provider_credentials_are_external_auth(self):
        result = classify_failure(RuntimeError("MISSING_CREDENTIAL: no API key for provider"))
        self.assertEqual("external_auth", result.name)
        self.assertTrue(result.external)
        self.assertTrue(result.user_action_required)

    def test_live_probe_records_health_without_user_task_or_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            store = EventStore(config.database_path); store.initialize()

            class Adapter:
                def __init__(self, name): self.name = name
                def run(self, _prompt, _context):
                    return AgentResult(self.name, HEALTH_MARKER, 4, provider="test", model="flash")

            with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda agent: Adapter(agent.name)):
                result = live_agent_probe(config, store)
            self.assertTrue(result["ok"])
            self.assertEqual(["a", "b"], result["healthy_agents"])
            self.assertEqual([], store.list_tasks())
            self.assertEqual([], store.review_queue("all"))
            self.assertEqual({"healthy"}, {row["status"] for row in store.agent_health()})
            self.assertFalse((config.runtime_dir / "memory-outbox").exists())

    def test_dual_probe_failure_blocks_queue_without_repair_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            store = EventStore(config.database_path); store.initialize()
            task_id = store.submit("must remain queued", 3)

            class Adapter:
                def run(self, _prompt, _context):
                    error = TransientAdapterError("upstream disconnected")
                    error.external_upstream = True
                    raise error

            with patch("xiaoba_agentos.recovery.build_adapter", return_value=Adapter()):
                result = start_session_health(config, store)
            self.assertEqual("degraded_blocked", result["session_mode"])
            self.assertEqual([], result["repair_jobs"])
            self.assertEqual("pending", store.get_task(task_id)["status"])
            self.assertEqual([], store.repair_jobs())

    def test_external_failure_never_queues_self_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            store = EventStore(config.database_path); store.initialize()

            class Adapter:
                def __init__(self, name): self.name = name
                def run(self, _prompt, _context):
                    if self.name == "b":
                        error = TransientAdapterError("429 rate limit")
                        error.external_upstream = True
                        raise error
                    return AgentResult(self.name, HEALTH_MARKER, 1)

            with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda a: Adapter(a.name)):
                result = start_session_health(config, store)
            self.assertEqual("degraded_blocked", result["session_mode"])
            self.assertEqual([], store.repair_jobs())
            self.assertEqual("external_blocked", {r["agent_name"]: r for r in store.agent_health()}["b"]["status"])

    def test_missing_health_marker_is_an_internal_candidate_fault(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            store = EventStore(config.database_path); store.initialize()

            class Adapter:
                def __init__(self, name): self.name = name
                def run(self, _prompt, _context):
                    return AgentResult(self.name, "ordinary answer", 1)

            with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda a: Adapter(a.name)):
                result = live_agent_probe(config, store)
            self.assertFalse(result["ok"])
            self.assertEqual({"internal_adapter_candidate"}, {
                item["failure_class"] for item in result["agents"].values()
            })

    def test_missing_marker_keeps_probe_evidence_and_attempts_for_every_driver(self):
        # Round-41 DSH review: for AGY/Qoder a reply without the marker lost the
        # probe state (it fell back to 'unknown') and the route attempts that
        # name the model actually used; only DSH kept them.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = [
                {"name": "dsh_review", "type": "mock", "driver": "dsh", "enabled": True, "role": "", "timeout_seconds": 1},
                {"name": "agy_review", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
            ]
            config = self.config(root, agents=agents)
            store = EventStore(config.database_path); store.initialize()

            class Adapter:
                def __init__(self, name): self.name = name
                def run(self, _prompt, _context):
                    result = AgentResult(self.name, "ordinary answer", 1)
                    result.route_attempts = [{"provider": "p", "model": f"{self.name}-model"}]
                    return result

            with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda a: Adapter(a.name)):
                result = live_agent_probe(config, store)
            for name in ("dsh_review", "agy_review"):
                with self.subTest(agent=name):
                    self.assertEqual("response_without_health_marker",
                                     result["agents"][name]["health_probe_state"])
                    health = {row["agent_name"]: row for row in store.agent_health()}[name]
                    self.assertIn("response_without_health_marker", json.dumps(health, ensure_ascii=False))
            with store.connect() as conn:
                attempts = [json.loads(row["payload_json"]) for row in conn.execute(
                    "SELECT payload_json FROM events WHERE event_type='health.provider_attempt'")]
            self.assertEqual({"dsh_review-model", "agy_review-model"},
                             {item.get("model") for item in attempts})

    def test_dsh_probe_distinguishes_process_unavailable_from_missing_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = [
                {"name": "dsh_review", "type": "mock", "driver": "dsh", "enabled": True, "role": "", "timeout_seconds": 1},
                {"name": "agy_review", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
            ]
            config = self.config(root, agents=agents)
            store = EventStore(config.database_path); store.initialize()

            class Adapter:
                def __init__(self, name): self.name = name
                def run(self, _prompt, _context):
                    if self.name == "dsh_review":
                        raise TransientAdapterError("dsh timeout: dsh_review")
                    return AgentResult(self.name, HEALTH_MARKER, 1)

            with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda agent: Adapter(agent.name)):
                result = live_agent_probe(config, store)
            dsh = result["agents"]["dsh_review"]
            self.assertEqual("dsh_process_unavailable", dsh["failure_class"])
            self.assertEqual("process_unavailable", dsh["health_probe_state"])

            class MarkerlessAdapter:
                def __init__(self, name): self.name = name
                def run(self, _prompt, _context):
                    if self.name == "dsh_review":
                        return AgentResult(self.name, "ordinary answer", 1)
                    return AgentResult(self.name, HEALTH_MARKER, 1)

            with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda agent: MarkerlessAdapter(agent.name)):
                result = live_agent_probe(config, store)
            dsh = result["agents"]["dsh_review"]
            self.assertEqual("dsh_health_marker_missing", dsh["failure_class"])
            self.assertEqual("response_without_health_marker", dsh["health_probe_state"])

    def test_dsh_process_failure_reports_action_needed_without_repair_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = [
                {"name": "dsh_review", "type": "mock", "driver": "dsh", "enabled": True, "role": "", "timeout_seconds": 1},
                {"name": "agy_review", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
            ]
            config = self.config(root, agents=agents)
            store = EventStore(config.database_path); store.initialize()

            class Adapter:
                def __init__(self, name): self.name = name
                def run(self, _prompt, _context):
                    if self.name == "dsh_review":
                        raise TransientAdapterError("dsh timeout: dsh_review")
                    return AgentResult(self.name, HEALTH_MARKER, 1)

            with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda agent: Adapter(agent.name)):
                result = start_session_health(config, store)
            self.assertEqual("degraded_blocked", result["session_mode"])
            self.assertEqual("retired", result["peer_auto_repair"])
            health = {row["agent_name"]: row for row in store.agent_health()}["dsh_review"]
            self.assertEqual("dsh_process_unavailable", health["failure_class"])
            self.assertIn("DSH 进程或端点未启动", health["error"])
            self.assertEqual([], store.repair_jobs())
            self.assertEqual([], store.pending_notifications())

    def test_diagnostics_redact_credentials(self):
        text = sanitize_diagnostic_text(
            "Authorization: secret-value api_key=sk-abcdefghijklmnop Bearer token-value"
        )
        self.assertNotIn("secret-value", text)
        self.assertNotIn("sk-abcdefghijklmnop", text)
        self.assertNotIn("token-value", text)

    def test_internal_probe_failure_stays_degraded_without_repair_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            store = EventStore(config.database_path); store.initialize()

            class Adapter:
                def __init__(self, name): self.name = name
                def run(self, _prompt, _context):
                    if self.name == "b":
                        error = TransientAdapterError("Headroom unavailable")
                        error.failure_class = "internal_headroom"
                        error.repairable = True
                        raise error
                    return AgentResult(self.name, HEALTH_MARKER, 1)

            with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda agent: Adapter(agent.name)):
                result = start_session_health(config, store)
            self.assertFalse(result["ok"])
            self.assertEqual("degraded_blocked", result["session_mode"])
            self.assertEqual([], result["repair_jobs"])
            self.assertEqual("retired", result["peer_auto_repair"])
            health = {r["agent_name"]: r for r in store.agent_health()}["b"]
            self.assertEqual("degraded", health["status"])
            self.assertEqual("internal_headroom", health["failure_class"])
            self.assertEqual([], store.repair_jobs())
            self.assertEqual([], store.pending_notifications())

    def test_recovered_probe_does_not_unfreeze_until_user_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            store = EventStore(config.database_path); store.initialize()
            task_id = store.submit("work", 3, snapshot={"evidence": "same"})
            store.lease_next(60)
            run_a = store.start_agent_run(task_id, "a")
            store.finish_agent_run(run_a, "completed", "kept output", None, 1)
            frozen = store.freeze_task(
                task_id,
                failure_class="internal_headroom",
                failures=[{
                    "agent": "b", "error": "Headroom unavailable",
                    "failure_class": "internal_headroom", "repairable": True, "external": False,
                }],
            )

            class Adapter:
                def __init__(self, name): self.name = name
                def run(self, _prompt, _context):
                    return AgentResult(self.name, HEALTH_MARKER, 1)

            with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda agent: Adapter(agent.name)):
                reprobe = start_session_health(config, store)
            self.assertTrue(reprobe["ok"])
            self.assertEqual("frozen_paused", store.get_task(task_id)["status"])
            self.assertEqual(frozen["freeze_hash"], store.get_task(task_id)["freeze_hash"])
            self.assertEqual([], store.repair_jobs())
            self.assertEqual([], store.pending_notifications())
            self.assertIsNone(store.lease_next(60))

            resumed = store.resume_task(
                task_id, frozen["freeze_hash"],
                enabled_names=["a", "b"], health_max_age_seconds=86400,
            )
            self.assertEqual("retry", resumed["status"])
            self.assertEqual("frozen_paused", resumed["resumed_from"])
            self.assertEqual(1, resumed["resume_generation"])
            with self.assertRaises(ValueError):
                store.resume_task(
                    task_id, frozen["freeze_hash"],
                    enabled_names=["a", "b"], health_max_age_seconds=86400,
                )
            self.assertEqual("kept output", store.latest_agent_runs(task_id)["a"]["output"])


if __name__ == "__main__":
    unittest.main()
