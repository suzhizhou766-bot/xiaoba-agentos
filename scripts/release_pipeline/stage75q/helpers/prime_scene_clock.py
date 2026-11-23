"""Seed the idle clock in the scene baseline, before fault-rollback DB comparisons.
The independent post-switch card52b scratch case still verifies a first idle enable.
"""

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
s=json.loads((O/'SCENE.json').read_text());root=Path(s['root']).resolve();assert root.is_relative_to(R/'rehearsal')
sys.path.insert(0,str(_BIND.C/'src'))
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.orchestrator import Orchestrator
cfg=AppConfig.load(Path(s['config']));assert cfg.database_path.resolve().is_relative_to(root)
store=EventStore(cfg.database_path)
with store.read_only() as db:
 assert not db.execute("SELECT id FROM tasks WHERE status NOT IN ('completed','failed') OR lease_until IS NOT NULL").fetchall()
 before=db.execute("SELECT count(*) FROM events WHERE event_type='memory.formal_enabled'").fetchone()[0]
result=Orchestrator(cfg,store).run_once(preflight_when_idle=False);assert result['status']=='idle',result
with store.read_only() as db:
 after=db.execute("SELECT count(*) FROM events WHERE event_type='memory.formal_enabled'").fetchone()[0]
 assert after==1
x={'ok':True,'worker_idle_result':result,'enabled_before':before,'enabled_after':after,'purpose':'baseline includes intentional one-time idle enable, so full DB rollback comparison remains strict'}
(O/'23-scene-clock-baseline.json').write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n');print(json.dumps(x,ensure_ascii=False))
