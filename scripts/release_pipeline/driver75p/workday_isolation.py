"""Read-only real-workspace guard and strict configuration containment checks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ASSISTANT = Path('/Users/suzhizhou/Documents/私人助理')


def real_views() -> dict[str, Any]:
    workspace = ASSISTANT / '工作区'
    books = {p.name for p in workspace.glob('*-小八台账')}
    catalog = ASSISTANT / '工具/每日台账索引总目录'
    files: dict[str, str] = {}
    if catalog.exists():
        assert not catalog.is_symlink()
        for p in sorted(catalog.rglob('*')):
            assert not p.is_symlink(), str(p)
            if p.is_file():
                files[str(p.relative_to(catalog))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return {'books': sorted(books), 'catalog_exists': catalog.exists(), 'catalog_files': files}


def unchanged(before: dict[str, Any]) -> dict[str, Any]:
    after = real_views()
    return {
        'ok': after == before,
        'before': before,
        'after': after,
        'new_books': sorted(set(after['books']) - set(before['books'])),
        'read_only': True,
    }


def assert_config(path: Path | str, scene: Path | str) -> dict[str, Any]:
    path = Path(path).resolve()
    scene = Path(scene).resolve()
    assert path.is_relative_to(scene), f'STOP: config is outside isolated site: {path}'
    value = json.loads(path.read_text(encoding='utf-8'))
    vault = Path(value['memory']['obsidian_vault']).resolve()
    assert vault.is_relative_to(scene) and vault.parent.is_relative_to(scene), 'STOP: ledger root escapes isolated site'
    assert not vault.parent.is_relative_to(ASSISTANT / '工作区') or vault.parent.is_relative_to(scene)
    assert vault.parent != ASSISTANT, 'STOP: ledger would write real workspace'
    for field in ('runtime_dir', 'project_dir'):
        assert Path(value[field]).resolve().is_relative_to(scene), field
    for section in ('memory', 'paths'):
        if section in value:
            assert Path(value[section]['obsidian_vault']).resolve() == vault
    return {
        'ok': True,
        'config': str(path),
        'scene': str(scene),
        'ledger_root': str(vault.parent),
        'vault': str(vault),
    }
