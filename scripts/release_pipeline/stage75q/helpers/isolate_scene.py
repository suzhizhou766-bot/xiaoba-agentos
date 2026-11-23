"""Disable all copied real nodes in the scene before any Worker starts."""

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
from pathlib import Path
# Pending v1614-r7 must never consume historical evidence or run before new-BASE preparation.
assert (_BIND.work / 'BASE_READY.json').is_file(), 'STOP: BASE_READY missing'
import os,sys,json
R = _BIND.root;os.chdir(R);O = _BIND.O
s=json.loads((O/'SCENE.json').read_text());root=Path(s['root']).resolve()
assert root.is_relative_to(R/'rehearsal') and s['label'].startswith('com.xiaoba.agentos.rehearsal.')
sys.path[:0]=[str(Path(s['previous_package'])/'agentos/src')]
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.store import EventStore
from xiaoba_review.store import ReviewStore
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.resource_stdio import json_stdio_factory
from xiaoba_review.agent_pool import AgentPool
cfg=AppConfig.load(Path(s['config']));assert cfg.database_path.resolve().is_relative_to(root)
n=EventStore(cfg.database_path);store=ReviewStore(n,cfg.runtime_dir/'memory-outbox',root/'agent-workspaces')
b=BackendRegistry(store,resource_factory=lambda source:json_stdio_factory(source,store.workspace_root));pool=AgentPool(store,b);disabled=[]
try:
 for node in pool.state()['nodes']:
  if node.get('enabled'):
   pool.change(node['node_id'],node['revision'],'toggle',False);disabled.append(node['node_id'])
 assert not any(node.get('enabled') for node in pool.state()['nodes'])
finally:b.close()
x={'ok':True,'disabled_copied_nodes':disabled,'purpose':'no real model calls in rehearsal','root':str(root)}
(O/'22-scene-isolation.json').write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n');print(json.dumps(x,ensure_ascii=False))
