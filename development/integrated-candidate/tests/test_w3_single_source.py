"""Wave 3 (W3-M1~M5 & W3-N1) single-source-of-truth structural regression guards.

Verifies:
- W3-M1: Monitor UI assets are NOT duplicated inside review_ui_assets/ in the dev worktree;
  resolve_asset_path() resolves all 9 TARGETS cleanly (from canonical monitor/ in dev, or
  from materialized review_ui_assets/ in packaged releases), and create_manifest projects all 6.
- W3-M2 / W3-M3: Top-level monitor/ is canonical; zero hardcoded legacy
  snapshot-package references remain in active code.
- W3-M4: scripts/release_pipeline/ contains AST-valid release pipeline scripts.
- W3-M5: START_HERE.md exists, stays concise (<= 60 lines), and documents canonical roots.
- W3-N1: Legacy snapshot dirs are absent from repo root.
"""
from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path

CANDIDATE = Path(__file__).resolve().parents[1]
REPO_ROOT = CANDIDATE.parents[1]
sys.path.insert(0, str(CANDIDATE / "src"))
sys.path.insert(0, str(CANDIDATE / "scripts"))

from xiaoba_agentos.repo_paths import MONITOR_REL_PATH, resolve_monitor_root  # noqa: E402
from xiaoba_agentos.review_ui_install import (  # noqa: E402
    ASSETS,
    MONITOR_ASSET_SOURCES,
    TARGETS,
    resolve_asset_path,
)

LEGACY_PREFIX = "architecture-package-" + "20261027"
IS_DEV_WORKTREE = (REPO_ROOT / ".git").exists() and (REPO_ROOT / "development" / "integrated-candidate").is_dir()


class Wave3SingleSourceTests(unittest.TestCase):
    def test_all_nine_targets_resolve_and_exist(self):
        self.assertEqual(9, len(TARGETS))
        for asset in TARGETS:
            path = resolve_asset_path(ASSETS, asset)
            self.assertTrue(path.is_file(), f"missing resolved asset {asset} -> {path}")

    def test_dev_worktree_eliminates_duplicate_monitor_assets_and_projects_them(self):
        if not IS_DEV_WORKTREE:
            self.skipTest("dev-worktree-only guard")
        import create_manifest as cm

        monitor_root = resolve_monitor_root(REPO_ROOT)
        self.assertEqual((REPO_ROOT / MONITOR_REL_PATH).resolve(), monitor_root.resolve())
        self.assertTrue((monitor_root / "xiaoba-monitor.html").is_file())

        # W3-M1: the 6 monitor-owned files must NOT be duplicated inside review_ui_assets/ in git
        for asset, mon_rel in MONITOR_ASSET_SOURCES.items():
            self.assertFalse(
                (ASSETS / asset).exists(),
                f"duplicate monitor asset must not exist in dev review_ui_assets/: {asset}",
            )
            self.assertEqual(
                (monitor_root / mon_rel).resolve(),
                resolve_asset_path(ASSETS, asset).resolve(),
            )

        # Manifest projection still exposes all 6 under src/xiaoba_agentos/review_ui_assets/
        entries = cm.collect_entries(CANDIDATE)
        for asset in MONITOR_ASSET_SOURCES:
            key = f"src/xiaoba_agentos/review_ui_assets/{asset}"
            self.assertIn(key, entries, f"projected manifest entry missing: {key}")

    def test_zero_legacy_package_paths_in_active_code_and_removed_snapshots(self):
        if not IS_DEV_WORKTREE:
            self.skipTest("dev-worktree-only guard")
        # W3-N1: obsolete snapshot directories moved out of repo root
        self.assertFalse((REPO_ROOT / LEGACY_PREFIX).exists())
        self.assertFalse((REPO_ROOT / "release").exists())

        # W3-M3: zero legacy path literals in active code
        hits = []
        for sub in ("development", "monitor", "scripts"):
            base = REPO_ROOT / sub
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "node_modules")]
                for fn in files:
                    if not fn.endswith((".py", ".sh", ".js", ".html", ".json")):
                        continue
                    p = Path(root) / fn
                    rel = p.relative_to(REPO_ROOT).as_posix()
                    if rel.endswith("SOURCE_MANIFEST.json"):
                        continue
                    if LEGACY_PREFIX in p.read_text(encoding="utf-8", errors="ignore"):
                        hits.append(rel)
        self.assertEqual([], hits, f"legacy path still referenced in code: {hits}")

    def test_release_pipeline_scripts_ast_valid_and_start_here_concise(self):
        if not IS_DEV_WORKTREE:
            self.skipTest("dev-worktree-only guard")
        # W3-M4: collected release pipeline scripts parse cleanly
        pipeline_dir = REPO_ROOT / "scripts" / "release_pipeline"
        self.assertTrue((pipeline_dir / "README.md").is_file())
        py_files = []
        for root, dirs, files in os.walk(pipeline_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fn in files:
                if fn.endswith(".py"):
                    p = Path(root) / fn
                    py_files.append(p)
                    ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        self.assertGreaterEqual(len(py_files), 60)

        # W3-M5: START_HERE.md exists and stays concise
        start_here = REPO_ROOT / "START_HERE.md"
        self.assertTrue(start_here.is_file())
        lines = start_here.read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), 60, f"START_HERE.md too long: {len(lines)} lines")
        text = "\n".join(lines)
        self.assertIn("monitor/", text)
        self.assertIn("scripts/release_pipeline/", text)
        self.assertIn("switch_release.py", text)


if __name__ == "__main__":
    unittest.main()
