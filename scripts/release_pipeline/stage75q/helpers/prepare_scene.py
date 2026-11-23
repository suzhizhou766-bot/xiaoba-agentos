"""v1.6.7 rehearsal copy under Documents (so the signed Runner + 3.14 worker is tested against Documents privacy rules). Never a production recovery source."""

# --- rebound by stage75q ---
"""stage75q helper — rebound from mature v1614-r7 source; paths via STAGE75Q_BIND_JSON."""
from pathlib import Path
import os, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bind import load_bind
_BIND = load_bind()
_BIND.require_ready()
R = _BIND.root
O = _BIND.O
A = _BIND.A
W = _BIND.W
C = _BIND.C
PKG = _BIND.PKG
VERSION = _BIND.version
LIVE = _BIND.live
P = LIVE
os.chdir(R)
# --- end bind header; original body follows with path overrides ---
import os
from pathlib import Path
# Pending v1614-r7 must never consume historical evidence or run before new-BASE preparation.
assert (_BIND.work / 'BASE_READY.json').is_file(), 'STOP: BASE_READY missing'
os.chdir(R)
import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = _BIND.root
C = _BIND.C
P = _BIND.live
from leaf_contracts import (
    load_scene_drain_task_ids,
    require_acceptance_passed,
    resolve_scene_config_template,
)
V=Path('/Users/suzhizhou/Documents/私人助理/第二大脑')
state=json.loads((A/'STATE.json').read_text())
require_acceptance_passed(state)
assert state.get('verified_package')
spec=importlib.util.spec_from_file_location('switch_scene',C/'scripts/switch_release.py')
sw=importlib.util.module_from_spec(spec);sys.modules[spec.name]=sw;spec.loader.exec_module(sw)
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
def dump(p,obj):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+chr(10))
(ROOT/'rehearsal').mkdir(exist_ok=True)
tmp=Path(tempfile.mkdtemp(prefix=str(_BIND.round)+'-scene-',dir=ROOT/'rehearsal')).resolve()
runtime=tmp/'runtime';runtime.mkdir()
(tmp/'agent-workspaces').mkdir()
index=json.loads((P/'.runtime/releases/index.json').read_text())
old_version=index['current_release'];assert old_version==Path(_BIND.previous_package).name, (old_version, Path(_BIND.previous_package).name)
entry=next(r for r in index['releases'] if r['version']==old_version)
old=P/'.runtime/releases'/entry['path'];sw.verify_package(old);sw.verify_source(P)
previous=runtime/'.runtime/releases'/old_version
shutil.copytree(old,previous)
# 线上已执行过的配置步骤记录（v1.6.1 的三条内容路径）随现场带上，它引用的 v1.6.1 包也一并带上，切换脚本要逐条核对。
history=json.loads((P/'.runtime/release-steps.json').read_text())
step_releases=sorted({r['release'] for r in history['steps'].values()})
assert 'v1.6.1' in step_releases,step_releases
scene_entries=[]
for version in step_releases:
    e=next(r for r in index['releases'] if r['version']==version)
    pkg=P/'.runtime/releases'/e['path'];sw.verify_package(pkg)
    if version != old_version:
        shutil.copytree(pkg,runtime/'.runtime/releases'/e['path']);scene_entries.append(e)
shutil.copy2(P/'.runtime/release-steps.json',runtime/'.runtime/release-steps.json')
assert sha(P/'.runtime/release-steps.json')==sha(runtime/'.runtime/release-steps.json')
for name in (*sw.SOURCE_DIRS,*sw.SOURCE_FILES,'SOURCE_MANIFEST.json'):
    source=P/name;dest=runtime/name
    if not source.exists():continue  # v1.6.1 起 skills、knowledge 已不在运行目录
    if source.is_dir():shutil.copytree(source,dest,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.DS_Store'))
    else:shutil.copy2(source,dest)
vault=tmp/'assistant/第二大脑';vault.mkdir(parents=True)
vault_files={}
for p in V.rglob('*.md'):
    assert not p.is_symlink(),p
    rel=p.relative_to(V);dest=vault/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest)
    vault_files[str(rel)]=sha(p)
assert all(sha(V/p)==h for p,h in vault_files.items()),'Vault changed during copy'
db=runtime/'.runtime/agentos.sqlite3'
src=sqlite3.connect((P/'.runtime/agentos.sqlite3').as_uri()+'?mode=ro',uri=True)
dst=sqlite3.connect(db)
# Consistent rehearsal clone; retained only as an active test fixture, not rollback media.
src.backup(dst);src.close()
# Drain list from current record/work only — never hardcoded historical round path; never forge.
drain=load_scene_drain_task_ids(_BIND)
unfinished=dst.execute("SELECT id,status,attempts,lease_until FROM tasks WHERE status NOT IN ('completed','failed') OR lease_until IS NOT NULL").fetchall()
if unfinished and not drain:
    raise RuntimeError('STOP: live queue not idle and scene_drain.json missing/empty under current work; refuse forged drain')
