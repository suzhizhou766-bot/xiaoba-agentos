"""Neutral baseline launcher for running unittests per suite without hardcoding counts.

Outputs real execution metrics: testsRun, skipped, and status.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any


def run_suite(source_path: Path | str, suite: str) -> dict[str, Any]:
    source = Path(source_path).resolve()
    roots = {
        'core': source / 'development/integrated-candidate/tests',
        'ingress': source / 'development/review-ingress/tests',
        'architecture': source / 'development/architecture-center/tests',
        'monitor': source / 'monitor/tests',
    }
    if suite not in roots:
        raise ValueError(f'Unknown suite: {suite}. Must be one of {list(roots.keys())}')

    root = roots[suite]
    if not root.is_dir():
        raise FileNotFoundError(f'Test directory for suite {suite} missing at {root}')

    # Safe sys.path insertion
    sys.path[:0] = [
        str(source / 'development/integrated-candidate/src'),
        str(source / 'development/review-ingress'),
        str(root),
        str(root.parent),
    ]

    suite_obj = unittest.defaultTestLoader.discover(str(root))
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite_obj)

    out = {
        'suite': suite,
        'tests': result.testsRun,
        'skipped': len(result.skipped),
        'errors': len(result.errors),
        'failures': len(result.failures),
        'ok': result.wasSuccessful(),
    }
    return out


def main() -> None:
    if len(sys.argv) < 3:
        sys.stderr.write('Usage: baseline_runner.py <source_path> <suite>\n')
        sys.exit(2)

    assert 'PYTHONPATH' not in os.environ, 'STOP: PYTHONPATH in env'
    assert sys.dont_write_bytecode, 'STOP: bytecode writing enabled'

    source = Path(sys.argv[1]).resolve()
    suite = sys.argv[2]
    out = run_suite(source, suite)
    print(json.dumps(out, ensure_ascii=False), flush=True)
    sys.exit(0 if out['ok'] else 1)


if __name__ == '__main__':
    main()
