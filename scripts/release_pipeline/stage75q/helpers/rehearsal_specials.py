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
import json
import os
import sys
from pathlib import Path
# Pending v1614-r7 must never consume historical evidence or run before new-BASE preparation.
assert (_BIND.work / 'BASE_READY.json').is_file(), 'STOP: BASE_READY missing'

ROOT = _BIND.root
os.chdir(ROOT)
REPORT_DIR = _BIND.O
SCENE = json.loads((REPORT_DIR / "SCENE.json").read_text())
PKG = Path(SCENE["runtime"]) / f".runtime/releases/{VERSION}"
sys.path.insert(0, str(PKG / "agentos/src"))
from xiaoba_agentos.store import EventStore

database = Path(SCENE["runtime"]) / ".runtime/agentos.sqlite3"
store = EventStore(database)
store.initialize()

def event_types(task_id):
    with store.read_only() as conn:
        return [row[0] for row in conn.execute(
            "SELECT event_type FROM events WHERE task_id=? ORDER BY sequence", (task_id,)
        )]

abandon_id = store.submit(
    "卡42现场 task-abandon 专项冻结任务（不调用模型）",
    max_attempts=1,
    snapshot={"prompt": "卡42现场 task-abandon 专项冻结任务（不调用模型）", "rehearsal": True},
    idempotency_key="card63-v1614-r7-task-abandon",
    provenance={"kind": "rehearsal_fixture", "status": "pending"},
)
frozen = store.freeze_task(
    abandon_id,
    failure_class="rehearsal_fixture_failure",
    failures=[{"agent": "fixture", "error": "现场故障注入"}],
)
abandoned = store.abandon_task(abandon_id, frozen["freeze_hash"])
abandon_task = store.get_task(abandon_id)
abandon_events = event_types(abandon_id)
assert abandoned["status"] == "failed" and abandoned["abandoned"] is True
assert abandon_task["status"] == "failed"
assert "task.abandoned" in abandon_events

cancel_id = store.submit(
    "卡42现场 task-cancel 专项排队任务（不调用模型）",
    max_attempts=1,
    snapshot={"prompt": "卡42现场 task-cancel 专项排队任务（不调用模型）", "rehearsal": True},
    idempotency_key="card63-v1614-r7-task-cancel",
    provenance={"kind": "rehearsal_fixture", "status": "pending"},
)
cancelled = store.cancel_task(cancel_id)
cancel_task = store.get_task(cancel_id)
cancel_events = event_types(cancel_id)
assert cancelled["status"] == "failed" and cancelled["cancelled"] is True
assert cancel_task["status"] == "failed"
assert "task.cancelled" in cancel_events

result = {
    "ok": True,
    "scene_root": SCENE["root"],
    "database": str(database),
    "task_abandon": {
        "task_id": abandon_id,
        "freeze_hash": frozen["freeze_hash"],
        "result": abandoned,
        "final_status": abandon_task["status"],
        "events": abandon_events,
    },
    "task_cancel": {
        "task_id": cancel_id,
        "result": cancelled,
        "final_status": cancel_task["status"],
        "events": cancel_events,
    },
}
(REPORT_DIR / "33-task-abandon-cancel.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(result, ensure_ascii=False))
