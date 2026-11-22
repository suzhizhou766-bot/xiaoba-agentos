"""几组测试共用的夹具：一份最小配置，数事件、数候选的查询，跑一次命令行并取回 JSON，算文件哈希。

文件名不以 test_ 开头，不会被当成测试收集。
"""
import hashlib
import io
import json
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

from xiaoba_agentos.cli import main
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.store import EventStore


def _raw_config(root: Path) -> dict:
    root = root.resolve()
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
    root = root.resolve()
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


def _admit_authored_candidate(store: EventStore, candidate: dict, *, score: int = 8) -> dict:
    """Pass a host-authored fixture through two attested Agent Pool receipts."""
    ingress = Path(__file__).resolve().parents[2] / "review-ingress"
    if str(ingress) not in sys.path:
        sys.path.insert(0, str(ingress))
    from xiaoba_review.agent_pool import AgentPool
    from xiaoba_review.backends import BackendRegistry
    from xiaoba_review.store import ReviewStore

    staged_path = str(Path(candidate["staged_path"]).resolve(strict=True))
    outbox = Path(staged_path).parent
    review_store = ReviewStore(store, outbox)
    pool = AgentPool(review_store, BackendRegistry(review_store))
    suffix = candidate["task_id"][:8] + candidate["evidence_sha256"][:8]
    source_ids = (f"TEST-value-review-{suffix}", f"TEST-value-both-{suffix}")
    for node_id, kind in zip(source_ids, ("review", "both")):
        source = pool.add({
            "node_id": node_id,
            "name": node_id,
            "agent": node_id,
            "kind": kind,
            "interface": "cli",
            "entry": "/TEST/" + node_id,
            "model": "TEST model " + node_id,
            "agent_type": "other",
        })
        pool.change(node_id, source["node"]["revision"], "toggle", True)
    request = review_store.create(
        candidate["task_id"], source_ids[0], source_ids[1],
        "TEST value review for a host-authored fixture",
        [{"ref": staged_path, "sha256": candidate["evidence_sha256"]}],
        dsh_channel="external_handoff", peer_channel="external_handoff",
        review_type="new_experience", value_review=True,
    )
    for assignment in request["assignments"]:
        card = assignment["card"]
        receipt = {
            key: card[key] for key in (
                "review_request_id", "assignment_id", "reviewer_role", "plan_revision",
                "reviewer_id", "source_ref", "candidate_sha256", "input_digest", "card_digest",
            )
        }
        receipt.update({
            "receipt_id": f"TEST-value-receipt-{assignment['id']}",
            "verdict": "PASS",
            "findings": "TEST synthetic value-review receipt",
            "evidence_refs": [item["ref"] for item in card["evidence_refs"]],
            "started_at": time.time() - 1,
            "finished_at": time.time(),
            "review_type": "new_experience",
            "score": score,
            "applicability": "TEST fixture only",
        })
        accepted = review_store.receive(receipt)
        review_store.attest(accepted["id"], accepted["digest"], "TEST source checked")
    admitted = store.get_memory_candidate(candidate["task_id"])
    if admitted is None or admitted["status"] != "pending_delivery_review":
        raise AssertionError("TEST double-receipt scoring did not admit the authored candidate")
    return admitted


def _stage_candidate_with_value_review(
    store: EventStore, task_id: str, staged_path: Path, evidence_sha256: str,
    *, source_task_ids=None, score: int = 8,
) -> dict:
    """Exercise the version-bound queue before exposing a fixture to human review."""
    from xiaoba_agentos.value_queue import defer_tx

    path = Path(staged_path).resolve(strict=True)
    with store.connect() as conn:
        defer_tx(conn, task_id, {
            "version": 1,
            "staged_path": str(path),
            "candidate_sha256": evidence_sha256,
            "source_task_ids": list(source_task_ids or []),
            "reason": "test_candidate_revision",
        })
    return _admit_authored_candidate(store, {
        "task_id": task_id,
        "staged_path": str(path),
        "evidence_sha256": evidence_sha256,
    }, score=score)
