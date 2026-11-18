"""Public intake and direct lookup preserve source bindings without adoption."""
import json
import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_experience_loop as loop
from fixtures_review import _admit_authored_candidate, _run_cli
from xiaoba_agentos.experience_workflow import match_trials
from xiaoba_agentos.pending_matching import explain


class RecallTests(unittest.TestCase):
    setUp = loop.ExperienceLoopTests.setUp
    file = loop.ExperienceLoopTests.file
    count = loop.ExperienceLoopTests.count
    event_count = loop.ExperienceLoopTests.event_count

    def authored(self, title, key=""):
        c = self.store.create_authored_candidate(title, "TEST reusable recipe", self.cfg.runtime_dir / "memory-outbox",
            author="TEST-author", experience_kind="method", problem_key=key)
        c = _admit_authored_candidate(self.store, c)
        self.store.review_memory(c["task_id"], "delivery", "approve", "TEST human", c["evidence_sha256"])
        return c

    def test_keyword_evaluation_set_and_key_precedence(self):
        self.authored("室内建筑调色保留材质", "interior-colour")
        self.authored("海报透明抠图与alpha核验")
        self.authored("AE视频字幕位置", "ae-caption")
        cases = [("请为室内建筑做色彩调整", "室内建筑调色保留材质"),
                 ("海报需要移除背景并核验alpha", "海报透明抠图与alpha核验"),
                 ("AE视频添加字幕条", "AE视频字幕位置"), ("晚餐预约意大利餐厅", None)]
        for query, expected in cases:
            with self.subTest(query=query):
                rows = match_trials(self.store, query=query)
                self.assertEqual([expected] if expected else [], [row["title"] for row in rows])
        rows = match_trials(self.store, problem_key="ae-caption", query="海报抠图")
        self.assertEqual("AE视频字幕位置", rows[0]["title"])
        self.assertEqual("problem_key", rows[0]["match_basis"])
        self.assertTrue(rows[0]["missing_conditions"])

    def test_real_subprocess_direct_intake_has_complete_suggestions_no_adoption(self):
        c = self.authored("室内建筑调色保留材质")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPATH", None)
        src = str(Path(__file__).resolve().parents[1] / "src")
        code = "import sys;sys.path.insert(0,sys.argv.pop(1));from xiaoba_agentos.cli import main;sys.exit(main())"
        cmd = [sys.executable, "-B", "-c", code, src, "--config", str(self.cfg.path), "intake",
               "请为室内建筑做色彩调整", "--message-id", "TEST-direct"]
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        value = json.loads(result.stdout)
        row = value["trial_suggestions"][0]
        self.assertEqual(c["task_id"], row["task_id"])
        self.assertEqual(c["task_id"], row["candidate_id"])
        self.assertEqual(c["evidence_sha256"], row["candidate_sha256"])
        self.assertTrue(row["delivery_review_id"])
        self.assertTrue({"problem_key", "task_type", "applicability_conditions", "exclusions", "environment",
                         "match_reason", "missing_conditions"}.issubset(row))
        replay = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(0, replay.returncode, replay.stderr + replay.stdout)
        self.assertEqual(value["route_id"], json.loads(replay.stdout)["route_id"])
        self.assertEqual(0, self.event_count("memory.application_recorded"))
        self.assertEqual(0, self.event_count("memory.formal_retrieved"))
        self.assertEqual(0, self.count("memory_reuse"))

    def _subprocess_intake(self, prompt, message_id, *extra):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPATH", None)
        src = str(Path(__file__).resolve().parents[1] / "src")
        code = "import sys;sys.path.insert(0,sys.argv.pop(1));from xiaoba_agentos.cli import main;sys.exit(main())"
        return subprocess.run(
            [sys.executable, "-B", "-c", code, src, "--config", str(self.cfg.path),
             "intake", prompt, "--message-id", message_id, *extra],
            capture_output=True, text=True, env=env, timeout=20,
        )

    def test_public_intake_exact_problem_key_precedes_unrelated_keywords(self):
        c = self.authored("海报透明抠图与alpha核验", "poster-alpha")
        self.authored("AE视频字幕位置", "ae-caption")
        result = self._subprocess_intake(
            "晚餐预约意大利餐厅", "TEST-key", "--problem-key", "poster-alpha",
        )
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual("direct", payload["route"])
        rows = payload["trial_suggestions"]
        self.assertEqual([c["task_id"]], [row["candidate_id"] for row in rows])
        self.assertEqual("problem_key", rows[0]["match_basis"])
        self.assertFalse(rows[0]["normal_retrieval"])
        self.assertEqual(0, self.count("memory_reuse"))
        self.assertEqual(0, self.event_count("memory.application_recorded"))
        self.assertEqual(0, self.event_count("memory.formal_retrieved"))

    def test_public_intake_unrelated_description_has_no_false_suggestion(self):
        self.authored("海报透明抠图与alpha核验")
        result = self._subprocess_intake(
            "晚餐预约意大利餐厅", "TEST-unrelated",
        )
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual([], payload["trial_suggestions"])
        self.assertEqual("ok", payload["trial_suggestion_status"])
        self.assertEqual(0, self.count("memory_reuse"))

    def test_public_intake_exposes_quarantined_formal_without_ordinary_reuse(self):
        c = self.authored("室内建筑调色保留材质", "interior-colour")
        note = self.project / "quarantined-formal.md"
        note.write_text("TEST isolated formal note", encoding="utf-8")
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE memory_candidates SET status='revalidation_required',published_path=? WHERE task_id=?",
                (str(note), c["task_id"]),
            )
        result = self._subprocess_intake(
            "无关表达", "TEST-quarantined", "--problem-key", "interior-colour",
        )
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        row = json.loads(result.stdout)["trial_suggestions"][0]
        self.assertEqual("formal_revalidation_required", row["classification"])
        self.assertEqual(c["task_id"], row["candidate_id"])
        self.assertFalse(row["normal_retrieval"])
        self.assertTrue(row["retrieval_blocker"])
        self.assertIn("missing_conditions", row)
        self.assertEqual(0, self.count("memory_reuse"))

    def test_public_intake_fails_closed_on_broken_event_chain(self):
        self.authored("室内建筑调色保留材质", "interior-colour")
        with self.store.connect() as conn:
            conn.execute("UPDATE events SET payload_json='{}' WHERE sequence=(SELECT MAX(sequence) FROM events)")
        self.assertFalse(self.store.verify_event_chain()["ok"])
        result = self._subprocess_intake(
            "室内建筑色彩调整", "TEST-broken-chain", "--problem-key", "interior-colour",
        )
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual([], payload["trial_suggestions"])
        self.assertEqual("unavailable", payload["trial_suggestion_status"])
        self.assertEqual(0, self.count("memory_reuse"))

    def test_real_unix_bridge_intake_returns_suggestions_without_adoption(self):
        c = self.authored("海报透明抠图与alpha核验", "poster-alpha")
        payload = self._bridge_request({
            "command": "intake", "prompt": "晚餐预约意大利餐厅",
            "problem_key": "poster-alpha", "message_id": "TEST-bridge-key",
        })
        suggestion = payload["result"]["trial_suggestions"][0]
        self.assertEqual(c["task_id"], suggestion["candidate_id"])
        self.assertEqual("problem_key", suggestion["match_basis"])
        self.assertEqual("direct", payload["result"]["route"])
        self.assertEqual(0, self.event_count("memory.application_recorded"))
        self.assertEqual(0, self.event_count("memory.formal_retrieved"))
        self.assertEqual(0, self.count("memory_reuse"))

    def _bridge_request(self, request):
        import tempfile
        sock_tmp = tempfile.TemporaryDirectory(prefix="xbs-", dir="/tmp" if Path("/tmp").is_dir() else None)
        self.addCleanup(sock_tmp.cleanup)
        socket_path = Path(sock_tmp.name) / "b.sock"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPATH", None)
        src = str(Path(__file__).resolve().parents[1] / "src")
        code = "import sys;sys.path.insert(0,sys.argv.pop(1));from xiaoba_agentos.cli import main;sys.exit(main())"
        proc = subprocess.Popen(
            [sys.executable, "-B", "-c", code, src, "--config", str(self.cfg.path),
             "bridge", "--socket", str(socket_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 5
        while not socket_path.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(socket_path.exists(), "bridge socket did not become ready")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(socket_path))
            client.sendall(json.dumps(request).encode("utf-8"))
            client.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        stdout, stderr = proc.communicate(timeout=5)
        self.assertEqual(0, proc.returncode, stderr + stdout)
        payload = json.loads(b"".join(chunks).decode("utf-8"))
        self.assertTrue(payload["ok"], payload)
        return payload

    def test_real_bridge_submit_and_intake_submit_preserve_key_and_frozen_suggestions(self):
        c = self.authored("海报透明抠图与alpha核验", "poster-alpha")
        config = json.loads(self.cfg.path.read_text(encoding="utf-8"))
        skills_root = Path(__file__).resolve().parent / "fixtures" / "content" / "skills"
        config["skills"] = {"root_dir": str(skills_root), "index_path": str(skills_root / "index.json"), "max_selected": 4}
        config["release"] = {"expected_version": "1.6.15"}
        self.cfg.path.write_text(json.dumps(config), encoding="utf-8")
        for command in ("submit", "intake"):
            with self.subTest(command=command):
                value = self._bridge_request({
                    "command": command, "prompt": "晚餐预约意大利餐厅",
                    "problem_key": "poster-alpha", "submit": True,
                    "force_route": "agentos_multi", "queue_when_blocked": True,
                    "message_id": "TEST-bridge-" + command,
                })["result"]
                snapshot = json.loads(self.store.get_task(value["task_id"])["execution_snapshot_json"])
                self.assertEqual("poster-alpha", snapshot["experience_policy"]["problem_key"])
                self.assertEqual(snapshot["trial_suggestions"], value["trial_suggestions"])
                self.assertEqual(snapshot["trial_suggestion_status"], value["trial_suggestion_status"])
                self.assertEqual(c["task_id"], value["trial_suggestions"][0]["candidate_id"])
                self.assertEqual("ok", value["trial_suggestion_status"])
                self.assertIn("missing_conditions", value["trial_suggestions"][0])
        self.assertEqual(0, self.event_count("memory.application_recorded"))
        self.assertEqual(0, self.event_count("memory.formal_retrieved"))
        self.assertEqual(0, self.count("memory_reuse"))

    def _configure_submit(self):
        config = json.loads(self.cfg.path.read_text(encoding="utf-8"))
        skills_root = Path(__file__).resolve().parent / "fixtures" / "content" / "skills"
        config["skills"] = {"root_dir": str(skills_root), "index_path": str(skills_root / "index.json"), "max_selected": 4}
        config["release"] = {"expected_version": "1.6.15"}
        self.cfg.path.write_text(json.dumps(config), encoding="utf-8")

    def test_real_cli_submit_paths_return_frozen_suggestion_status(self):
        c = self.authored("海报透明抠图与alpha核验", "poster-alpha")
        self._configure_submit()
        src = str(Path(__file__).resolve().parents[1] / "src")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPATH", None)
        code = "import sys;sys.path.insert(0,sys.argv.pop(1));from xiaoba_agentos.cli import main;sys.exit(main())"
        for command, flags in (("submit", []), ("intake", ["--submit", "--force-route", "agentos_multi", "--queue-when-blocked"])):
            with self.subTest(command=command):
                p = subprocess.run([sys.executable, "-B", "-c", code, src, "--config", str(self.cfg.path),
                    command, "晚餐预约意大利餐厅", "--problem-key", "poster-alpha", *flags],
                    capture_output=True, text=True, env=env, timeout=20)
                self.assertEqual(0, p.returncode, p.stderr + p.stdout)
                value = json.loads(p.stdout)
                snapshot = json.loads(self.store.get_task(value["task_id"])["execution_snapshot_json"])
                self.assertEqual(c["task_id"], value["trial_suggestions"][0]["candidate_id"])
                for key in ("trial_suggestions", "trial_suggestion_status"):
                    self.assertEqual(snapshot[key], value[key])
        self.assertEqual(0, self.event_count("memory.application_recorded"))
        self.assertEqual(0, self.count("memory_reuse"))

    def test_cli_intake_returns_unavailable_from_submission_snapshot(self):
        self._configure_submit()
        available = {"trial_suggestions": [], "trial_suggestion_status": "ok"}
        unavailable = {"trial_suggestions": [], "trial_suggestion_status": "unavailable",
                       "trial_suggestion_error": "TEST unavailable during submit"}
        with patch("xiaoba_agentos.experience_workflow.suggest_trials", side_effect=[available, unavailable]):
            result, value = _run_cli(["--config", str(self.cfg.path), "intake", "TEST submit",
                "--submit", "--force-route", "agentos_multi", "--queue-when-blocked"])
        self.assertEqual(0, result)
        for key, expected in unavailable.items():
            self.assertEqual(expected, value[key])
        result, value = _run_cli(["--config", str(self.cfg.path), "submit", "TEST audit", "--memory-kind", "audit"])
        self.assertEqual(0, result)
        self.assertEqual([], value["trial_suggestions"])
        self.assertEqual("ok", value["trial_suggestion_status"])

    def test_real_subprocess_submit_does_not_count_retrieved_formal_memory_as_reuse(self):
        c = self.authored("室内建筑调色保留材质")
        vault = Path(self.cfg.memory["obsidian_vault"])
        note = vault / "AgentOS" / "formal.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(
            f'---\nstatus: "active"\nsource_task_id: "{c["task_id"]}"\nauthority: "false"\n---\n\nTEST formal experience',
            encoding="utf-8",
        )
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE memory_candidates SET status='published',published_path=?,published_at=1 WHERE task_id=?",
                (str(note), c["task_id"]),
            )

        project = Path(self.cfg.project_dir)
        scripts = project / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        bridge = Path(__file__).resolve().parents[1] / "scripts" / "knowlp_query.py"
        (scripts / "knowlp_query.py").write_bytes(bridge.read_bytes())
        module = project / "knowlp_test.py"
        module.write_text(
            "def knowlp_search(query, limit=6, engines=None):\n"
            "    return {'hits': [{'path': 'AgentOS/formal.md', 'title': 'TEST formal experience'}]}\n",
            encoding="utf-8",
        )
        config = json.loads(self.cfg.path.read_text(encoding="utf-8"))
        config["memory"].update(
            knowlp_enabled=True,
            knowlp_python=sys.executable,
            knowlp_module=str(module),
            knowlp_vault=str(vault),
        )
        skills_root = Path(__file__).resolve().parent / "fixtures" / "content" / "skills"
        config["skills"] = {
            "root_dir": str(skills_root),
            "index_path": str(skills_root / "index.json"),
            "max_selected": 4,
        }
        config["release"] = {"expected_version": "1.6.15"}
        self.cfg.path.write_text(json.dumps(config), encoding="utf-8")

        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPATH", None)
        src = str(Path(__file__).resolve().parents[1] / "src")
        code = "import sys;sys.path.insert(0,sys.argv.pop(1));from xiaoba_agentos.cli import main;sys.exit(main())"
        diagnostic = subprocess.run(
            [sys.executable, "-B", "-c", code, src, "--config", str(self.cfg.path),
             "memory-retrieve", "请为室内建筑做色彩调整"],
            capture_output=True, text=True, env=env, timeout=20,
        )
        self.assertEqual(0, diagnostic.returncode, diagnostic.stderr + diagnostic.stdout)
        self.assertIn(c["task_id"], diagnostic.stdout)
        self.assertEqual(0, self.event_count("memory.formal_retrieved"))
        result = subprocess.run(
            [sys.executable, "-B", "-c", code, src, "--config", str(self.cfg.path), "submit",
             "请为室内建筑做色彩调整"],
            capture_output=True, text=True, env=env, timeout=20,
        )
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual("pending", payload["status"])
        task = self.store.get_task(payload["task_id"])
        snapshot = json.loads(task["execution_snapshot_json"])
        self.assertEqual(c["task_id"], snapshot["memory_hits"][0]["source_task_id"])
        self.assertEqual(1, self.event_count("memory.formal_retrieved"))
        self.assertEqual(0, self.count("memory_reuse"))

    def test_successful_workflow_submission_skips_retrieval(self):
        from xiaoba_agentos.cli import _submit_task
        self.cfg.raw["release"] = {"expected_version": "1.6.15"}
        plan = {"version": 1, "phase": "work", "agents": ["a", "b"],
                "assignments": {"a": "TEST implement", "b": "TEST verify"}}
        with patch("xiaoba_agentos.cli.MemoryBridge") as memory:
            task_id = _submit_task(self.cfg, self.store, "TEST workflow", workflow=plan,
                                   task_context="TEST frozen context")
        memory.assert_not_called()
        snapshot = json.loads(self.store.get_task(task_id)["execution_snapshot_json"])
        self.assertEqual(plan, snapshot["workflow"])
        self.assertEqual([], snapshot["memory_hits"])
        self.assertEqual(0, self.event_count("memory.formal_retrieved"))

    def test_advice_failure_does_not_block_intake(self):
        with patch("xiaoba_agentos.experience_workflow.match_trials", side_effect=ValueError("TEST bad source")):
            code, out = _run_cli(["--config", str(self.cfg.path), "intake", "普通任务"])
        self.assertEqual(0, code)
        self.assertEqual("unavailable", out["trial_suggestion_status"])
        self.assertEqual([], out["trial_suggestions"])

    def test_query_reading_does_not_record_use_and_is_bounded(self):
        for n in range(8):
            self.authored("室内建筑调色 " + str(n))
        before = self.count("events")
        rows = match_trials(self.store, query="室内建筑调色")
        self.assertEqual(5, len(rows))
        self.assertEqual(before, self.count("events"))
        self.assertFalse(any(row["normal_retrieval"] for row in rows))

    def test_quarantined_formal_is_discoverable_but_not_reused(self):
        c = self.authored("室内建筑调色保留材质")
        note = self.project / "published.md"
        note.write_text("TEST quarantined formal note")
        with self.store.connect() as conn:
            conn.execute("UPDATE memory_candidates SET status='revalidation_required',published_path=? WHERE task_id=?", (str(note), c["task_id"]))
        rows = match_trials(self.store, query="室内建筑调色")
        self.assertEqual(1, len(rows))
        self.assertEqual("formal_revalidation_required", rows[0]["classification"])
        self.assertTrue(rows[0]["retrieval_blocker"])
        self.assertIn("missing_conditions", rows[0])
        self.assertFalse(rows[0]["normal_retrieval"])

    def test_bridge_delegates_to_canonical_submit(self):
        from xiaoba_agentos.bridge import _submit
        with patch("xiaoba_agentos.cli._submit_task", return_value="TEST canonical") as submit:
            result = _submit("TEST prompt", self.store, self.cfg, session_id="TEST session", route_decision_id="TEST route")
        self.assertEqual("TEST canonical", result)
        submit.assert_called_once()