assert {r[0] for r in unfinished}<=set(drain),'Live queue has unfinished tasks not named in scene_drain.json'
assert all(r[1]=='pending' and r[2]==0 and r[3] is None for r in unfinished),unfinished
dst.close()
if unfinished:
    code=('import sys;from pathlib import Path;from xiaoba_agentos.store import EventStore;s=EventStore(Path(sys.argv[1]))\n'
          'for t in sys.argv[2:]:s.fail(t,"演练现场：按切换前线上队列已清空演练（scene_drain.json），线上未改")')
    import subprocess
    subprocess.run([sys.executable,'-B','-c',code,str(db),*[r[0] for r in unfinished]],check=True,
                   env={**os.environ,'PYTHONPATH':str(previous/'agentos/src'),'PYTHONDONTWRITEBYTECODE':'1'})
dst=sqlite3.connect(db)
pending=dst.execute("SELECT COUNT(*) FROM tasks WHERE status NOT IN ('completed','failed') OR lease_until IS NOT NULL").fetchone()[0]
assert pending==0,'Live queue must be idle before starting a copied worker'
scene_drained=[r[0] for r in unfinished]
rows=dst.execute('SELECT task_id,staged_path,published_path FROM memory_candidates').fetchall()
remapped=[]
for task,staged,published in rows:
    updates={}
    for column,value in [('staged_path',staged),('published_path',published)]:
        if not value:continue
        p=Path(value)
        if p.is_relative_to(V):target=vault/p.relative_to(V)
        elif p.is_relative_to(P):target=runtime/p.relative_to(P)
        else:raise RuntimeError('Unexpected candidate file outside copied roots: '+str(p))
        if p.is_file() and not target.exists():
            assert not p.is_symlink(),p
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
        updates[column]=str(target)
    if updates:
        dst.execute('UPDATE memory_candidates SET '+','.join(k+'=?' for k in updates)+' WHERE task_id=?',list(updates.values())+[task])
        remapped.append(task)
dst.commit();dst.close()
cfg_template=resolve_scene_config_template(_BIND)
raw=json.loads(cfg_template.read_text())
raw['project_dir']=str(runtime);raw['runtime_dir']=str(runtime/'.runtime')
raw['release'].update(index_path=str(runtime/'.runtime/releases/index.json'),expected_version=old_version,acceptance_mode=False)
graph=runtime/'.runtime/knowlp'
for k in ('memory','paths'):
    raw[k].update(obsidian_vault=str(vault),knowlp_vault=str(vault),knowlp_graph_dir=str(graph))
raw['memory']['publish_enabled']=False
assistant=tmp/'assistant'
# v1.6.1 起三条内容配置步骤已执行：现场配置指到现场助理目录下的固定位置（与线上指到 私人助理/ 同构）。
raw['skills'].update(root_dir=str(assistant/'skills'),index_path=str(assistant/'skills/index.json'))
raw['knowledge']['index_path']=str(assistant/'系统/价值文件索引/index.json')
cfg=runtime/'.runtime/active.json';dump(cfg,raw);cfg.chmod(0o600)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from workday_isolation import assert_config
assert_config(cfg,tmp)
dump(runtime/'.runtime/releases/index.json',{'format_version':2,'current_release':old_version,'releases':scene_entries+[entry]})
entries=tuple(tmp/'entries'/n for n in ('agents','codex'))
for home in entries:
    (home/'scripts').mkdir(parents=True)
    for name in ('xiaoba','verify-release'):
        text=(previous/'interface/scripts'/name).read_text()
        text=sw.DEFAULT_LINE.sub(lambda _: 'DEFAULT_CONFIG = Path('+json.dumps(str(cfg),ensure_ascii=False)+')',text)
        p=home/'scripts'/name;p.write_text(text);p.chmod(0o755)
    shutil.copy2(previous/'interface/SKILL.md',home/'SKILL.md')
