"""Cold-start leaf contracts for 75S prepare/freeze (read-only, no git mutation).

Binds three things against real Git refs at the assigned BASE before any write:

1. ``derive_export_roots`` — minimal non-overlapping export roots derived from the
   declared ``params.diff.paths`` scopes. ``development/*`` collapses to the whole
   ``development`` layout (existing test paths and create_manifest's sibling
   review-ingress projection rely on it); other scopes keep their declared root
   (monitor directory, docs/architecture-topology). Descendant roots are dropped.
2. ``verify_previous_source`` — the old live SOURCE_MANIFEST.json sha256 must equal
   ``manager.previous_source_sha256``.
3. ``verify_monitor_authorization`` — the 75A three-file authorization contract:
   params.post_files == record.syntax file set, each working-tree hash == the
   pointer syntax hash == the actual Git blob hash, AND the whole monitor
   same-source set (not just the three names) matches BASE per-blob.
4. ``verify_record_diff`` — the unique attempt ``record.diff.diff_path`` is this
   round's authority: its hash equals ``record.diff.diff_sha256`` and equals the
   actual ``git diff diff_base base -- params.scope`` bytes, so no second round
   diff with a different scope is rebuilt.

Nothing here writes, mutates git, reads credentials, or calls a model.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

import release_context as rc


def _monitor_same_source(repo: Path, base: str, monitor_root: str) -> dict[str, Any]:
    """Whole-monitor same-source set: working tree == BASE for every TRACKED source
    file, and no untracked source file. Bytecode artifacts (__pycache__/*.pyc) and
    .DS_Store are ignored — they are not source, exactly as create_manifest defines
    the managed source tree. This catches other monitor code drift, not just the
    three authorized names. Read-only: git ls-tree + per-blob sha1, no git write.
    """
    raw = subprocess.check_output(
        ['git', '-C', str(repo), 'ls-tree', '-rz', base, '--', monitor_root]
    )
    expected: dict[str, tuple[bytes, str]] = {}
    for item in raw.split(b'\0'):
        if not item:
            continue
        meta, path_bytes = item.split(b'\t', 1)
        mode, kind, oid = meta.split()
        expected[path_bytes.decode('utf-8')] = (mode, oid.decode('ascii'))

    changed: list[str] = []
    missing: list[str] = []
    for name, (mode, oid) in expected.items():
        p = repo / name
        if mode == b'120000' or not p.is_file() or p.is_symlink():
            missing.append(name)
            continue
        data = p.read_bytes()
        blob_sha = hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()
        if blob_sha != oid:
            changed.append(name)

    extra: list[str] = []
    root_path = repo / monitor_root
    if root_path.is_dir():
        for p in root_path.rglob('*'):
            if not p.is_file() or p.is_symlink():
                continue
            if '__pycache__' in p.parts or p.suffix == '.pyc' or p.name == '.DS_Store':
                continue
            rel = p.relative_to(repo).as_posix()
            if rel not in expected:
                extra.append(rel)

    result = {
        'base': base, 'monitor_root': monitor_root, 'files': len(expected),
        'changed': sorted(changed), 'missing': sorted(missing), 'extra': sorted(extra),
    }
    result['ok'] = not (changed or missing or extra)
    return result


def derive_export_roots(scopes: list[str]) -> list[str]:
    if not isinstance(scopes, (list, tuple)) or not scopes:
        raise SystemExit('STOP: params.diff.paths 为空，无法派生导出 roots')
    roots: set[str] = set()
    for raw in scopes:
        if not isinstance(raw, str) or not raw.strip():
            raise SystemExit(f'STOP: 非法 diff scope: {raw!r}')
        parts = PurePosixPath(raw).parts
        if parts and parts[0] == 'development':
            roots.add('development')
        else:
            roots.add(PurePosixPath(raw).as_posix())
    final: list[str] = []
    for r in sorted(roots):
        rp = PurePosixPath(r)
        if any(rp != PurePosixPath(o) and rp.is_relative_to(PurePosixPath(o)) for o in roots):
            continue
        final.append(r)
    if not final:
        raise SystemExit('STOP: 派生导出 roots 为空')
    return final


def verify_previous_source(ctx: dict[str, Any]) -> dict[str, Any]:
    expected = ctx['state'].get('previous_source_sha256')
    if not expected or not rc.HEX64_RE.match(str(expected)):
        raise SystemExit('STOP: manager.previous_source_sha256 缺失或不是合法 64-hex')
    live_manifest = Path(ctx['live']) / 'SOURCE_MANIFEST.json'
    rc._check_no_symlinks(live_manifest, 'live SOURCE_MANIFEST')
    if not live_manifest.is_file():
        raise SystemExit(f'STOP: 旧 live SOURCE_MANIFEST 不存在: {live_manifest}')
    actual = rc._sha256_file(live_manifest)
    if actual != expected:
        raise SystemExit(
            f'STOP: 旧 live SOURCE_MANIFEST != manager.previous_source_sha256: expected {expected}, got {actual}'
        )
    return {'path': str(live_manifest), 'expected': expected, 'actual': actual}


def _monitor_root(repo: Path, post_paths: list[str]) -> str:
    repo_resolved = repo.resolve()
    rels = []
    for p in post_paths:
        rp = Path(p).resolve()
        if not rp.is_relative_to(repo_resolved):
            raise SystemExit(f'STOP: post_file 不在 repo 内: {p}')
        rels.append(rp.relative_to(repo_resolved).as_posix())
    common = os.path.commonpath(rels)
    if not common or common == '.':
        raise SystemExit('STOP: 无法从 post_files 派生监看同源根目录')
    return common


def verify_monitor_authorization(ctx: dict[str, Any]) -> dict[str, Any]:
    params = ctx['parameters']
    record = ctx['record']
    repo = Path(ctx['repo'])
    base = ctx['assigned']['base']

    post_files = params.get('post_files')
    syntax = record.get('syntax')
    if not isinstance(post_files, list) or not post_files:
        raise SystemExit('STOP: params.post_files 缺失（75A 三文件授权合同）')
    if not isinstance(syntax, list) or not syntax:
        raise SystemExit('STOP: record.syntax 缺失（pointer syntax 哈希）')

    syntax_by_path: dict[str, str] = {}
    for item in syntax:
        if not isinstance(item, dict) or not item.get('path') or not item.get('sha256'):
            raise SystemExit('STOP: record.syntax 条目缺 path/sha256')
        syntax_by_path[str(Path(item['path']))] = item['sha256']

    post_paths = [str(Path(f)) for f in post_files]
    if len(set(post_paths)) != len(post_paths):
        raise SystemExit('STOP: params.post_files 含重复路径')
    if set(post_paths) != set(syntax_by_path):
        raise SystemExit('STOP: params.post_files 与 record.syntax 文件集合不一致（授权范围漂移）')

    # Whole-monitor same-source set: working tree == BASE for EVERY tracked monitor
    # source file (bytecode artifacts ignored), not only the three authorized names.
    monitor_root = _monitor_root(repo, post_paths)
    same = _monitor_same_source(repo, base, monitor_root)
    if not same.get('ok'):
        raise SystemExit(f'STOP: 监看同源集合与指派 BASE 不符（不止三文件）: {same}')

    authorized = []
    for raw in post_files:
        p = Path(raw)
        rc._check_no_symlinks(p, 'post_file')
        if not p.is_file():
            raise SystemExit(f'STOP: post_file 不存在: {p}')
        working_tree = rc._sha256_file(p)
        rec_sha = syntax_by_path[str(p)]
        if working_tree != rec_sha:
            raise SystemExit(f'STOP: post_file 工作树 hash != record.syntax: {p}')
        rel = p.resolve().relative_to(repo.resolve()).as_posix()
        try:
            blob = subprocess.check_output(
                ['git', '-C', str(repo), 'cat-file', '-p', f'{base}:{rel}'],
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as err:
            raise SystemExit(
                f'STOP: 读取 post_file Git blob 失败 {rel}: {err.stderr.decode("utf-8", "replace")}'
            ) from err
        git_blob = hashlib.sha256(blob).hexdigest()
        if git_blob != rec_sha:
            raise SystemExit(f'STOP: post_file Git blob hash != record.syntax: {p}')
        authorized.append({
            'path': str(p), 'git_rel': rel, 'sha256': rec_sha,
            'working_tree_sha256': working_tree, 'git_blob_sha256': git_blob,
        })
    return {'monitor_root': monitor_root, 'same_source': same, 'authorized_files': authorized}


def verify_record_diff(ctx: dict[str, Any]) -> dict[str, Any]:
    record = ctx['record']
    assigned = ctx['assigned']
    spec = ctx['parameters']['diff']
    repo = Path(ctx['repo'])

    d = record.get('diff')
    if not isinstance(d, dict):
        raise SystemExit('STOP: record.diff 缺失（唯一本轮 diff 权威）')
    diff_path = Path(d.get('diff_path', ''))
    rc._check_no_symlinks(diff_path, 'record.diff.diff_path')
    if not diff_path.is_file():
        raise SystemExit(f'STOP: record.diff.diff_path 不存在: {diff_path}')
    expected = d.get('diff_sha256')
    if not expected or not rc.HEX64_RE.match(str(expected)):
        raise SystemExit('STOP: record.diff.diff_sha256 缺失或不是合法 64-hex')
    actual = rc._sha256_file(diff_path)
    if actual != expected:
        raise SystemExit(f'STOP: record.diff 文件 hash != diff_sha256: expected {expected}, got {actual}')
    if d.get('base') != assigned['base'] or d.get('diff_base') != assigned['diff_base']:
        raise SystemExit('STOP: record.diff BASE/diff_base 与指派不符')

    argv = ['git', '-C', str(repo), 'diff', assigned['diff_base'], assigned['base'], '--', *spec['paths']]
    for ex in spec.get('exclude', []):
        argv.append(f':(exclude){ex}')
    try:
        git_bytes = subprocess.check_output(argv, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as err:
        raise SystemExit(
            f'STOP: 核对 record.diff 的实际 git diff 失败: {err.stderr.decode("utf-8", "replace")}'
        ) from err
    git_sha = hashlib.sha256(git_bytes).hexdigest()
    if git_sha != expected:
        raise SystemExit(
            f'STOP: record.diff 与 params scope 的实际 git diff 不符（scope 漂移）: record {expected}, git {git_sha}'
        )
    return {
        'diff_path': str(diff_path), 'diff_sha256': expected,
        'base': assigned['base'], 'diff_base': assigned['diff_base'],
        'git_argv': argv, 'git_diff_sha256': git_sha,
    }


def cold_start_prewrite_contract(ctx: dict[str, Any]) -> dict[str, Any]:
    """Read-only cold-start contract run before ANY prepare write/marker."""
    previous = verify_previous_source(ctx)
    monitor = verify_monitor_authorization(ctx)
    diff = verify_record_diff(ctx)
    roots = derive_export_roots(ctx['parameters']['diff']['paths'])
    return {
        'previous_source': previous,
        'monitor_authorization': monitor,
        'diff_authority': diff,
        'export_roots': roots,
    }