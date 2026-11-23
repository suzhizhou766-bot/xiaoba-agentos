"""Read-only monitor runtime contract for the authorized post-switch sync.

Ported by purpose from v1.6.14 monitor_contract.py (b71759ce…). Differences:
  * the authorized sync set is passed in as monitor-root-relative paths;
  * the runtime file count is COMPUTED from the actual same-source set, never
    asserted to a hardcoded 32 (the card forbids fixed-output 32).
"""
import hashlib
import json
import os
import stat
from pathlib import Path

from common import StopError

_SUFFIXES = {'.py', '.js', '.css', '.html', '.command'}
_EXCLUDE_PARTS = {'tests', 'logs', '__pycache__', '.runtime', '.git'}


def relative_name(value):
    """Validate a canonical POSIX relative file identity before any path join."""
    if (not isinstance(value, str) or not value or '\\' in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(part in ('', '.', '..') for part in value.split('/'))
            or Path(value).is_absolute()):
        raise StopError('STOP: invalid monitor relative path: %r' % value)
    return value


def rooted_path(root, name):
    """Join a validated identity, refusing symlinks before resolve hides them.

    Files may be absent for the read-only pre-export plan. macOS system aliases
    are the same explicit exceptions used by the shared release context.
    """
    name = relative_name(name)
    root = Path(root)
    if not root.is_absolute():
        raise StopError('STOP: monitor path root must be absolute: %s' % root)
    path = root / name
    for part in (path, *path.parents):
        if part.is_symlink() and part not in (Path('/var'), Path('/tmp'), Path('/etc')):
            raise StopError('STOP: monitor path or parent symlinked: %s' % part)
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise StopError('STOP: monitor path escapes root: %s' % path) from exc
    return path


