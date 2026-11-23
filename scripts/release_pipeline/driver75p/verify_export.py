"""Verify that exported source files exactly match git blob objects at assigned BASE.

逐 blob 核对：计算 sha1("blob " + len + "\\0" + content) 与 git ls-tree 记录的 object id 对比。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def verify_export_blobs(
    repo_path: Path | str,
    base_commit: str,
    source_dir: Path | str,
    roots: list[str] | None = None,
) -> dict[str, Any]:
    repo = Path(repo_path).resolve()
    source = Path(source_dir).resolve()
    if roots is None:
        roots = ['development', 'monitor']

    raw = subprocess.check_output(
        ['git', '-C', str(repo), 'ls-tree', '-rz', base_commit, '--', *roots]
    )
    expected: dict[str, str] = {}
    bad: list[str] = []

    for item in raw.split(b'\0'):
        if not item:
            continue
        meta, path_bytes = item.split(b'\t', 1)
        mode, kind, oid = meta.split()
        name = path_bytes.decode('utf-8')
        expected[name] = oid.decode('ascii')
        p = source / name
        if mode == b'120000' or not p.is_file() or p.is_symlink():
            bad.append(name)
            continue
        data = p.read_bytes()
        blob_sha = hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()
        if blob_sha != oid.decode('ascii'):
            bad.append(name)

    actual: set[str] = set()
    for root in roots:
        root_path = source / root
        if root_path.is_dir():
            for p in root_path.rglob('*'):
                if p.is_file() and not p.is_symlink():
                    actual.add(str(p.relative_to(source)))

    result = {
        'base': base_commit,
        'files': len(expected),
        'changed': sorted(bad),
        'extra': sorted(actual - expected.keys()),
        'missing': sorted(expected.keys() - actual),
    }
    result['ok'] = not any(result[k] for k in ('changed', 'extra', 'missing'))
    return result


def main() -> None:
    if len(sys.argv) >= 4:
        repo = sys.argv[1]
        base = sys.argv[2]
        source = sys.argv[3]
    else:
        # Fallback to shared context
        import release_context as rc
        ctx = rc.load_context()
        repo = ctx['repo']
        base = ctx['assigned']['base']
        source = ctx['source']

    res = verify_export_blobs(repo, base, source)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    raise SystemExit(0 if res['ok'] else 1)


if __name__ == '__main__':
    main()