label='com.xiaoba.agentos.rehearsal.'+tmp.name.rsplit('-',1)[-1]
plist=tmp/'worker.plist'
data=plistlib.loads(Path('/Users/suzhizhou/Library/LaunchAgents/com.xiaoba.agentos.plist').read_bytes())
data.update(Label=label,WorkingDirectory=str(runtime),StandardOutPath=str(tmp/'worker.out.log'),StandardErrorPath=str(tmp/'worker.err.log'))
data['EnvironmentVariables'].update(XIAOBA_CONFIG=str(cfg),XIAOBA_PROJECT_DIR=str(runtime/'launchd/worker'),PYTHONDONTWRITEBYTECODE='1')
plist.write_bytes(plistlib.dumps(data))
# 内容已在包外：把线上 私人助理/skills 与 系统/价值文件索引 原样复制到现场助理目录，逐字节核对。
A0=Path('/Users/suzhizhou/Documents/私人助理')
shutil.copytree(A0/'skills',assistant/'skills',ignore=shutil.ignore_patterns('__pycache__','.DS_Store','*.pyc'),symlinks=True)
(assistant/'系统').mkdir(parents=True,exist_ok=True)
shutil.copytree(A0/'系统/价值文件索引',assistant/'系统/价值文件索引',ignore=shutil.ignore_patterns('__pycache__','.DS_Store','*.pyc'))
for rel in ('skills/index.json','skills/catalog.json','系统/价值文件索引/index.json','系统/价值文件索引/catalog.json'):
    assert sha(A0/rel)==sha(assistant/rel),rel
layout=sw.Layout(runtime,cfg,Path(state.get('verified_package') or state['candidate_package']),vault,graph,entries,plist,label)
graph.mkdir(parents=True)
for rel,obj in sw.build_graph(layout).items():(graph/rel).write_bytes(sw.graph_bytes(obj))
sw.verify_graph(layout)
changes=sw.vault_actions(layout,previous)
# Isolated failed-recovery fixture objects; original inodes, never copied recovery bytes.
sys.path.insert(0,str(C/'src'))
from xiaoba_agentos.store import EventStore
native=EventStore(db)
with native.connect() as conn:
    conn.execute('BEGIN IMMEDIATE')
    assert native.verify_event_chain_tx(conn)['ok']
    native._append_event_tx(conn,None,'agent_pool.workspace_bound',{'root':str(tmp/'agent-workspaces'),'purpose':'isolated rehearsal path remap; live untouched'})
    native._append_event_tx(conn,None,'workday.root_bound',{'root':str(assistant),'purpose':'isolated rehearsal clone binding; real workspace untouched'})
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos import retained
fixture_config=AppConfig.load(cfg)
retained_fixture={}
for original in [runtime/'.runtime/memory-outbox/rehearsal-evidence.json', vault/'70_复利规则/经验/rehearsal-note.md', graph/'rehearsal-index.json']:
    original.parent.mkdir(parents=True,exist_ok=True)
    original.write_bytes(b'CLAIMED_ORIGINAL_INODE')
    with original.open('ab') as held_fd:
        claim=original.with_name('.'+original.name+'.4242.rollback')
        os.rename(original,claim)
        held_fd.write(b'\\nLATER_OLD_FD_WRITER')
        held_fd.flush()
        os.fsync(held_fd.fileno())
    retained.record_retained(fixture_config,retained_path=claim,original_path=original,operation='rehearsal.failed_recovery',reason='isolated claimed-inode fixture for switch preservation',writer_pid=4242)
    retained_fixture[str(claim)]={'sha256':sha(claim),'dev':claim.stat().st_dev,'ino':claim.stat().st_ino}
info={'scene_under_documents':str(tmp).startswith('/Users/suzhizhou/Documents/'),'root':str(tmp),'runtime':str(runtime),'config':str(cfg),'vault':str(vault),'graph':str(graph),'entries':[str(x) for x in entries],
      'plist':str(plist),'label':label,'candidate_package':state['candidate_package'],'previous_package':str(previous),
      'retained_fixture_objects':retained_fixture,'retained_fixture_purpose':'Synthetic failed-recovery already-claimed inodes, with old fd appended bytes; switching must preserve them, not a real production failure claim','copied_vault_markdown_files':len(vault_files),'vault_source_hashes':vault_files,'remapped_candidate_path_rows':len(remapped),'scene_drained_live_pending_tasks':scene_drained,
      'copied_source_manifest_sha256':sha(runtime/'SOURCE_MANIFEST.json'),'production_source_manifest_sha256':sha(P/'SOURCE_MANIFEST.json'),
      'vault_merge_plan':[str(x[0].relative_to(vault)) for x in changes],
      'runner_program':data['ProgramArguments'][0],'runner_sha256':sha(Path(data['ProgramArguments'][0])),
      'launcher':'same installed signed runner and copied worker launcher; explicit isolated config',
      'isolation':'Separate launchd label, runtime, DB, vault, graph, installed entries; source unchanged; DB paths remapped only in clone; only named scene tasks settled; publication disabled; no user credentials copied.'}
dump(O/('SCENE-formal.json' if '--formal-confirmation' in sys.argv else 'SCENE.json'),info)
print(json.dumps({k:info[k] for k in ('root','label','copied_vault_markdown_files','remapped_candidate_path_rows','vault_merge_plan')},ensure_ascii=False))
