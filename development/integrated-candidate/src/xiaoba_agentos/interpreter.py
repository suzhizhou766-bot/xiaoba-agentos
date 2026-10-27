"""The one interpreter the AgentOS core runs on.

User decision 2026-10-14: the core moves from the system Python 3.9
(Command Line Tools) to the shared Python 3.14 under 私人助理/配置.  The same absolute path
is written into bin/agentos and the two entry scripts' first line; a test
keeps the three in agreement with PINNED_PYTHON.  3.15 is deliberately not
used: one unexplained extra failure in five full runs (2026-10-14).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict

PINNED_PYTHON = Path(
    "/Users/suzhizhou/Documents/私人助理/配置/python/"
    "cpython-3.14.7-macos-aarch64-none/bin/python3.14"
)
PINNED_VERSION = (3, 14)


def describe() -> Dict[str, Any]:
    """Report which interpreter is running and whether it is the pinned one."""
    running = Path(sys.executable)
    try:
        matches = running.resolve() == PINNED_PYTHON.resolve()
    except OSError:
        matches = False
    return {
        "running": str(running),
        "running_version": "%d.%d.%d" % sys.version_info[:3],
        "pinned": str(PINNED_PYTHON),
        "pinned_version": "%d.%d" % PINNED_VERSION,
        "pinned_available": PINNED_PYTHON.is_file() and os.access(PINNED_PYTHON, os.X_OK),
        "running_is_pinned": matches,
    }
