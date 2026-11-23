"""Read-only lookup of uniquely owned agent workspace by marker node_id."""
import json
import os
import stat
from pathlib import Path


def _regular_bytes(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size>1024*1024:
            raise ValueError('invalid workspace marker/card')
        return stream.read()


def workspace_for_node(workspace_root,node_id,*,required=True):
    root=Path(workspace_root)
    if root.is_symlink() or not root.is_dir():raise ValueError('invalid workspace root')
    root=root.resolve(strict=True);matches=[]
    for child in root.iterdir():
        if child.is_symlink():raise ValueError('symlinked workspace directory')
        if not child.is_dir():continue
        marker=child/'.xiaoba-agent-workspace.json'
        if marker.is_symlink():raise ValueError('symlinked workspace marker')
        if not marker.exists():continue
        try:data=json.loads(_regular_bytes(marker))
        except (json.JSONDecodeError,FileNotFoundError):
            if required:raise
            continue # marker creation can be observed between exclusive open and write
        if not isinstance(data,dict) or data.get('schema')!='xiaoba-agent-workspace-v1':
            if required:raise ValueError('invalid workspace marker schema')
            continue
        if data.get('node_id')==node_id:
            if child.resolve(strict=True).parent!=root:raise ValueError('workspace outside root')
            matches.append(child)
    if len(matches)>1 or (required and not matches):raise ValueError('workspace node ownership missing or ambiguous: '+node_id)
    return matches[0] if matches else None


def task_material_dirs(root,task_id):
    records=Path(root)/'任务记录'
    if records.is_symlink():raise ValueError('symlinked task records')
    if not records.is_dir():return []
    result=[]
    for record in sorted(records.glob('work-*')):
        if record.is_symlink():raise ValueError('symlinked task record')
        if not record.is_dir():continue
        card=record/'任务卡.json'
        if card.is_symlink():raise ValueError('symlinked task card')
        if not card.is_file():continue
        data=json.loads(_regular_bytes(card))
        if data.get('task_id')!=task_id or data.get('step_code')!='REVIEW-01':continue
        material=record/'材料'
        if material.is_symlink():raise ValueError('symlinked materials directory')
        if material.is_dir():result.append(material)
    if len(result)>1:raise ValueError('ambiguous task material directories')
    return result
