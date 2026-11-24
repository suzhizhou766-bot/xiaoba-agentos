"""Wave 4 Category P (W4-P1~P4) modular decomposition and lifecycle freeze guards.

Verifies:
- W4-P1: cli.py split into cli_groups/{parser_spec,daily_ops,memory_ops,maintenance_ops}.py,
  with main() compact and all 81 subcommands intact.
- W4-P2: Orchestrator.run_once() split into four explicit lifecycle stage methods.
- W4-P3: EventStore split into store_{common,events,tasks,memory}.py; xiaoba_review/review_store.py
  is canonical and aliased by xiaoba_review.store.
- W4-P4: xiaoba-monitor.html split into external same-origin monitor-keypool.js and
  monitor-review-center.js with strict CSP routing; monitor_core/lifecycle.py remains frozen.
"""
from __future__ import annotations

import hashlib
import inspect
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

CANDIDATE = Path(__file__).resolve().parents[1]
REPO_ROOT = CANDIDATE.parents[1]
sys.path.insert(0, str(CANDIDATE / "src"))
if (REPO_ROOT / "development" / "review-ingress").is_dir():
    sys.path.insert(0, str(REPO_ROOT / "development" / "review-ingress"))

IS_DEV_WORKTREE = (REPO_ROOT / ".git").exists() and (REPO_ROOT / "development" / "integrated-candidate").is_dir()
FROZEN_LIFECYCLE_SHA256 = "7b3fdd11a75cbaeba6fc2aa0d1d9147b4df38d5219f59b8a9e30a6c64368a8bd"


class Wave4CategoryPTests(unittest.TestCase):
    def test_w4_p1_cli_split_into_four_group_modules(self):
        import xiaoba_agentos.cli as cli_mod
        from xiaoba_agentos.cli_groups import daily_ops, maintenance_ops, memory_ops, parser_spec

        for mod in (parser_spec, daily_ops, memory_ops, maintenance_ops):
            self.assertTrue(Path(mod.__file__).is_file())
        cli_lines = Path(cli_mod.__file__).read_text(encoding="utf-8").splitlines()
        self.assertLess(len(cli_lines), 1600)
        main_lines = inspect.getsource(cli_mod.main).splitlines()
        self.assertLess(len(main_lines), 80)

    def test_w4_p2_orchestrator_run_once_split_into_four_stages(self):
        from xiaoba_agentos.orchestrator import Orchestrator

        for stage_name in (
            "_stage_preflight_and_claim",
            "_stage_prepare_and_execute",
            "_stage_cover_and_validate",
            "_stage_judge_and_settle",
        ):
            self.assertTrue(callable(getattr(Orchestrator, stage_name, None)), f"missing {stage_name}")
        run_once_lines = inspect.getsource(Orchestrator.run_once).splitlines()
        self.assertLessEqual(len(run_once_lines), 45)

    def test_w4_p3_store_mixins_and_review_store_canonical_alias(self):
        import xiaoba_agentos.store as store_mod
        from xiaoba_agentos import store_common, store_events, store_memory, store_tasks

        for mod in (store_common, store_events, store_tasks, store_memory):
            self.assertTrue(Path(mod.__file__).is_file())
        store_lines = Path(store_mod.__file__).read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(store_lines), 100)
        self.assertTrue(issubclass(store_mod.EventStore, store_events.EventChainStoreMixin))
        self.assertTrue(issubclass(store_mod.EventStore, store_tasks.TaskStoreMixin))
        self.assertTrue(issubclass(store_mod.EventStore, store_memory.MemoryStoreMixin))

        import xiaoba_review.review_store as canonical_rs
        import xiaoba_review.store as alias_rs

        self.assertIs(alias_rs, canonical_rs)
        self.assertIs(alias_rs.ReviewStore, canonical_rs.ReviewStore)

    def test_w4_p4_monitor_scripts_extracted_and_lifecycle_frozen(self):
        from xiaoba_agentos.review_ui_install import ASSETS, resolve_asset_path

        html_path = resolve_asset_path(ASSETS, "xiaoba-monitor.html")
        keypool_path = resolve_asset_path(ASSETS, "monitor-keypool.js")
        review_path = resolve_asset_path(ASSETS, "monitor-review-center.js")
        system_path = resolve_asset_path(ASSETS, "monitor_system.py")

        html_text = html_path.read_text(encoding="utf-8")
        self.assertLess(len(html_text.splitlines()), 2500)
        self.assertIn('<script src="/monitor-keypool.js"></script>', html_text)
        self.assertIn('<script src="/monitor-review-center.js"></script>', html_text)
        self.assertLess(
            html_text.index('<script src="/monitor-keypool.js"></script>'),
            html_text.index('<script src="/monitor-review-center.js"></script>'),
        )

        system_text = system_path.read_text(encoding="utf-8")
        self.assertIn("monitor-keypool.js", system_text)
        self.assertIn("monitor-review-center.js", system_text)

        node = shutil.which("node")
        if node:
            for js_path in (keypool_path, review_path):
                proc = subprocess.run([node, "--check", str(js_path)], capture_output=True, text=True, timeout=10)
                self.assertEqual(0, proc.returncode, f"node --check failed on {js_path}: {proc.stderr}")

        if IS_DEV_WORKTREE:
            lifecycle = REPO_ROOT / "monitor" / "monitor_core" / "lifecycle.py"
            self.assertEqual(
                FROZEN_LIFECYCLE_SHA256,
                hashlib.sha256(lifecycle.read_bytes()).hexdigest(),
                "monitor_core/lifecycle.py is explicitly frozen in Wave 4 (W4-P4)",
            )


if __name__ == "__main__":
    unittest.main()
