"""Verify generated second brain tree against canonical structure.

Calls candidate scripts/build_second_brain.py --verify.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def verify_brain(
    candidate_root: Path | str,
    target_brain: Path | str,
    live_root: Path | str,
    python_bin: Path | str | None = None,
) -> bool:
    cand = Path(candidate_root).resolve()
    target = Path(target_brain).resolve()
    live = Path(live_root).resolve()
    script = cand / 'scripts' / 'build_second_brain.py'
    if not script.is_file():
        raise SystemExit(f'STOP: build_second_brain.py missing at {script}')

    py = str(python_bin or sys.executable)
    cmd = [
        py,
        '-B',
        str(script),
        '--agentos-root',
        str(cand),
        '--published-agentos-root',
        str(live),
        '--target',
        str(target),
        '--verify',
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        return False
    return True


def main() -> None:
    import release_context as rc
    ctx = rc.load_context()
    target = ctx['root'] / f'generated-brain-{ctx["round"]}'
    ok = verify_brain(ctx['candidate'], target, ctx['live'])
    raise SystemExit(0 if ok else 1)


if __name__ == '__main__':
    main()
