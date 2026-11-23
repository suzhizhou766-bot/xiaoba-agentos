"""Shared low-level helpers for stage75r (real switch + post-switch recovery).

Ported by purpose from the accepted v1.6.14 tools (run_switch.py 42ca2bad…,
post_switch_recovery1.py 0c28bd0c…, post-switch-v1.6.14-v1614-r7.py 49ea00ed…).
Nothing here is release-specific: every path/version/attempt arrives via the
shared release context and the ONE attempt record, never hardcoded.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

PIN = Path('/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14')


class StopError(RuntimeError):
    """Explicit refusal. Carries a human-readable STOP reason; never a silent ok."""


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    p = Path(path)
    if p.is_symlink() or not p.is_file():
        raise StopError('STOP: required JSON missing or symlinked: %s' % p)
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except ValueError as exc:
        raise StopError('STOP: invalid JSON in %s: %s' % (p, exc)) from exc


def guard_runtime():
    """Reject optimization / bytecode writing / wrong interpreter BEFORE any write.

    The shared release_context.check_interpreter is the strict gate used by the
    CLI; this is the leaf-level guard so a directly imported leaf also refuses
    -O/-OO (which would silently disable any assert-based check downstream).
    """
    if getattr(sys.flags, 'optimize', 0) != 0:
        raise StopError('STOP: python optimization (-O/-OO) detected; refuse before any write')
    if not sys.dont_write_bytecode:
        raise StopError('STOP: bytecode writing enabled; rerun with -B')
    if Path(sys.executable).resolve() != PIN.resolve():
        raise StopError('STOP: run under the unified interpreter %s (got %s)' % (PIN, sys.executable))


def clean_env():
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONOPTIMIZE')}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    return env


def atomic_dump(path, value, overwrite=False):
    """tmp + fsync + os.replace. Exclusive by default: an existing record is never
    clobbered; a fault before replace leaves the original byte-for-byte intact."""
    path = Path(path)
    if not overwrite and (path.exists() or path.is_symlink()):
        raise StopError('STOP: record already exists; preserve evidence: %s' % path)
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    tmp = path.with_name(path.name + '.tmp-%d' % os.getpid())
    try:
        with tmp.open('xb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def run_cli_json(argv, *, cwd, env, log_path, timeout=None):
    """Actually run a command, capture stdout to an exclusive log file, parse JSON.

    stderr is captured to a sibling .stderr log so stdout stays pure JSON.
    Returns (returncode, value, text). The caller decides what ok/activated/writes
    mean; this leaf never invents success. A non-zero exit or unparsable output is
    surfaced verbatim (logs preserved), never retried here.
    """
    log_path = Path(log_path)
    if log_path.exists() or log_path.is_symlink():
        raise StopError('STOP: command log already exists; preserve evidence: %s' % log_path)
    err_path = log_path.with_name(log_path.name + '.stderr')
    with log_path.open('x', encoding='utf-8') as handle, err_path.open('x', encoding='utf-8') as err:
        q = subprocess.run([str(a) for a in argv], cwd=str(cwd), env=env,
                           stdout=handle, stderr=err, timeout=timeout)
    text = log_path.read_text(encoding='utf-8')
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    return q.returncode, value, text