"""卡49 第4步前置：用用户授权的历史 v1.6.7 代码在现场 Agent 池里预放一个 workbuddy 类型节点。

只写演练现场自己的数据库；切换后由 specials_v169.py 核对它被标出、测试返回 engine_retired、数据不变。
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
import os
from pathlib import Path
# Pending v1614-r7 must never consume historical evidence or run before new-BASE preparation.
assert (_BIND.work / 'BASE_READY.json').is_file(), 'STOP: BASE_READY missing'

os.chdir(R)
import hashlib
import json
import logging
import sys
from pathlib import Path

ROOT = _BIND.root
REPORT_DIR = _BIND.O
SCENE = json.loads((REPORT_DIR / "SCENE.json").read_text())
OLD = Path("/Users/suzhizhou/Documents/小八AgentOS/.runtime/releases/v1.6.7")
assert OLD.is_dir()
assert Path(SCENE["previous_package"]).name == Path(_BIND.previous_package).name
sys.path[:0] = [str(OLD / "agentos/src"), str(_BIND.source / "monitor")]

from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.store import EventStore
from xiaoba_review.store import ReviewStore
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.resource_stdio import json_stdio_factory
from xiaoba_review.agent_pool import AgentPool
from monitor_core import config as monitor_config
from monitor_core.web import Context

NODE_ID = "card49-sp-workbuddy"
database = Path(SCENE["runtime"]) / ".runtime/agentos.sqlite3"
config_path = Path(SCENE["config"])
workspaces = Path(SCENE["root"]) / "agent-workspaces"

# Legacy code is a fixture writer only. Product assertions use the new package after switching.
assert Path(SCENE["root"]).resolve().is_relative_to(ROOT/"rehearsal")
assert database.resolve().is_relative_to(Path(SCENE["root"]).resolve())

cfg = AppConfig.load(config_path)
native = EventStore(database)
# The scene DB is already initialized; do not run legacy schema initialization.
store = ReviewStore(native, cfg.runtime_dir / "memory-outbox", workspaces)
backends = BackendRegistry(store, resource_factory=lambda source: json_stdio_factory(source, store.workspace_root))
ctx = Context(monitor_config.load(cfg.runtime_dir / "unused-monitor.json", {"port": 0}), logging.getLogger("card49-prework"), 0)
pool = AgentPool(store, backends)

report = {
    "purpose": "卡45 演练前置：仅用历史v1.6.7代码预放旧节点（实际切换v1.6.13到v1.6.14）一个 workbuddy 类型节点，切换后核对新版本的处置",
    "old_package": str(OLD),
    "old_source_manifest_sha256": hashlib.sha256((OLD / "agentos/SOURCE_MANIFEST.json").read_bytes()).hexdigest(),
    "node_id": NODE_ID,
    "ok": False,
}
try:
    node = {
        "node_id": NODE_ID, "name": "卡49 现场旧 WorkBuddy 节点", "agent": "WORKBUDDY", "kind": "review",
        "interface": "cli", "entry": "/TEST/bin/workbuddy", "model": "TEST-legacy-workbuddy-model",
        "agent_type": "workbuddy", "no_progress_seconds": 1800,
    }
    report["submitted_node"] = node
    source = pool.add(node)
    report["added"] = {"reviewer_id": source["reviewer_id"], "kind": source["kind"], "node": source["node"]}
    revision = source["node"]["revision"]
    toggled = pool.change(NODE_ID, revision, "toggle", True)
    report["enabled"] = {"ok": True, "revision": toggled["node"]["revision"]}
    with native.read_only() as conn:
        row = conn.execute("SELECT data FROM ri_sources WHERE id=?", (NODE_ID,)).fetchone()
        events = [dict(item) for item in conn.execute(
            "SELECT event_type,payload_json FROM events WHERE event_type LIKE 'agent_pool.%' ORDER BY sequence")]
    data = json.loads(row[0])
    report["stored"] = {
        "raw_json": row[0], "sha256": hashlib.sha256(row[0].encode()).hexdigest(),
        "revision": data["node"]["revision"], "enabled": data["node"]["enabled"],
        "agent_type": data["node"].get("agent_type"), "entry": data["node"]["entry"],
        "test": data["node"].get("test"), "agent_pool_events": events,
    }
    assert data["node"]["agent_type"] == "workbuddy" and data["node"]["enabled"] is True, data
    report["ok"] = True
finally:
    backends.close()
    (REPORT_DIR / "40-prework-workbuddy-node.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: report.get(k) for k in ("ok", "node_id")}, ensure_ascii=False)
          + " stored=" + json.dumps(report.get("stored", {}), ensure_ascii=False)[:400], flush=True)
assert report["ok"] is True
