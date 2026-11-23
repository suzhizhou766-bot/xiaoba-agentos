"""Read-only real-workspace guard and strict configuration containment checks."""
import hashlib
import json
from pathlib import Path

ASSISTANT=Path('/Users/suzhizhou/Documents/私人助理')


def real_views():
    workspace=ASSISTANT/'工作区'
    books={p.name for p in workspace.glob('*-小八台账')}
    book_files={}
    for name in sorted(books):
        book=workspace/name
        assert book.is_dir() and not book.is_symlink(),('STOP: invalid real ledger directory',str(book))
        for p in sorted(book.rglob('*')):
            assert not p.is_symlink(),('STOP: real ledger contains symlink',str(p))
            if p.is_file():book_files[str(p.relative_to(workspace))]=hashlib.sha256(p.read_bytes()).hexdigest()
    catalog=ASSISTANT/'工具/每日台账索引总目录'
    files={}
    if catalog.exists():
        assert not catalog.is_symlink()
        for p in sorted(catalog.rglob('*')):
            assert not p.is_symlink(),str(p)
            if p.is_file():files[str(p.relative_to(catalog))]=hashlib.sha256(p.read_bytes()).hexdigest()
    return {'books':sorted(books),'book_files':book_files,
            'catalog_exists':catalog.exists(),'catalog_files':files}


def unchanged(before):
    after=real_views()
    return {'ok':after==before,'before':before,'after':after,
            'new_books':sorted(set(after['books'])-set(before['books'])),'read_only':True}


def assert_config(path,scene):
    path=Path(path).resolve();scene=Path(scene).resolve()
    assert path.is_relative_to(scene),'STOP: config is outside isolated site'
    value=json.loads(path.read_text())
    assert isinstance(value.get('memory'),dict),'STOP: isolated config requires memory section'
    assert value['memory'].get('obsidian_vault'),'STOP: isolated memory requires obsidian_vault'
    vault=Path(value['memory']['obsidian_vault']).resolve()
    assert vault.is_relative_to(scene) and vault.parent.is_relative_to(scene),'STOP: ledger root escapes isolated site'
    assert not vault.parent.is_relative_to(ASSISTANT/'工作区') or vault.parent.is_relative_to(scene)
    assert vault.parent!=ASSISTANT,'STOP: ledger would write real workspace'
    for field in ('runtime_dir','project_dir'):
        assert isinstance(value.get(field),str) and value[field],('STOP: isolated config requires '+field)
        assert Path(value[field]).resolve().is_relative_to(scene),('STOP: '+field+' escapes isolated site')
    for section in ('memory','paths'):
        if section in value:
            assert isinstance(value[section],dict) and value[section].get('obsidian_vault'),('STOP: isolated '+section+' requires obsidian_vault')
            assert Path(value[section]['obsidian_vault']).resolve()==vault,('STOP: isolated '+section+' vault disagrees with memory')
    return {'ok':True,'config':str(path),'scene':str(scene),'ledger_root':str(vault.parent),'vault':str(vault)}