def runtime_files(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise StopError('STOP: monitor runtime root missing or symlinked: %s' % root)
    paths = [p for p in root.glob('*') if p.is_file()]
    for directory in ('monitor_core', 'tools'):
        sub = root / directory
        if sub.is_symlink():
            raise StopError('STOP: symlinked monitor runtime directory: %s' % sub)
        if sub.is_dir():
            paths.extend(sub.rglob('*'))
    result = {}
    for path in paths:
        relative = path.relative_to(root)
        if any(part in _EXCLUDE_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            raise StopError('STOP: symlinked monitor runtime path: %s' % path)
        if not path.is_file():
            continue
        if path.suffix not in _SUFFIXES:
            continue
        rooted_path(root, relative.as_posix())
        result[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def compare(repository, live):
    expected = runtime_files(repository)
    actual = runtime_files(live)
    return {'ok': expected == actual, 'mode': 'read_only_runtime_code_comparison',
            'source': str(repository), 'live': str(live),
            'missing': sorted(expected.keys() - actual.keys()),
            'extra': sorted(actual.keys() - expected.keys()),
            'changed': sorted(n for n in expected.keys() & actual.keys() if expected[n] != actual[n]),
            'expected_sha256': expected, 'actual_sha256': actual,
            'expected_file_count': len(expected), 'actual_file_count': len(actual),
            'monitor_files_copied': 0, 'monitor_files_patched': 0,
            'scope': 'Monitor root runtime files + monitor_core/ + tools/; 3D projects, tests/logs/state/credentials/docs excluded.'}


def sync_precheck(repository, live, *, sync_files, expected_export, live_before,
                  ui_files=None, install=None, installed_modes=None):
    """Read-only authorized-sync precheck (ZERO writes). sync_files is the
    authorized name list; expected_export / live_before are {name: sha256}.

    The whole directory is compared strictly: after the ONLY permitted absences
    (the authorized first_install target, plus UI assets independently allowed by
    their own manifest rule) the repo set and live set must be identical, with no
    extra and no unauthorized change. Every sync file may sit at its pinned old
    hash (not yet installed) OR at the authorized export postimage (resumable).
    Both sources are SHA-bound: repo bytes are recomputed by compare(), export
    hashes come from the B01 record-bound pin, and they must agree per file.

    A first_install file found at its postimage additionally has its INSTALLED
    mode checked against the declared mode (installed_modes = observed {name:
    '0o644'}); a missing/mismatched observation refuses. That makes the same
    declaration readable after a first install without accepting a wrong mode.
    """
    repository = Path(repository)
    live = Path(live)
    sync_files = [relative_name(name) for name in sync_files]
    if not sync_files or len(set(sync_files)) != len(sync_files):
        raise StopError('STOP: authorized sync set empty or duplicated')
    if set(expected_export) != set(sync_files) or set(live_before) != set(sync_files):
        raise StopError('STOP: authorized sync hash keys do not match file identities')
    for root in (repository, live):
        for name in sync_files:
            rooted_path(root, name)
    result = compare(repository, live)
    ui_files = ui_files or {}
    if set(sync_files) & set(ui_files):
        raise StopError('STOP: product UI installer and generic sync targets overlap')
    for name in ui_files:
        rooted_path(repository, name)
        rooted_path(live, name)
    first = {n for n, r in (install or {}).items() if r.get('policy') == 'first_install'}
    if not first <= set(sync_files):
        raise StopError('STOP: first-install declaration outside authorized sync set: %s'
                        % sorted(first - set(sync_files)))
    first_absent = {n for n in first if n not in result['actual_sha256']}
    # lstat gate: runtime_files does not collect FIFO/dir/symlink, so such an
    # object at a first_install target would otherwise look "absent". A captured
    # absent preimage must still be TRULY absent at lstat time, with the parent
    # chain regular. A regular file at the postimage is the legitimate resume
    # case and is handled by the mode gate below, not here.
    absent_lstat = {}
    for name in sorted(first_absent):
        target = live / name
        if target.is_symlink():
            raise StopError('STOP: first-install target became a symlink after absent '
                            'preimage: %s' % target)
        try:
            info = target.lstat()
        except FileNotFoundError:
            info = None
        if info is not None:
            raise StopError('STOP: first-install target no longer truly absent (%s): %s'
                            % ('directory' if stat.S_ISDIR(info.st_mode)
                               else 'fifo' if stat.S_ISFIFO(info.st_mode)
                               else 'special object', target))
        for parent in target.parents:
            if parent == live:
                break
            if parent.is_symlink() or not parent.is_dir():
                raise StopError('STOP: first-install parent chain not regular: %s' % parent)
        absent_lstat[name] = 'lstat_absent_parent_regular'
    expected = result['expected_sha256']
    actual = result['actual_sha256']
    # Postimage mode gate: a first_install target already present at the export
    # hash must also carry the declared install mode; absence needs no mode.
    modes = installed_modes or {}
    first_postimage = {n for n in first if actual.get(n) == expected_export[n]}
    mode_ok = True
    mode_detail = {}
    for name in sorted(first_postimage):
        declared = (install or {})[name].get('mode')
        observed = modes.get(name)
        good = bool(declared) and observed == declared
        mode_ok = mode_ok and good
        mode_detail[name] = {'declared': declared, 'observed': observed, 'ok': good}
    # Official UI prepare already constrained these observations to the exact
    # manifest preimage or postimage. An absent observation is permitted only
    # through that check. Recompare here to reject intervening drift.
    ui_missing = {name for name, row in ui_files.items() if row['observed_sha256'] is None}
    allowed_absent = ui_missing | first_absent
    counts_match = len(expected) - len(allowed_absent) == len(actual)
    allowed_differences = (set(result['missing']) == allowed_absent and not result['extra']
                           and set(result['changed']) <= set(sync_files) | set(ui_files))
    drift = [name for name in sync_files
             if actual.get(name) not in (live_before[name], expected_export[name])]
    pinned_live = not drift
    ui_bound = all(expected.get(name) == row['after_sha256']
                   and actual.get(name) == row['observed_sha256']
                   for name, row in ui_files.items())
    sources_regular = True
    for root in (repository, live):
        for name in sync_files:
            if root == live and name in first_absent:
                continue
            path = root / name
            sources_regular = sources_regular and path.is_file() and not path.is_symlink()
            for parent in path.parents:
                if parent == root:
                    break
                sources_regular = sources_regular and not parent.is_symlink()
    config_path = live / 'monitor.json'
    config_regular = config_path.is_file() and not config_path.is_symlink()
    absent = False
    if config_regular:
        data = json.loads(config_path.read_text(encoding='utf-8'))
        absent = isinstance(data, dict) and 'taskcards' not in data
    export_matches = (set(expected_export) == set(sync_files)
                      and set(sync_files) <= set(expected)
                      and all(expected.get(name) == expected_export[name] for name in sync_files))
    result.update(mode='read_only_authorized_sync_precheck',
                  ok=bool(counts_match and allowed_differences and pinned_live and sources_regular
                          and config_regular and absent and export_matches and ui_bound and mode_ok),
                  allowed_sync_files=list(sync_files), runtime_file_count_matches=counts_match,
                  strict_whole_dir_compare=bool(counts_match and allowed_differences),
                  dual_source_sha_match=bool(export_matches and pinned_live),
                  only_authorized_differences=allowed_differences, live_pinned_hashes_match=pinned_live,
                  live_drifted_files=drift,
                  first_install_postimage_modes_match=mode_ok,
                  first_install_postimage_modes=mode_detail,
                  sources_regular=sources_regular, monitor_json_regular=config_regular,
                  monitor_json_taskcards_absent=absent, export_hashes_match=export_matches,
                  ui_assets_match=ui_bound, ui_assets_allowed_missing=sorted(ui_missing),
                  first_install_allowed_absent=sorted(first_absent),
                  first_install_absent_lstat=absent_lstat,
                  live_before_sha256=dict(live_before),
                  export_sha256={name: expected.get(name) for name in sync_files})
    return result
