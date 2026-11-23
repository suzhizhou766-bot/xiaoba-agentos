"""STEP4/5 orchestrator: real helper subprocesses, fourteen-item gates, read-only plan/preflight.
FakeRunner may inject only for isolation; normal branch executes real commands and parses outputs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from bind import PIN, Bind, clean_env, sha
from cleanup import cleanup_scene
from gates import initialize_attempt_state, load_acceptance_state, run_prebuild_gates
from schema75c import check_ri_pending_references
from workday_isolation import real_views, unchanged

HELPER_STEPS = [
    ("build_verified.py", "20-formal-build.stdout.txt", "stage"),
    ("prepare_scene.py", "21-prepare-scene.stdout.txt", "helpers"),
    ("isolate_scene.py", "22-isolate-scene.stdout.txt", "helpers"),
    ("prime_scene_clock.py", "23-prime-clock.stdout.txt", "helpers"),
    ("scene_prework_v169.py", "40-prework-workbuddy.stdout.txt", "helpers"),
    ("run_rehearsal.py", "30-run-rehearsal.stdout.txt", "helpers"),
    ("specials_v169.py", "41-specials.stdout.txt", "helpers"),
    ("specials_scores_v169.py", "42-card52-52b.stdout.txt", "helpers"),
    ("rehearsal_specials.py", "44-task-specials.stdout.txt", "helpers"),
    ("specials_v1614.py", "48-v1614-new-cases.stdout.txt", "helpers"),
    ("workday_case12.py", "49-workday-case12.stdout.txt", "helpers"),
    ("case13_reasoning.py", "50-reasoning-case13.stdout.txt", "stage"),
    ("case14_chain.py", "51-chain-case14.stdout.txt", "stage"),
]

FOURTEEN_KEYS = (
    "card43",
    "card44",
    "card45",
    "card46",
    "card47",
    "card51",
    "card52",
    "card52b",
    "card57",
    "void",
    "ui_install",
    "workday",
    "reasoning_effort",
    "event_chain",
)


def check_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("ok") is not True:
        raise RuntimeError(("json not ok", str(path), data))
    return data


class Orchestrator:
    def __init__(
        self,
        bind: Bind,
        *,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        skip_live: bool = False,
        allow_real: bool = False,
    ):
        self.bind = bind
        self.runner = runner or subprocess.run
        self.skip_live = skip_live
        self.allow_real = allow_real
        self.summary: dict[str, Any] = {
            "ok": False,
            "production_switch": False,
            "steps": [],
            "started_at": time.time(),
            "stage": "stage75q",
        }
        self._calls: list[dict[str, Any]] = []

    def dump(self) -> None:
        self.bind.O.mkdir(parents=True, exist_ok=True)
        (self.bind.O / "STEP4-5-result.json").write_text(
            json.dumps(self.summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def run_cmd(self, label: str, args: list[str], out_name: str, *, cwd: Path | None = None) -> str:
        print("START " + label, flush=True)
        out_path = self.bind.O / out_name
        env = clean_env()
        env["STAGE75Q_BIND_JSON"] = os.environ["STAGE75Q_BIND_JSON"]
        self._calls.append({"label": label, "argv": list(args), "out": out_name})
        with out_path.open("x", encoding="utf-8") as fh:
            proc = self.runner(args, cwd=str(cwd or self.bind.root), env=env, stdout=fh, stderr=subprocess.STDOUT)
        text = out_path.read_text(encoding="utf-8")
        print(text, flush=True)
        self.summary["steps"].append({"step": label, "exit": proc.returncode, "output": str(out_path)})
        self.dump()
        if proc.returncode:
            raise RuntimeError(label + " failed; STOP without retry. Full original output kept.")
        return text

    def helper(self, name: str, out_name: str, where: str) -> str:
        # helpers → record.step4_helpers_dir/helpers ; stage scripts → record.step4_helpers_dir
        if where == "helpers":
            script = self.bind.W / name
        else:
            script = self.bind.stage_dir / name
        return self.run_cmd(name, [str(PIN), "-B", str(script)], out_name)

    def assert_fourteen(self, items: dict[str, Any]) -> None:
        if set(items) != set(FOURTEEN_KEYS):
            raise RuntimeError("STOP: fourteen items key set mismatch: " + str(sorted(items)))
        if len(items) != 14 or not all(items.values()):
            raise RuntimeError("STOP: fourteen items incomplete or failed: " + str(items))

    def run(self) -> dict[str, Any]:
        if not self.allow_real and self.runner is subprocess.run:
            raise RuntimeError(
                "STOP: real stage execution requires explicit --allow-real after manager acceptance; "
                "use isolation FakeRunner for verification"
            )
        bind = self.bind
        bind.require_ready()
        real_before = real_views()
        gates = run_prebuild_gates(bind, skip_live=self.skip_live)
        self.summary["prebuild_gates"] = gates
        # Live may lack 75C extension table — not a failure.
        if not self.skip_live:
            self.summary["live_75c"] = check_ri_pending_references(
                bind.live / ".runtime/agentos.sqlite3",
                require_present=False,
                phase="pre_switch_live",
            )
        original = load_acceptance_state(bind)
        initialize_attempt_state(bind, original)
        (bind.O / "00-online-before-step4.sha256").write_text(
            sha(bind.live / "SOURCE_MANIFEST.json") + "\n", encoding="utf-8"
        )
        try:
            self.helper("build_verified.py", "20-formal-build.stdout.txt", "stage")
            rel = json.loads((bind.PKG / "RELEASE.json").read_text(encoding="utf-8"))
            evidence = rel["acceptance"]["end_to_end_evidence"]
            if not set(("conclusions", "pool_nodes", "execution")) <= set(evidence):
                raise RuntimeError("STOP: end_to_end_evidence incomplete")
            if "_source" in evidence:
                raise RuntimeError("STOP: end_to_end_evidence has _source")

            self.helper("prepare_scene.py", "21-prepare-scene.stdout.txt", "helpers")
            scene = json.loads((bind.O / "SCENE.json").read_text(encoding="utf-8"))
            scene_db = Path(scene["runtime"]) / ".runtime/agentos.sqlite3"
            # Readonly snapshot of copied scene ONLY — never pre-migrate before new-package init.
            self.summary["scene_75c_before"] = check_ri_pending_references(
                scene_db, require_present=False, phase="pre_switch_copy_readonly"
            )
            self.summary["scene_75c_init_timing"] = {
                "pre_migrate_forbidden": True,
                "migrate_owner": "new_package_init",
                "before_phase": "pre_switch_copy_readonly",
            }

            self.helper("isolate_scene.py", "22-isolate-scene.stdout.txt", "helpers")
            self.helper("prime_scene_clock.py", "23-prime-clock.stdout.txt", "helpers")
            self.helper("scene_prework_v169.py", "40-prework-workbuddy.stdout.txt", "helpers")
            self.helper("run_rehearsal.py", "30-run-rehearsal.stdout.txt", "helpers")
            rep = check_json(bind.O / "30-复制现场演练.json")
            if len(rep["cases"]) != 11 or rep["success"]["result"]["read_back"]["restart_verified"] is not True:
                raise RuntimeError("STOP: rehearsal cases/restart_verified failed")
            # After real new-package init/switch: readonly six-column gate.
            self.summary["scene_75c_after"] = check_ri_pending_references(
                scene_db, require_present=True, phase="post_new_package_init"
            )
            after = self.summary["scene_75c_after"]
            if after.get("needs_four_to_six_migration"):
                raise RuntimeError(
                    "STOP: new package init did not migrate ri_pending_references four→six"
                )
            if not after.get("six_columns"):
                raise RuntimeError("STOP: post-init 75C six columns missing: " + str(after))
            self.summary["scene_75c"] = after
            self.summary["scene_75c_init_timing"]["after_phase"] = "post_new_package_init"
            self.summary["scene_75c_init_timing"]["before_columns"] = self.summary[
                "scene_75c_before"
            ].get("columns")
            self.summary["scene_75c_init_timing"]["after_columns"] = after.get("columns")

            score = check_json(bind.O / "34-card51-worker-scores.json")
            plain = check_json(bind.O / "35-card57-default-task.json")
            self.helper("specials_v169.py", "41-specials.stdout.txt", "helpers")
            old = check_json(bind.O / "41-v169-new-behaviors.json")
            if len(old["sections"]) != 6 or not all(v.get("ok") for v in old["sections"].values()):
                raise RuntimeError("STOP: specials_v169 sections failed")
            self.helper("specials_scores_v169.py", "42-card52-52b.stdout.txt", "helpers")
            new = check_json(bind.O / "42-card52-52b.json")
            if len(new["sections"]) != 3 or not all(v.get("ok") for v in new["sections"].values()):
                raise RuntimeError("STOP: card52/52b failed")
            self.helper("rehearsal_specials.py", "44-task-specials.stdout.txt", "helpers")
            check_json(bind.O / "33-task-abandon-cancel.json")

            eight = {
                "card43": True,
                "card44": True,
                "card45": True,
                "card46": True,
                "card47": True,
                "card51": score["ok"],
                "card52": True,
                "card52b": True,
            }
            ninth = {
                "default_task": plain["ok"],
                "host_pool_missing_score_refused": old["sections"]["card57_host_pool_score_required"]["ok"],
            }
            if not all(ninth.values()):
                raise RuntimeError("STOP: card57 failed")
            nine = dict(eight, card57=True)
            self.helper("specials_v1614.py", "48-v1614-new-cases.stdout.txt", "helpers")
            additions = check_json(bind.O / "48-v1614-new-cases.json")
            if set(additions["sections"]) != {"10_void", "11_ui_install"}:
                raise RuntimeError("STOP: void/ui sections missing")
            if not all(v["ok"] for v in additions["sections"].values()):
                raise RuntimeError("STOP: void/ui failed")
            eleven = dict(
                nine,
                void=additions["sections"]["10_void"]["ok"],
                ui_install=additions["sections"]["11_ui_install"]["ok"],
            )
            self.summary["cleanup_before_case12"] = cleanup_scene(bind)
            if not self.summary["cleanup_before_case12"]["ok"]:
                raise RuntimeError("STOP: cleanup before case12 failed")
            self.dump()
            self.helper("workday_case12.py", "49-workday-case12.stdout.txt", "helpers")
            ledger_case = check_json(bind.O / "49-workday-case12.json")
            twelve = dict(eleven, workday=ledger_case["ok"])
            self.helper("case13_reasoning.py", "50-reasoning-case13.stdout.txt", "stage")
            reasoning = check_json(bind.O / "50-reasoning-case13.json")
            if not (
                reasoning["fake_cli_only"]
                and reasoning["unset_matches_previous_release_argv"]
                and reasoning["test_after"] == "invalidated"
            ):
                raise RuntimeError("STOP: case13 assertions failed")
            thirteen = dict(twelve, reasoning_effort=reasoning["ok"])
            self.helper("case14_chain.py", "51-chain-case14.stdout.txt", "stage")
            chain = check_json(bind.O / "51-chain-case14.json")
            if set(chain["cases"]) != {"V1", "V2", "V8"} or not chain["ok"]:
                raise RuntimeError("STOP: case14 V1/V2/V8 failed")
            fourteen = dict(thirteen, event_chain=chain["ok"])
            self.assert_fourteen(fourteen)
            self.summary["fourteen_items"] = fourteen
            self.summary["cleanup_before_live_checks"] = cleanup_scene(bind)
            if not self.summary["cleanup_before_live_checks"]["ok"]:
                raise RuntimeError("STOP: cleanup before live checks failed")
            self.summary["package"] = {
                "path": str(bind.PKG),
                "files_sha256": sha(bind.PKG / "FILES.sha256"),
                "status": rel["status"],
                "release_tier": rel["release_tier"],
                "end_to_end_keys": sorted(evidence),
            }
            self.summary["step4_passed"] = True
            self.dump()

            for mode, num in (("plan", "45"), ("preflight", "46")):
                text = self.run_cmd(
                    "online read-only " + mode,
                    [str(PIN), "-B", str(bind.PKG / "agentos/scripts/switch_release.py"), mode, "--package", str(bind.PKG)],
                    num + "-online-" + mode + ".json",
                )
                value = json.loads(text)
                if value.get("ok") is not True or value.get("writes_performed") != 0:
                    raise RuntimeError(("STOP: online mode writes or failed", mode, value))

            from cli_retest_preflight import require_testable_cli

            self.summary["cli_retest_preflight"] = require_testable_cli(bind.live)
            self.summary["step5_passed"] = True
            self.summary["ok"] = True
        finally:
            try:
                self.summary["cleanup"] = cleanup_scene(bind)
            except Exception as exc:  # noqa: BLE001 — must always record cleanup
                self.summary["cleanup"] = {"ok": False, "error": str(exc)}
            self.summary["real_workspace_guard"] = unchanged(real_before)
            (bind.O / "49-real-workspace-guard.json").write_text(
                json.dumps(self.summary["real_workspace_guard"], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.summary["online_manifest_after"] = sha(bind.live / "SOURCE_MANIFEST.json")
            self.summary["finished_at"] = time.time()
            self.summary["subprocess_calls"] = self._calls
            expected = bind.online_manifest_sha256
            self.summary["ok"] = bool(
                self.summary.get("ok")
                and self.summary["cleanup"].get("ok")
                and (expected is None or self.summary["online_manifest_after"] == expected)
                and self.summary["real_workspace_guard"].get("ok")
            )
            self.dump()
            # Independent tool hash gate + production manifest before/after
            tool_hash_path = bind.O / "stage75q-tool-hashes.json"
            tool_hashes = {}
            stage_root = bind.stage_dir
            for rel in sorted(
                p.relative_to(stage_root).as_posix() for p in stage_root.rglob("*.py") if p.is_file()
            ):
                tool_hashes[rel] = sha(stage_root / rel)
            tool_hash_path.write_text(json.dumps(tool_hashes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self.summary["stage75q_tool_hashes"] = str(tool_hash_path)
            self.summary["online_manifest_before"] = (bind.O / "00-online-before-step4.sha256").read_text().strip()
            self.dump()
        if not self.summary["ok"]:
            raise RuntimeError("STOP: step4/step5/cleanup failed; do not retry")
        print("RESULT: STEP4_AND_STEP5_READONLY_DONE. No production switch.", flush=True)
        return self.summary


def plan_only(bind: Bind) -> dict[str, Any]:
    """Read-only resolved argv / paths; no writes."""
    return {
        "ok": True,
        "read_only": True,
        "writes": 0,
        "version": bind.version,
        "attempt": bind.attempt,
        "acceptance_step4_dir": str(bind.A),
        "reports_step4_dir": str(bind.O),
        "releases_package_dir": str(bind.PKG),
        "helpers_dir": str(bind.W),
        "stage_dir": str(bind.stage_dir),
        "candidate": str(bind.C),
        "previous_package": str(bind.previous_package),
        "helper_steps": [h[0] for h in HELPER_STEPS],
        "fourteen_keys": list(FOURTEEN_KEYS),
        "expected_helper_argv": [
            [str(PIN), "-B", str((bind.W if where == "helpers" else bind.stage_dir) / name)]
            for name, _out, where in HELPER_STEPS
        ],
        "note": "plan only; formal build/rehearsal not executed",
    }
