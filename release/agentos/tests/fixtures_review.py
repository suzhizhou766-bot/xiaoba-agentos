"""几组测试共用的夹具：一份最小配置，数事件、数候选的查询，跑一次命令行并取回 JSON，算文件哈希。

文件名不以 test_ 开头，不会被当成测试收集。
"""
import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from xiaoba_agentos.cli import main
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.store import EventStore


def _raw_config(root: Path) -> dict:
    return {
        "schema_version": 1,
        "runtime_dir": str(root / "runtime"),
        "project_dir": str(root),
        "worker": {
            "poll_seconds": 1, "lease_seconds": 60, "max_attempts": 3,
            "retry_base_seconds": 0, "retry_max_seconds": 0, "parallelism": 2,
            "minimum_successful_agents": 2, "max_agent_invocations_per_task": 6,
        },
        "memory": {
            "knowlp_enabled": False, "obsidian_vault": str(root / "vault"),
            "obsidian_subdir": "AgentOS", "publish_enabled": False,
        },
        "integrations": {},
        "agents": [
            {"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1,
             "template": "任务 {prompt} 的方案 A"},
            {"name": "b", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1,
             "template": "任务 {prompt} 的方案 B，包含验证"},
        ],
        "evolution": {"minimum_samples": 1, "promotion_margin": 0.05, "latency_penalty": 0.01},
    }


def _config(root: Path, name: str = "config.json") -> AppConfig:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_raw_config(root)), encoding="utf-8")
    return AppConfig.load(path)


def _event_count(store: EventStore, task_id: str, event_type: str) -> int:
    with store.read_only() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM events WHERE task_id=? AND event_type=?", (task_id, event_type)
        ).fetchone()[0]


def _candidate_count(store: EventStore, task_id: str) -> int:
    with store.read_only() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM memory_candidates WHERE task_id=?", (task_id,)
        ).fetchone()[0]


def _run_cli(argv):
    output = io.StringIO()
    with redirect_stdout(output):
        code = main(argv)
    return code, json.loads(output.getvalue())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
