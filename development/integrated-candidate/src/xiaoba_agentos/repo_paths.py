"""W3-M3: Single authoritative source for repository-relative layout constants.

Uses top-level ``monitor`` as the one authoritative monitor source tree.
"""
from __future__ import annotations

from pathlib import Path

MONITOR_REL_PATH = "monitor"


def resolve_monitor_root(repo_root: Path) -> Path:
    """Return the authoritative monitor directory under ``repo_root``."""
    return Path(repo_root) / MONITOR_REL_PATH


def resolve_repo_root_from_src(src_pkg_dir: Path) -> Path | None:
    """Given ``.../development/integrated-candidate/src/xiaoba_agentos``, return repo root."""
    pkg = Path(src_pkg_dir).resolve()
    # .../repo/development/integrated-candidate/src/xiaoba_agentos -> parents[3] == repo
    if len(pkg.parents) >= 4:
        candidate_repo = pkg.parents[3]
        if (candidate_repo / "development").is_dir():
            return candidate_repo
    return None
