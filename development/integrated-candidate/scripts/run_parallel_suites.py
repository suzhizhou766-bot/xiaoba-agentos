#!/usr/bin/env python3
"""Official parallel 4-suite baseline runner (Plan A2).

Runs core, ingress, architecture, and monitor concurrently in isolated temporary
working/temp directories without polluting bytecode or leaking PYTHONPATH.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SUITES = ("core", "ingress", "architecture", "monitor")

_CHILD_DRIVER = """
import json, os, sys, unittest
from pathlib import Path

assert 'PYTHONPATH' not in os.environ, 'STOP: PYTHONPATH in env'
assert sys.dont_write_bytecode, 'STOP: bytecode writing enabled'

source = Path(sys.argv[1]).resolve()
suite = sys.argv[2]
roots = {
    'core': source / 'development/integrated-candidate/tests',
    'ingress': source / 'development/review-ingress/tests',
    'architecture': source / 'development/architecture-center/tests',
    'monitor': source / 'architecture-package-20261027/shared/工具/小八监看/tests',
}
root = roots[suite]
if not root.is_dir():
    raise FileNotFoundError(f'Test directory for suite {suite} missing at {root}')

sys.path[:0] = [
    str(source / 'development/integrated-candidate/src'),
    str(source / 'development/review-ingress'),
    str(root),
    str(root.parent),
]
suite_obj = unittest.defaultTestLoader.discover(str(root))
result = unittest.TextTestRunner(verbosity=1).run(suite_obj)
out = {
    'suite': suite,
    'tests': result.testsRun,
    'skipped': len(result.skipped),
    'errors': len(result.errors),
    'failures': len(result.failures),
    'ok': result.wasSuccessful(),
}
print('__XIAOBA_SUITE_JSON__=' + json.dumps(out, ensure_ascii=False), flush=True)
sys.exit(0 if out['ok'] else 1)
"""


def run_one_suite(
    python_exe: str,
    source_tree: Path,
    suite: str,
    log_dir: Path | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"xb-{suite[:3]}-") as sandbox:
        sandbox_path = Path(sandbox)
        cwd_dir = sandbox_path / "c"
        tmp_dir = sandbox_path / "t"
        cwd_dir.mkdir(mode=0o700)
        tmp_dir.mkdir(mode=0o700)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["TMPDIR"] = str(tmp_dir)
        proc = subprocess.run(
            [python_exe, "-B", "-c", _CHILD_DRIVER, str(source_tree), suite],
            cwd=str(cwd_dir),
            env=env,
            capture_output=True,
            text=True,
        )
    elapsed = round(time.monotonic() - started, 3)
    combined_log = (proc.stdout or "") + ("\n" if proc.stdout and proc.stderr else "") + (proc.stderr or "")
    log_bytes = combined_log.encode("utf-8")
    log_sha = hashlib.sha256(log_bytes).hexdigest()
    log_file = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        target = log_dir / f"{suite}-baseline.log"
        target.write_bytes(log_bytes)
        log_file = str(target)
    metrics: dict[str, Any] = {
        "suite": suite,
        "tests": 0,
        "skipped": 0,
        "errors": 0,
        "failures": 0,
        "ok": False,
    }
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith("__XIAOBA_SUITE_JSON__="):
            metrics = json.loads(line.split("=", 1)[1])
            break
    return {
        "suite": suite,
        "exit_code": proc.returncode,
        "elapsed_seconds": elapsed,
        "metrics": metrics,
        "log_sha256": log_sha,
        "log_path": log_file,
    }


def run_suites_parallel(
    source_tree: Path,
    suites: tuple[str, ...] = SUITES,
    *,
    python_exe: str | None = None,
    log_dir: Path | None = None,
    max_workers: int = 4,
) -> dict[str, Any]:
    exe = python_exe or sys.executable
    source = source_tree.resolve()
    wall_start = time.monotonic()
    results_by_name: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(suites)))) as pool:
        futs = {
            pool.submit(run_one_suite, exe, source, name, log_dir): name
            for name in suites
        }
        for fut in concurrent.futures.as_completed(futs):
            name = futs[fut]
            results_by_name[name] = fut.result()
    ordered = [results_by_name[name] for name in suites]
    wall_elapsed = round(time.monotonic() - wall_start, 3)
    serial_sum = round(sum(item["elapsed_seconds"] for item in ordered), 3)
    return {
        "ok": all(item["exit_code"] == 0 and item["metrics"].get("ok") for item in ordered),
        "source_tree": str(source),
        "wall_elapsed_seconds": wall_elapsed,
        "serial_sum_seconds": serial_sum,
        "tests_run": sum(int(item["metrics"].get("tests", 0)) for item in ordered),
        "skipped": sum(int(item["metrics"].get("skipped", 0)) for item in ordered),
        "errors": sum(int(item["metrics"].get("errors", 0)) for item in ordered),
        "failures": sum(int(item["metrics"].get("failures", 0)) for item in ordered),
        "suites": ordered,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_tree", type=Path)
    parser.add_argument("--suite", action="append", choices=SUITES, help="Specific suite(s); default runs all 4")
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument("--max-workers", type=int, default=4)
    args = parser.parse_args()
    selected = tuple(args.suite) if args.suite else SUITES
    report = run_suites_parallel(
        args.source_tree,
        selected,
        python_exe=sys.executable,
        log_dir=args.log_dir,
        max_workers=args.max_workers,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
