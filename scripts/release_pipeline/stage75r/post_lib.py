"""Post-switch library: language-split syntax, atomic authorized-file sync, taskcards path
proof, full-set in-memory compilation. Ported by purpose from the accepted v1.6.14
post_switch_recovery1.py (0c28bd0c…) and post-switch-v1.6.14-v1614-r7.py (49ea00ed…).

No .pyc is ever written: every Python check is an in-memory compile(); Shell checks
run /bin/bash -n on the real path. Failed new-content staging is retained for the
lead (never a silent half-write, never a whole-tree backup).
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

from common import StopError, sha, sha_bytes
from monitor_contract import runtime_files


def syntax_check(name, data, path):
    """Language-split syntax gate, binding each file's sha256.
    .py -> in-memory compile(exec); .command -> /bin/bash -n; other -> refused."""
    suffix = Path(name).suffix
    digest = sha_bytes(data)
    if suffix == '.py':
        try:
            compile(data, str(path), 'exec')
        except SyntaxError as exc:
            raise StopError('STOP: python syntax check failed: %s: %s' % (name, exc)) from exc
        return {'file': name, 'kind': 'python', 'method': 'compile(exec)', 'ok': True, 'sha256': digest}
    if suffix == '.command':
        q = subprocess.run(['/bin/bash', '-n', str(path)], capture_output=True, text=True)
        if q.returncode != 0:
            raise StopError('STOP: shell syntax check failed: %s\n%s' % (name, q.stderr.strip()))
        return {'file': name, 'kind': 'shell', 'method': '/bin/bash -n', 'ok': True,
                'exit_code': q.returncode, 'sha256': digest}
    raise StopError('STOP: unknown file suffix; report, do not guess: %s' % name)


def stage_new_file(target, data, mode, out_dir, *, fault=None):
    """B04: stage new content in the TARGET'S OWN directory, without publishing.

    The target must be lstat-truly-absent (a FIFO/dir/symlink is not 'absent')
    and its parent chain must be real directories. The staged temp is fully
    written + fsync'd + fchmod'd to the declared mode, then its bytes and mode
    are re-read and checked. The live target stays absent; publication is a
    separate atomic step (B05). Any failure retains the staged artifact under
    out_dir/_待删 with a readable reason -- no silent half-write, no backup.

    'fault' is a rehearsal-only interruption seam: 'after_stage' raises after the
    staged bytes/mode are verified, to exercise the evidence window.
    """
    target = Path(target)
    parent = target.parent
    if parent.is_symlink() or not parent.is_dir():
        raise StopError('STOP: stage target parent not a real directory: %s' % parent)
    for part in parent.parents:
        if part.is_symlink():
            raise StopError('STOP: stage target parent chain symlinked: %s' % part)
        if part == part.parent:
            break
    if target.is_symlink():
        raise StopError('STOP: stage target is a symlink: %s' % target)
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    else:
        raise StopError('STOP: stage target not absent (lstat): %s' % target)
    if not isinstance(mode, int) or not 0 <= mode <= 0o7777:
        raise StopError('STOP: invalid declared install mode: %r' % (mode,))
    digest = sha_bytes(data)
    descriptor, temporary = tempfile.mkstemp(prefix='.stage75r-new-', dir=str(parent))
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        staged_sha = sha(temporary)
        if staged_sha != digest:
            raise StopError('STOP: staged bytes differ from declared hash: %s' % temporary)
        staged_mode = stat.S_IMODE(os.stat(temporary).st_mode)
        if staged_mode != mode:
            raise StopError('STOP: staged mode differs from declared mode: %s' % temporary)
        if fault == 'after_stage':
            raise RuntimeError('rehearsal interruption after_stage')
        return {'target': str(target), 'staged_path': temporary,
                'sha256': staged_sha, 'mode': '0o%o' % staged_mode,
                'target_still_absent': not target.exists() and not target.is_symlink(),
                'published': False}
    except BaseException as exc:
        pending = Path(out_dir) / '_待删'
        pending.mkdir(parents=True, exist_ok=True)
        retained = pending / Path(temporary).name
        if os.path.exists(temporary):
            os.replace(temporary, retained)
        with (pending / '说明.md').open('a', encoding='utf-8') as handle:
            handle.write('新文件暂存失败（不是旧文件备份），目标保持缺席，供主力核对：%s；原因：%s\n'
                         % (retained, exc))
        raise StopError('STOP: stage failed; staged artifact retained for review: %s' % retained) from exc


def create_absent(target, data, mode, out_dir, *, fault=None):
    """B05: publish an ABSENT target by exclusive atomic hard-link, zero overwrite.

    Content and mode are completed in the same-directory staged artifact first
    (B04); os.link then publishes the complete inode atomically and FAILS if any
    object (regular file, directory, symlink, FIFO) occupies the target -- the
    occupant is never opened, linked through, or replaced. The target is either
    absent or fully formed+verified; no half-written object ever appears.

    fault='before_publish' is a rehearsal fixture: another writer places a real
    occupant after staging but before link, exercising the lost-race path.
    """
    target = Path(target)
    staged = stage_new_file(target, data, mode, out_dir)
    temporary = Path(staged['staged_path'])
    try:
        if fault == 'before_publish':
            with target.open('xb') as handle:
                handle.write(b'rehearsal concurrent occupant\n')
                handle.flush()
                os.fsync(handle.fileno())
        os.link(temporary, target)              # atomic; FileExistsError if occupied
        temporary.unlink()
    except BaseException as exc:
        pending = Path(out_dir) / '_待删'
        pending.mkdir(parents=True, exist_ok=True)
        retained = pending / temporary.name
        if temporary.exists():
            os.replace(temporary, retained)
        with (pending / '说明.md').open('a', encoding='utf-8') as handle:
            handle.write('排他创建失败（占位零覆盖），暂存件保留供核对：%s；原因：%s\n'
                         % (retained, exc))
        raise StopError('STOP: exclusive atomic create failed; occupant preserved, '
                        'staged artifact retained: %s' % retained) from exc
    # readback: the published target must match the declared bytes and mode
    if sha(target) != staged['sha256'] or stat.S_IMODE(target.stat().st_mode) != mode:
        raise StopError('STOP: published target failed readback: %s' % target)
    return {'target': str(target), 'sha256': sha(target),
            'mode': '0o%o' % stat.S_IMODE(target.stat().st_mode),
            'created_exclusively': True, 'overwrites': 0,
            'staged_path': staged['staged_path'], 'fault': fault}


def classify_existing(target, live_before, expected_after=None):
    """B06 three-state classification of an EXISTING replacement target.

    Checks the object itself (lstat regular file, not symlink/FIFO/dir) and the
    real parent chain -- not just the path string, so startup compatibility and
    file-type checks are never degraded to path matching. State is 'before'
    (pinned old bytes), 'target' (already at authorized postimage), or a hard
    STOP for any third hash (drift / half-write).
    """
    target = Path(target)
    if target.is_symlink():
        raise StopError('STOP: existing target is a symlink: %s' % target)
    try:
        info = target.lstat()
    except FileNotFoundError as exc:
        raise StopError('STOP: existing target missing: %s' % target) from exc
    if not stat.S_ISREG(info.st_mode):
        raise StopError('STOP: existing target is not a regular file: %s' % target)
    for parent in target.parents:
        if parent.is_symlink():
            raise StopError('STOP: existing target parent symlinked: %s' % parent)
        if parent == parent.parent:
            break
    current = sha(target)
    if expected_after is not None and current == expected_after:
        state = 'target'
    elif current == live_before:
        state = 'before'
    else:
        raise StopError('STOP: existing target drifted (neither pinned before nor '
                        'authorized target): %s' % target)
    return {'state': state, 'sha256': current,
            'mode': stat.S_IMODE(info.st_mode)}


def readback_synced(target, expected_sha, expected_mode=None):
    """B07: read back a just-installed target before the next dependency step.

    Checks object type (lstat regular, not symlink/FIFO/dir), the declared bytes,
    and optionally the declared mode. Any mismatch stops the ordered sync.
    """
    target = Path(target)
    if target.is_symlink():
        raise StopError('STOP: synced target became a symlink: %s' % target)
    try:
        info = target.lstat()
    except FileNotFoundError as exc:
        raise StopError('STOP: synced target missing at readback: %s' % target) from exc
    if not stat.S_ISREG(info.st_mode):
        raise StopError('STOP: synced target not a regular file: %s' % target)
    current = sha(target)
    if current != expected_sha:
        raise StopError('STOP: synced target hash mismatch at readback: %s' % target)
    mode = stat.S_IMODE(info.st_mode)
    if expected_mode is not None and mode != expected_mode:
        raise StopError('STOP: synced target mode mismatch at readback: %s (got 0o%o, want 0o%o)'
                        % (target, mode, expected_mode))
    return {'target': str(target), 'sha256': current, 'mode': '0o%o' % mode, 'ok': True}


def atomic_sync(target, data, expected_before, out_dir, *, expected_after=None):
    """Atomic single-file install with failed-staging retention. Refuses if the
    live target drifted from the pinned before-hash (checked twice: before staging
    and immediately before replace). Preserves the target's permission mode.
    B06: the object/path recheck and three-state rule come from classify_existing.
    """
    target = Path(target)
    if expected_after is not None and sha_bytes(data) != expected_after:
        raise StopError('STOP: replacement bytes do not match authorized target: %s' % target)
    state = classify_existing(target, expected_before, expected_after)
    if state['state'] != 'before':
        raise StopError('STOP: existing target already at authorized target; '
                        'caller must skip, not re-sync: %s' % target)
    mode = state['mode']
    descriptor, temporary = tempfile.mkstemp(prefix='.stage75r-sync-', dir=str(target.parent))
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        # recheck path + hash immediately before replace (no drift / type downgrade)
        if classify_existing(target, expected_before, expected_after) != state:
            raise StopError('STOP: live file changed during synchronization: %s' % target)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            pending = Path(out_dir) / '_待删'
            pending.mkdir(parents=True, exist_ok=True)
            retained = pending / Path(temporary).name
            os.replace(temporary, retained)
            with (pending / '说明.md').open('a', encoding='utf-8') as handle:
                handle.write('失败的新内容暂存（不是旧文件备份），供主力核对清理：%s\n' % retained)
    return {'file': target.name, 'before_sha256': expected_before, 'after_sha256': sha(target),
            'mode': '0o%o' % stat.S_IMODE(target.stat().st_mode)}


def _load_live_config(monitor_dir):
    config_path = Path(monitor_dir) / 'monitor_core/config.py'
    if config_path.is_symlink() or not config_path.is_file():
        raise StopError('STOP: live monitor config.py missing or symlinked: %s' % config_path)
    package_name = '_stage75r_live_monitor_core_' + sha_bytes(os.fsencode(config_path.parent))[:16]
    package = ModuleType(package_name)
    package.__path__ = [str(config_path.parent)]
    spec = importlib.util.spec_from_file_location(package_name + '.config', config_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    sys.modules[spec.name] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
        for name in tuple(sys.modules):
            if name == package_name or name.startswith(package_name + '.'):
                sys.modules.pop(name)
    return module, config_path


def resolved_taskcards(monitor_dir):
    """Resolve taskcards dirs/ledger/receipt_root using the LIVE config loader and
    resolver, then Path.resolve. Read-only; returns absolute path strings."""
    monitor_dir = Path(monitor_dir)
    module, config_path = _load_live_config(monitor_dir)
    loaded = module.load(monitor_dir / 'monitor.json')
    if Path(loaded['root']).resolve() != monitor_dir.resolve():
        raise StopError('STOP: live config root differs from monitor dir')
    settings = loaded['taskcards']
    resolved = {'dirs': [str(module.resolve(loaded, item).resolve()) for item in settings['dirs']],
                'ledger': str(module.resolve(loaded, settings['ledger']).resolve()),
                'receipt_root': str(module.resolve(loaded, settings['receipt_root']).resolve())}
    return resolved, str(config_path), sha(config_path)


def taskcards_proof(monitor_dir, expected):
    """Prove the synced live config resolves taskcards to the SAME authorized
    absolute paths captured before sync (before/after equality, no rebase)."""
    actual, resolver_file, resolver_sha = resolved_taskcards(monitor_dir)
    monitor_json = json.loads((Path(monitor_dir) / 'monitor.json').read_text(encoding='utf-8'))
    proof = {'ok': actual == expected, 'root': str(monitor_dir),
             'resolver_file': resolver_file, 'resolver_sha256': resolver_sha,
             'actual': actual, 'expected': expected,
             'method': 'live config.load + config.resolve + Path.resolve; exact equality with pre-sync capture',
             'monitor_json_taskcards_absent': 'taskcards' not in monitor_json}
    if not proof['ok']:
        raise StopError('STOP: resolved taskcards paths differ from pre-sync capture: %s'
                        % json.dumps(proof, ensure_ascii=False))
    return proof


def compile_runtime_set(repository, monitor_dir):
    """In-memory compile every live .py runtime file; /bin/bash -n every live
    .command. No .pyc is written anywhere. Returns per-file check records."""
    checks = []
    for relative in sorted(runtime_files(repository)):
        live_path = Path(monitor_dir) / relative
        if relative.endswith('.py'):
            try:
                compile(live_path.read_bytes(), str(live_path), 'exec')
            except SyntaxError as exc:
                raise StopError('STOP: live python syntax check failed: %s: %s' % (relative, exc)) from exc
            checks.append({'file': relative, 'kind': 'python', 'method': 'compile(exec)', 'ok': True})
        elif relative.endswith('.command'):
            q = subprocess.run(['/bin/bash', '-n', str(live_path)], capture_output=True, text=True)
            if q.returncode != 0:
                raise StopError('STOP: live shell syntax check failed: %s\n%s' % (relative, q.stderr.strip()))
            checks.append({'file': relative, 'kind': 'shell', 'method': '/bin/bash -n',
                           'ok': True, 'exit_code': q.returncode})
    return checks
