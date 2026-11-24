from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import retained as retained_objects
from .config import AgentConfig, AppConfig, ConfigError
from .bridge import UnixBridge, dispatch, _submission_result
from .health import doctor, execution_blockers
from .knowledge import KnowledgeCatalog
from .memory import (
    MemoryBridge,
    MemoryRecoveryError,
    _release_reserved_name,
    _reserve_private_name,
)
from .orchestrator import Orchestrator
from .review_flow import (
    PublicationUncertain,
    candidate_sha256 as _candidate_sha256,
    execute_review,
    publish_approved_candidate as _publish_approved_candidate,
    quarantine_pending_reuses as _quarantine_pending_reuses,
    reconcile_failed_reuses as _reconcile_failed_reuses,
    reconcile_validated_reuses as _reconcile_validated_reuses,
    reconciliation_payload as _reconciliation_payload,
)
from .routing import AGENTOS_MULTI, DIRECT, REVIEW, ASK_USER, decide_route, adjudicated_label
from .recovery import start_session_health
from .release import is_prose_doc_path
from .skills import SkillRegistry, SkillRegistryError
from .task_context import freeze_context, freeze_materials, read_context_file
from .store import EventStore, MEMORY_TERMINAL_STATES, terminal_reregistration_error


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_DIR / "config" / "agentos.json"
RUNNER_APP_NAME = "小八 AgentOS Runner.app"
RUNNER_EXECUTABLE_NAME = "XiaobaAgentOSRunner"


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _print(value: Any, as_json: bool = False) -> None:
    if as_json or isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    else:
        print(value)


class EvidenceRestoreError(RuntimeError):
    """The staged evidence could not be put back; manual action is required.

    ``retained`` names the private locations where this command kept the
    original object instead of deleting it (受限方案丙), so the caller can put
    them in the report the user reads.
    """

    def __init__(self, message: str, retained: Optional[List[str]] = None) -> None:
        super().__init__(message)
        self.retained: List[str] = list(retained or [])


RETAINED_NOTE = (
    "the approved 受限方案丙 keeps the original object instead of deleting it: the live "
    "path was dealt with, the object it replaced is retained under a private name and is "
    "listed in retained_objects (the self-check lists it too, including after a restart), "
    "and nothing is cleaned up automatically"
)


# (existed, previous_bytes, mode).  `previous` is None either when the file was
# genuinely absent or when it exists but its bytes could not be read; `existed`
# is what tells the two apart.
EvidenceSnapshot = Tuple[bool, Optional[bytes], Optional[int]]


def _read_evidence(path: Path) -> EvidenceSnapshot:
    """Snapshot a candidate file before a command is allowed to overwrite it.

    Only a genuinely missing file is reported as absent.  A file that exists but
    whose bytes cannot be read (permissions, a directory, a symlink, an I/O
    error) comes back as ``existed=True, previous=None`` so the caller refuses
    instead of overwriting bytes it could not preserve.
    """
    if path.is_symlink():
        return True, None, None
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return False, None, None
    except OSError:
        return True, None, None
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = None
    return True, data, mode


def _place_evidence_bytes(path: Path, content: bytes, mode: int) -> None:
    """Put ``content`` back at a path this command has just atomically emptied.

    ``os.link`` is the portable primitive that creates a name *only if it is
    free* (``FileExistsError`` otherwise), so evidence a second writer put at the
    path after the claim can never be overwritten by the restore; the caller
    turns that error into reported manual action.

    The staging name is *reserved* before anything is written into it (see
    :func:`xiaoba_agentos.memory._reserve_private_name`): the old blind
    ``write_bytes`` would truncate whatever an earlier call had left at the same
    name.  A taken staging name fails as a generic ``OSError`` (not
    ``FileExistsError``, which this command reserves for "the live path was
    taken"), so the captured copy is put back and the occupied location is named
    in the error instead of being silently overwritten.
    """
    staged = _reserve_private_name(path, "restore")
    try:
        staged.write_bytes(content)
        os.chmod(staged, mode)
        os.link(staged, path)
    finally:
        try:
            if staged.exists():
                staged.unlink()
        except OSError:
            pass


def _put_back_claim(claim: Path, path: Path) -> Optional[Path]:
    """Return a captured evidence file that turned out not to be this command's write.

    Returns ``None`` when the captured file is back at ``path``.  When the path
    is taken, the captured copy stays at ``claim`` and that location is returned
    so the caller can name the *actual* place the bytes are kept instead of
    reporting a conflict without a location or staying silent.
    """
    try:
        os.link(claim, path)
    except OSError:
        return claim  # keep the captured copy; the caller reports its location
    try:
        claim.unlink()
    except OSError:
        pass
    return None


def _retain_evidence_object(
    claim: Path,
    path: Path,
    written_sha256: Optional[str],
    *,
    config: Optional[AppConfig],
    task_id: Optional[str],
    operation: str,
    reason: str,
) -> str:
    """Keep a captured evidence object under its private name (受限方案丙).

    The object is not a copy: it is the very inode that was at ``path``, moved
    aside with ``os.replace``, so bytes another process writes through a file
    descriptor it opened before the claim are still in that object.  Keeping it
    never depends on bookkeeping: the retention is recorded in the self-check
    journal when a configuration is available, and a journal that cannot be
    written is reported, never turned into a deletion.

    Returns the sentence the caller reports to the user.
    """
    record_error = retained_objects.record_retained(
        config,
        retained_path=claim,
        original_path=path,
        operation=operation,
        reason=reason,
        task_id=task_id,
        written_bytes_sha256=written_sha256,
        writer_pid=os.getpid(),
    )
    message = (
        f"{path}: {reason}; the captured original object is kept at {claim} and is "
        "not deleted, so bytes written through file descriptors opened before the claim "
        "are still there; manual verification is required"
    )
    if record_error is not None:
        message += (
            f"; the retained object could not be recorded for the self-check "
            f"({record_error})"
        )
    return message


def _restore_evidence(
    path: Path,
    snapshot: EvidenceSnapshot,
    written_sha256: Optional[str],
    *,
    config: Optional[AppConfig] = None,
    task_id: Optional[str] = None,
    operation: str = "evidence.staged",
) -> List[str]:
    """Undo the staging write a refusal left behind, and only if it is still ours.

    ``written_sha256`` is the hash of what this command staged.  The check and
    the undo are one atomic step: the file is first moved aside with
    ``os.replace`` (the primitive that captures exactly the object that was at
    the path at one instant) and attribution is decided on the *captured* bytes,
    never on a read of the live path that the undo would then trust.  A file a
    second writer changed after the check is therefore never deleted or
    overwritten.  Anything that cannot be put back raises EvidenceRestoreError,
    so the caller reports manual action instead of claiming a clean rollback.

    The rollback staging name is *reserved* first (see
    :func:`xiaoba_agentos.memory._reserve_private_name`): it can already hold a
    copy an earlier command kept after a failed put-back, and ``os.replace``
    would destroy that content.  An occupied name is never touched; the command
    fails with an error naming the occupied location and leaves the evidence
    file unchanged.

    Under the approved 受限方案丙 the captured object is never deleted: once the
    live path has been dealt with, the object stays under its private name, and
    the returned list (also ``EvidenceRestoreError.retained``) names it, so a
    writer that opened a file descriptor before the claim can still be seen.
    The caller must therefore never report a bare, complete recovery when the
    list is not empty.
    """
    existed, previous, mode = snapshot
    if existed and previous is None:
        raise EvidenceRestoreError(
            f"evidence file {path} could not be snapshotted before the command ran"
        )
    try:
        if not path.is_file():
            return []
        current = path.read_bytes()
    except OSError as exc:
        raise EvidenceRestoreError(f"cannot read {path} back to restore it: {exc}") from exc
    ours = written_sha256 is not None and hashlib.sha256(current).hexdigest() == written_sha256
    if existed and current == previous:
        return []
    if not ours:
        if existed:
            raise EvidenceRestoreError(
                f"{path} changed after this command staged it; refusing to overwrite it"
            )
        raise EvidenceRestoreError(f"{path} is not this command's output; leaving it in place")
    try:
        claim = _reserve_private_name(path, "rollback")
    except OSError as exc:
        raise EvidenceRestoreError(f"cannot restore {path}: {exc}") from exc
    try:
        os.replace(path, claim)
    except OSError as exc:
        _release_reserved_name(claim)
        raise EvidenceRestoreError(f"cannot restore {path}: {exc}") from exc
    try:
        claimed = claim.read_bytes()
    except OSError as exc:
        raise EvidenceRestoreError(f"cannot read {path} back to restore it: {exc}") from exc
    if hashlib.sha256(claimed).hexdigest() != written_sha256:
        # The live path changed between the check and the atomic claim: the
        # captured file belongs to somebody else.
        kept = _put_back_claim(claim, path)
        if kept is not None:
            detail = _retain_evidence_object(
                kept,
                path,
                written_sha256,
                config=config,
                task_id=task_id,
                operation=operation,
                reason=(
                    "the live path was taken by a newer writer, so the captured "
                    "object was not put back"
                ),
            )
            raise EvidenceRestoreError(
                f"{path} changed after this command staged it; refusing to "
                f"overwrite it; {detail}",
                retained=[str(kept)],
            )
        raise EvidenceRestoreError(
            f"{path} changed after this command staged it; refusing to overwrite it"
        )
    if not existed:
        detail = _retain_evidence_object(
            claim,
            path,
            written_sha256,
            config=config,
            task_id=task_id,
            operation=operation,
            reason=(
                "this command staged the file and its write is being removed from "
                "the live path"
            ),
        )
        if path.exists():
            raise EvidenceRestoreError(
                f"{path} was recreated while this command was removing its own staged "
                f"write; {detail}",
                retained=[str(claim)],
            )
        return [str(claim)]
    try:
        _place_evidence_bytes(path, previous, mode if mode is not None else 0o600)
    except FileExistsError:
        detail = _retain_evidence_object(
            claim,
            path,
            written_sha256,
            config=config,
            task_id=task_id,
            operation=operation,
            reason=(
                "a newer writer took the live path, so the previous bytes were not "
                "put back"
            ),
        )
        raise EvidenceRestoreError(
            f"{path} changed after this command staged it; refusing to overwrite it; "
            f"{detail}",
            retained=[str(claim)],
        )
    except OSError as exc:
        kept = _put_back_claim(claim, path)
        message = f"cannot restore {path}: {exc}"
        if kept is not None:
            message += "; " + _retain_evidence_object(
                kept,
                path,
                written_sha256,
                config=config,
                task_id=task_id,
                operation=operation,
                reason="the previous bytes could not be put back",
            )
            raise EvidenceRestoreError(message, retained=[str(kept)]) from exc
        message += (
            "; the captured object was returned to this path with a hard link, so no "
            "object is retained elsewhere"
        )
        raise EvidenceRestoreError(message) from exc
    detail = _retain_evidence_object(
        claim,
        path,
        written_sha256,
        config=config,
        task_id=task_id,
        operation=operation,
        reason=(
            "the previous bytes were put back, so the object this command replaced "
            "is retained"
        ),
    )
    return [str(claim)]


def _load(path: str, initialize: bool = True) -> tuple:
    config = AppConfig.load(Path(path))
    store = EventStore(config.database_path)
    if initialize:
        store.initialize()
    try:
        from .workday_ledger import configure
        configure(store,config)
    except Exception:pass
    return config, store


def _submit_task(
    config: AppConfig,
    store: EventStore,
    prompt: str,
    priority: int = 0,
    *,
    session_id: Optional[str] = None,
    route_decision_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    required_capabilities: tuple = (),
    needs_web: bool = False,
    task_context: str = "",
    material_files: tuple = (),
    workflow: Optional[dict] = None,
    experience_kind: Optional[str] = None,
    problem_key: str = "",
    experience_trial: Optional[dict] = None,
    task_purpose: Optional[str] = None,
    work_task_id: Optional[str] = None,
    no_work_task: bool = False,
    release_acceptance: bool = False,
    host: Optional[dict] = None,
    needs_review: bool = True,
    memory_bridge_cls: Any = None,
    skill_registry_cls: Any = None,
) -> str:
    if release_acceptance and experience_trial is not None:
        raise ValueError("release acceptance cannot also be an experience trial")
    if task_purpose == "release_acceptance" and not release_acceptance:
        raise ValueError("release acceptance purpose requires --release-acceptance")
    if task_purpose == "experience_trial" and experience_trial is None:
        raise ValueError("experience trial purpose requires a frozen trial record")
    controlled_purpose = (
        "release_acceptance" if release_acceptance else
        "experience_trial" if experience_trial is not None else
        task_purpose
    )
    if controlled_purpose not in (None, "release_smoke", "release_acceptance", "experience_trial"):
        raise ValueError("invalid controlled task purpose")
    if release_acceptance and task_purpose not in (None, "release_acceptance"):
        raise ValueError("release acceptance purpose conflicts with task-purpose")
    if experience_trial is not None and task_purpose not in (None, "experience_trial"):
        raise ValueError("experience trial purpose conflicts with task-purpose")
    if controlled_purpose in {"release_smoke", "release_acceptance", "experience_trial"}:
        if experience_kind not in (None, "audit"):
            raise ValueError("release smoke, acceptance and experience trials must use audit settlement")
        kind = "audit"
        if not needs_review:
            raise ValueError("controlled audit tasks require review")
    else:
        kind = experience_kind or ("audit" if prompt.startswith("你是小八 AgentOS 的独立审查者之一，只读审查技能 ") else "method")
    if controlled_purpose is None:
        controlled_purpose = "technical_audit" if kind == "audit" else "user_task"
    from .experience_workflow import KINDS, match_trials, suggest_trials
    if kind not in KINDS:
        raise ValueError("invalid experience kind")
    if release_acceptance:
        # 廉价确定性检查前移：构包门禁（build_release._validate_acceptance_payload）要求
        # 验收任务声明来源工作任务或明确没有。与其跑完整轮双审到构包才被拒收（卡 49
        # 第四轮返工），提交时当场拒绝。只前移“声明是否存在”这一确定性检查，不改构包
        # 门禁、评分策略、用户裁决或事件/冻结哈希约束。
        # 必须早于 MemoryBridge.agent_contexts()：后者可经 formal_review.record_hits 写入
        # memory.formal_retrieved，知识检索最多等 30 秒，拒收路径不该产生这些副作用。
        # 用只含 review_context 与 workflow 的最小只读快照调用，与构包同口径；workflow
        # 也必须在其中，否则“发布验收 + workflow 来源声明”的合法组合会被误拒。
        from .pool_dispatch import declared_work_tasks
        probe = {
            'review_context': {
                'work_task_id': work_task_id,
                'no_work_task': no_work_task,
            },
        }
        if workflow:
            probe['workflow'] = workflow
        source_ids, explicit_none = declared_work_tasks(probe)
        if not source_ids and not explicit_none:
            raise ValueError(
                '发布验收任务必须声明来源工作任务：提交时用 --work-task 链上工作任务，'
                '或明确声明 --no-work-task')
    # 来源工作任务的确定性与存在性检查同样前移到检索之前：无效提交不该白跑
    # MemoryBridge.agent_contexts（可经 formal_review.record_hits 写 memory.formal_retrieved，
    # 知识检索最多等 30 秒）。错误语义保持原样。
    if work_task_id and not store.get_task(work_task_id):
        raise ValueError('来源工作任务不存在')
    if work_task_id and no_work_task:
        raise ValueError('来源工作任务和“没有来源工作任务”只能声明一个')
    # Validate host identity before retrieval can record formal activity.
    if host is not None and (set(host) != {'id','entry','model'} or any(not isinstance(v,str) or not v.strip() for v in host.values())):
        raise ValueError('宿主身份需要 id、entry、model；不能自述未干活')
    frozen_context = freeze_context(task_context)
    frozen_materials = freeze_materials(material_files)
    enabled_names = [agent.name for agent in config.agents if agent.enabled]
    if kind == "audit" or workflow:
        assignment = {"hits": [], "contexts": {}, "receipt": None}
        hits, context = [], ""
        skills = {"selected": [], "context": "", "context_sha256": hashlib.sha256(b"").hexdigest()}
    else:
        bridge_cls = memory_bridge_cls or MemoryBridge
        registry_cls = skill_registry_cls or SkillRegistry
        memory = bridge_cls(config, store)
        assignment = memory.agent_contexts(prompt, enabled_names)
        hits = assignment["hits"]
        context = memory.render_hits(hits)
        skills = registry_cls(config).select_context(prompt)
    snapshot = {
        "prompt": prompt,
        "task_context": frozen_context,
        "required_capabilities": list(required_capabilities),
        "needs_web": needs_web,
        "release_expected_version": config.expected_release_version,
        "source_manifest_sha256": _manifest_fingerprint(config.project_dir),
        "runtime_manifest_sha256": _runtime_manifest_fingerprint(config.project_dir),
        "knowlp_context": context,
        "knowlp_context_hash": hashlib.sha256(context.encode("utf-8")).hexdigest(),
        "memory_hits": hits,
        "retrieval_receipt": assignment.get("receipt"),
        "agent_memory_contexts": assignment["contexts"],
        "skills": skills["selected"],
        "skill_context": skills["context"],
        "skill_context_hash": skills["context_sha256"],
        "agents": [dict(agent.raw) for agent in config.agents],
        "worker": dict(config.worker),
        "provenance": {
            "kind": "agentos_multi_adjudicated",
            "minimum_successful_agents": int(config.worker["minimum_successful_agents"]),
        },
        "task_purpose": controlled_purpose,
    }
    if frozen_materials:
        snapshot["material_files"] = frozen_materials
    snapshot['needs_review'] = needs_review
    snapshot['review_context'] = {'work_task_id': work_task_id, 'host': host, 'needs_review': needs_review, 'release_acceptance': release_acceptance}
    if no_work_task:
        # 发布验收要从来源任务读出作者和执行者，所以“没有来源任务”也得显式声明一次；
        # 什么都不写不算声明，构包拒收。
        snapshot['review_context']['no_work_task'] = True
    if workflow:
        snapshot["workflow"] = workflow
        snapshot["agent_memory_contexts"] = {}
        snapshot["knowlp_context"] = ""
        snapshot["knowlp_context_hash"] = hashlib.sha256(b"").hexdigest()
        snapshot["memory_hits"] = []
        snapshot["retrieval_receipt"] = None
        snapshot["provenance"] = {"kind": "agentos_collaboration_" + workflow["phase"],
                                  "minimum_successful_agents": len(workflow["agents"])}
        hits = []
    snapshot["experience_policy"] = {"version": 1, "kind": kind, "problem_key": problem_key}
    if kind in {"method", "rule"} and needs_review:
        snapshot["value_receipt_contract"] = {"version": 2, "review_type": "new_experience"}
    snapshot.update(suggest_trials(store, problem_key, prompt) if kind != "audit" else {
        "trial_suggestions": [], "trial_suggestion_status": "ok"})
    if experience_trial is not None:
        if kind != "audit":
            raise ValueError("trial validation must be audit-only")
        snapshot["experience_trial"] = experience_trial
        snapshot.update(agent_memory_contexts={}, knowlp_context="", memory_hits=[], retrieval_receipt=None)
        snapshot["knowlp_context_hash"] = hashlib.sha256(b"").hexdigest()
        snapshot.update(skills=[], skill_context="", skill_context_hash=hashlib.sha256(b"").hexdigest())
        hits = []
    task_id = store.submit(
        prompt,
        int(config.worker["max_attempts"]),
        priority,
        snapshot=snapshot,
        idempotency_key=idempotency_key,
        session_id=session_id,
        route_decision_id=route_decision_id,
        provenance=snapshot["provenance"],
    )
    return task_id


def _answer_provenance(route: str) -> str:
    return {
        DIRECT: "session_model_direct",
        AGENTOS_MULTI: "agentos_multi_adjudicated",
        REVIEW: "agentos_review",
        ASK_USER: "agentos_clarification",
    }[route]


def _code_fingerprint(project_dir: Path) -> str:
    digest = hashlib.sha256()
    source_dir = project_dir / "src" / "xiaoba_agentos"
    paths = sorted(source_dir.rglob("*.py"))
    for path in paths:
        relative = str(path.relative_to(project_dir))
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _manifest_fingerprint(project_dir: Path) -> Optional[str]:
    path = project_dir / "SOURCE_MANIFEST.json"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _runtime_manifest_fingerprint(project_dir: Path) -> Optional[str]:
    path = project_dir / "SOURCE_MANIFEST.json"
    if not path.is_file() or path.is_symlink():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        files = data.get("files")
        if not isinstance(files, dict) or not files:
            return None
        runtime_files = {
            k: v for k, v in sorted(files.items())
            if not is_prose_doc_path(k)
        }
        if not runtime_files:
            return None
        canonical = json.dumps({"algorithm": "sha256", "files": runtime_files},
                               ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()
    except (OSError, ValueError, TypeError):
        return None


def _write_worker_state(config: AppConfig, store: EventStore) -> Dict[str, Any]:
    state = {
        "pid": os.getpid(),
        "started_at": time.time(),
        "code_fingerprint": _code_fingerprint(config.project_dir),
        "manifest_fingerprint": _manifest_fingerprint(config.project_dir),
        "database_schema_version": store.schema_status().get("current_version"),
    }
    target = config.runtime_dir / "worker-state.json"
    temporary = config.runtime_dir / f"worker-state.{os.getpid()}.tmp"
    _secure_directory(config.runtime_dir)
    temporary.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    os.chmod(target, 0o600)
    return state


def _worker_runtime_status(config: AppConfig) -> Dict[str, Any]:
    target = config.runtime_dir / "worker-state.json"
    current_code = _code_fingerprint(config.project_dir)
    current_manifest = _manifest_fingerprint(config.project_dir)
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "state_file": str(target), "error": str(exc)}
    pid = int(loaded.get("pid") or 0)
    alive = False
    if pid > 0:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            pass
    code_matches = loaded.get("code_fingerprint") == current_code
    manifest_matches = loaded.get("manifest_fingerprint") == current_manifest
    return {
        **loaded,
        "state_file": str(target),
        "alive": alive,
        "current_code_fingerprint": current_code,
        "current_manifest_fingerprint": current_manifest,
        "code_matches_disk": code_matches,
        "manifest_matches_disk": manifest_matches,
        "ok": alive and code_matches and manifest_matches,
    }


def _demo_config(config: AppConfig) -> AppConfig:
    raw = json.loads(json.dumps(config.raw))
    raw["runtime_dir"] = str(config.runtime_dir / "demo")
    raw["agents"] = [
        {"name": "offline_planner", "type": "mock", "enabled": True, "role": "规划", "timeout_seconds": 5, "template": "离线规划：拆解并验证任务“{prompt}”。"},
        {"name": "offline_critic", "type": "mock", "enabled": True, "role": "审查", "timeout_seconds": 5, "template": "离线审查：检查任务“{prompt}”的边界、风险和验收条件。"},
        {"name": "offline_verifier", "type": "mock", "enabled": True, "role": "验证", "timeout_seconds": 5, "template": "离线验证：为任务“{prompt}”生成可重复测试。"},
    ]
    raw["memory"]["knowlp_enabled"] = False
    return AppConfig(config.path, raw)


def _render_launchagent(config: AppConfig) -> Path:
    template = config.project_dir / "launchd" / "com.xiaoba.agentos.plist.template"
    target = config.project_dir / "artifacts" / "com.xiaoba.agentos.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = (
        template.read_text(encoding="utf-8")
        .replace("@@PROJECT_DIR@@", str(config.project_dir))
        .replace("@@CONFIG@@", str(config.path))
        .replace("@@NODE_DIR@@", str(Path(config.integrations["node_command"]).parent))
        .replace("@@RUNNER_EXECUTABLE@@", str(_runner_app_path() / "Contents" / "MacOS" / RUNNER_EXECUTABLE_NAME))
    )
    target.write_text(rendered, encoding="utf-8")
    return target


def _runner_app_path() -> Path:
    return Path.home() / "Applications" / RUNNER_APP_NAME


def _verify_runner_app(target: Path) -> Dict[str, Any]:
    executable = target / "Contents" / "MacOS" / RUNNER_EXECUTABLE_NAME
    plist = target / "Contents" / "Info.plist"
    lint = subprocess.run(["/usr/bin/plutil", "-lint", str(plist)], text=True, capture_output=True, check=False)
    signature = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(target)],
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        "ok": executable.is_file() and os.access(executable, os.X_OK) and lint.returncode == 0 and signature.returncode == 0,
        "executable": str(executable),
        "plist_lint": (lint.stdout or lint.stderr).strip(),
        "signature": (signature.stdout or signature.stderr).strip() or "valid",
    }


def _move_to_trash(path: Path) -> Optional[str]:
    """Move a replaced program bundle to the user's Trash; None if that is not possible."""
    trash = Path.home() / ".Trash"
    if not trash.is_dir():
        return None
    destination = trash / f"{path.name}-{time.time_ns()}"
    try:
        shutil.move(str(path), str(destination))
    except OSError:
        return None
    return str(destination)


def _build_runner_app(config: AppConfig, target: Optional[Path] = None) -> Dict[str, Any]:
    target = target or _runner_app_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    source = config.project_dir / "macos" / "runner" / "main.swift"
    info = config.project_dir / "macos" / "runner" / "Info.plist"
    backup: Optional[Path] = None
    with tempfile.TemporaryDirectory(prefix="xiaoba-runner-", dir=str(target.parent)) as temporary:
        staged = Path(temporary) / RUNNER_APP_NAME
        contents = staged / "Contents"
        executable_dir = contents / "MacOS"
        executable_dir.mkdir(parents=True)
        shutil.copy2(info, contents / "Info.plist")
        executable = executable_dir / RUNNER_EXECUTABLE_NAME
        compiled = subprocess.run(
            ["/usr/bin/swiftc", str(source), "-o", str(executable)],
            text=True,
            capture_output=True,
            check=False,
        )
        if compiled.returncode != 0:
            raise RuntimeError(f"Runner 编译失败：{compiled.stderr or compiled.stdout}")
        os.chmod(executable, 0o755)
        signed = subprocess.run(
            [
                "/usr/bin/codesign", "--force", "--deep", "--sign", "-",
                "--identifier", "com.xiaoba.agentos.runner", str(staged),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if signed.returncode != 0:
            raise RuntimeError(f"Runner 签名失败：{signed.stderr or signed.stdout}")
        verified = _verify_runner_app(staged)
        if not verified["ok"]:
            raise RuntimeError(f"Runner 验证失败：{verified}")
        # The previous Runner is moved aside next to the target, outside this
        # build's temporary directory, so no failure below can let the
        # temporary directory delete it (round 28). It is never kept as a
        # backup (user rule, B03): after a successful replacement it goes to
        # the user's Trash; if anything fails it stays where the error says.
        if target.exists():
            backup = target.parent / f".{RUNNER_APP_NAME}.previous-{time.time_ns()}"
            shutil.move(str(target), str(backup))
        try:
            os.replace(str(staged), str(target))
        except Exception as exc:
            if backup is None or not backup.exists():
                raise
            put_back = False
            if not target.exists():
                try:
                    shutil.move(str(backup), str(target))
                    put_back = True
                except Exception:
                    put_back = False
            if put_back:
                # The old Runner is back where it was: report the original
                # replacement failure unchanged; nothing is left aside.
                raise exc
            raise RuntimeError(
                f"替换 Runner 失败：{exc}；旧 Runner 未能放回原处，留在 {backup}，请用户处理"
            ) from exc
    previous_runner: Optional[str] = None
    if backup is not None and backup.exists():
        previous_runner = _move_to_trash(backup)
        if previous_runner is None:
            # Trash unavailable: keep the old bundle in the runtime directory
            # and say so.
            kept = config.runtime_dir / f"replaced-{RUNNER_APP_NAME}-{time.time_ns()}"
            try:
                shutil.move(str(backup), str(kept))
                previous_runner = f"未能移入废纸篓，留在 {kept}，请用户处理"
            except OSError:
                previous_runner = f"未能移入废纸篓，也未能移到运行目录，留在 {backup}，请用户处理"
    notice = f"；旧 Runner：{previous_runner}" if previous_runner else ""
    result = _verify_runner_app(target)
    if not result["ok"]:
        raise RuntimeError(f"安装后的 Runner 验证失败：{result}{notice}")
    check_environment = os.environ.copy()
    check_environment.update(
        {
            "XIAOBA_PROJECT_DIR": str(config.project_dir),
            "XIAOBA_CONFIG": str(config.path),
            "XIAOBA_RUNNER_CHECK_ONLY": "1",
        }
    )
    try:
        check = subprocess.run(
            [result["executable"]], text=True, capture_output=True, timeout=10, check=False, env=check_environment
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Runner 自检 10 秒内未结束{notice}") from exc
    if check.returncode != 0:
        raise RuntimeError(f"Runner 自检失败：{check.stderr or check.stdout}{notice}")
    result.update({"path": str(target), "previous_runner": previous_runner, "self_check": check.stdout.strip()})
    return result


def _primary_agents(config: Any) -> list:
    """Enabled non-standby agents; also works for minimal config stand-ins."""
    primaries = getattr(config, "primary_agents", None)
    if primaries is not None:
        return list(primaries)
    return [agent for agent in config.agents if agent.enabled and not getattr(agent, "standby", False)]


def _set_agents(config: AppConfig, names: list, enabled: bool) -> Dict[str, Any]:
    known = {item["name"] for item in config.raw.get("agents", [])}
    unknown = sorted(set(names) - known)
    if unknown:
        raise ConfigError("未知 Agent：" + ", ".join(unknown))
    # No copy of the config is made (user rule, B03): the new content is
    # written to a temporary file and swapped in atomically, so a failure
    # leaves the old file untouched.
    raw = json.loads(json.dumps(config.raw))
    for item in raw.get("agents", []):
        if item["name"] in names:
            item["enabled"] = enabled
    temporary = config.path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(config.path))
    return {"updated": names, "enabled": enabled}


def _launchctl_print(domain: str, label: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/launchctl", "print", f"{domain}/{label}"],
        text=True,
        capture_output=True,
        check=False,
    )


def _wait_service_unloaded(domain: str, label: str, timeout_seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _launchctl_print(domain, label).returncode != 0:
            return True
        time.sleep(0.25)
    return False


def _rollback_service(
    config: AppConfig,
    destination: Path,
    backup: Optional[bytes],
    domain: str,
    label: str,
    was_loaded: bool,
) -> Optional[str]:
    """Take the failed new plist out of place and put the original back.

    Both callers run only after the new plist replaced `destination`, so what
    is moved aside is the failed new configuration, never the original. The
    original is staged from memory first, so no failure in between leaves
    `destination` empty. Returns the failed copy's path, or None when there
    was nothing to move (then no backups directory is created either).
    """
    current = _launchctl_print(domain, label)
    if current.returncode == 0:
        subprocess.run(
            ["/bin/launchctl", "bootout", f"{domain}/{label}"],
            text=True,
            capture_output=True,
            check=False,
        )
        _wait_service_unloaded(domain, label)
    staged = None
    if backup is not None:
        staged = destination.with_suffix(".plist.agentos-restore")
        staged.write_bytes(backup)
    failed_copy: Optional[Path] = None
    if destination.exists():
        failed_copy = config.runtime_dir / "backups" / "launchd" / f"failed-install-{time.time_ns()}.plist"
        failed_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(destination), str(failed_copy))
    if staged is not None:
        try:
            os.replace(str(staged), str(destination))
        except OSError as exc:
            # The failed plist is already moved aside; the original bytes are
            # still in `staged`, which is left in place so it can be put back by
            # hand (round 31: the error used to leave no trace of it).
            raise RuntimeError(
                f"原服务配置未能放回原处：{exc}；原配置内容保存在 {staged}，"
                f"失败的新配置在 {failed_copy}，请用户处理"
            ) from exc
        if was_loaded:
            subprocess.run(
                ["/bin/launchctl", "bootstrap", domain, str(destination)],
                text=True,
                capture_output=True,
                check=False,
            )
    return str(failed_copy) if failed_copy is not None else None


def _rotate_worker_logs(config: AppConfig) -> list[str]:
    log_dir = config.runtime_dir / "logs"
    backup_dir = config.runtime_dir / "backups" / "logs"
    rotated = []
    for name in ("worker.out.log", "worker.err.log"):
        source = log_dir / name
        if not source.is_file():
            continue
        if source.stat().st_size == 0:
            # launchd opens both log paths before exec'ing the job.  When a
            # placeholder left behind by another process is already there,
            # xpcproxy exits EX_CONFIG(78) and the Runner never runs, so the
            # empty stderr log that would explain it is the very file launchd
            # could not open.  Only a directory launchd creates into works.
            source.unlink()
            continue
        _secure_directory(backup_dir)
        target = backup_dir / f"{source.stem}-{time.time_ns()}{source.suffix}"
        shutil.move(str(source), str(target))
        os.chmod(target, 0o600)
        rotated.append(str(target))
    return rotated


def _health_report(config: AppConfig, store: EventStore) -> Dict[str, Any]:
    """Health exactly as the gates weigh it, plus what was excluded and why.

    A bare ``agent_health()`` lists retired Agents as healthy capacity, so the
    ops report looked fine while ``run_once()`` refused every task.  Dropping the
    stale rows without a trace would repeat that mistake in the other direction,
    so they stay visible under ``agent_health_excluded``.
    """
    max_age = float(config.worker.get("health_max_age_seconds", 86400))
    enabled = {agent.name for agent in config.agents if agent.enabled}
    rows = store.agent_health()
    live = {
        row["agent_name"]
        for row in store.agent_health(enabled_names=sorted(enabled), max_age_seconds=max_age)
    }
    now = time.time()
    return {
        "agent_health": [row for row in rows if row["agent_name"] in live],
        "agent_health_excluded": [
            {
                "agent_name": row["agent_name"],
                "status": row["status"],
                "age_seconds": round(now - float(row.get("last_probe_at") or 0), 1),
                "reason": "not_enabled" if row["agent_name"] not in enabled else "probe_too_old",
            }
            for row in rows
            if row["agent_name"] not in live
        ],
        "agent_health_max_age_seconds": max_age,
    }


def _backup_database(config: AppConfig, store: EventStore) -> Optional[Path]:
    """Copy the database before a schema migration.

    Only called when the user explicitly passed --backup-database: a backup is
    never made on the user's behalf (user rule, B03).
    """
    if not store.path.is_file():
        return None
    backup_dir = config.runtime_dir / "backups" / "database"
    _secure_directory(backup_dir)
    target = backup_dir / f"agentos-pre-migration-{time.time_ns()}.sqlite3"
    source_uri = f"file:{store.path}?mode=ro"
    try:
        with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source, \
                closing(sqlite3.connect(str(target))) as destination:
            source.backup(destination)
            # The copy inherits WAL mode; switch it back so the backup is one
            # self-contained file with no -wal/-shm siblings left beside it.
            destination.execute("PRAGMA journal_mode=DELETE").fetchone()
            check = destination.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError(f"数据库备份完整性检查失败：{check}")
    except BaseException:
        # A copy that failed part-way is not a restore point: remove what this
        # call itself just wrote, so nobody later mistakes it for one (round 30).
        # Existing backups are never touched.
        for leftover in (target, Path(str(target) + "-wal"), Path(str(target) + "-shm")):
            if leftover.is_file():
                leftover.unlink()
        raise
    # All connections to the copy are closed and it is back in rollback-journal
    # mode, so any -wal/-shm left beside it belongs to nobody (older SQLite
    # keeps the -shm).  Remove only the copy's own sidecars.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(target) + suffix)
        if sidecar.is_file():
            sidecar.unlink()
    os.chmod(target, 0o600)
    return target


def _database_needs_migration(store: EventStore) -> bool:
    """True only for an existing database whose schema is not the current one."""
    if not store.path.is_file():
        return False
    status = store.schema_status()
    return not (status.get("ok") and status.get("current_version") == status.get("expected_version"))


def _prepare_database_for_install(
    config: AppConfig, store: EventStore, backup_authorized: bool
) -> Optional[Path]:
    """Decide whether install-service may touch the database, and how.

    A current database needs no migration and gets no backup.  A database that
    does need migrating is refused unless the user explicitly authorized a
    backup with --backup-database, because a migration without a way back is
    not safe and a backup without the user's say-so is not allowed.
    """
    if not _database_needs_migration(store):
        return None
    if not backup_authorized:
        status = store.schema_status()
        raise RuntimeError(
            "数据库需要迁移（当前结构版本 {current}，需要 {expected}），迁移前要先备份；"
            "按用户规则，备份必须由用户明确授权。确认后加 --backup-database 重新运行 install-service。".format(
                current=status.get("current_version"), expected=status.get("expected_version"))
        )
    return _backup_database(config, store)


def _restore_database(store: EventStore, backup: Path) -> None:
    staged = store.path.with_suffix(".sqlite3.restore")
    try:
        shutil.copy2(backup, staged)
        os.chmod(staged, 0o600)
    except BaseException:
        if staged.is_file():
            staged.unlink()
        raise
    # The live database's own -wal/-shm hold the failed migration's pages; left
    # beside the restored file, SQLite would replay them onto it on the next
    # open and the restore would no longer be the backup (round 36). The service
    # is stopped and every store connection is closed at this point.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(store.path) + suffix)
        if sidecar.is_file():
            sidecar.unlink()
    os.replace(staged, store.path)


def _restart_existing_service(domain: str, destination: Path, was_loaded: bool) -> Optional[bool]:
    """Bring the old service back; None when there was nothing to restart.

    The result is returned, not swallowed: an error message must not say the
    old service is running again when launchctl refused it (round-37 review).
    """
    if not (was_loaded and destination.is_file()):
        return None
    started = subprocess.run(
        ["/bin/launchctl", "bootstrap", domain, str(destination)],
        text=True,
        capture_output=True,
        check=False,
    )
    return started.returncode == 0


_RESTART_FAILED = "，但原服务重启失败（launchctl bootstrap 未成功），请用 launchctl print 检查并手动启动"


def _doctor_failure_reason(result: Dict[str, Any]) -> str:
    """Why doctor said no, in words the user can act on.

    ``database_schema`` is a whole check dictionary, not a message: formatting it
    straight into the error produced "迁移后 doctor 未通过：None" whenever that
    field was empty, which told the user nothing.  The failed checks' own
    problems/reason are the real answer; if none of them explains itself, say so
    instead of printing a bare ``None``.
    """
    reasons = [str(item) for item in (result.get("problems") or result.get("blockers") or []) if item]
    if not reasons:
        for name, check in result.items():
            if not isinstance(check, dict) or check.get("ok", True):
                continue
            detail = check.get("problems") or check.get("reason") or check.get("error")
            if isinstance(detail, (list, tuple)):
                detail = "；".join(str(item) for item in detail if item)
            if detail:
                reasons.append(f"{name}: {detail}")
    return "；".join(reasons) if reasons else "原因未知"


def _service_ready(config: AppConfig, report: Dict[str, Any]) -> bool:
    """装服务只看本地完整性：节点要在服务装好后才能建，审查任务会被单独挡住。"""
    from .pool_dispatch import _mock_agents
    if _mock_agents(config):
        return bool(report.get("ready_for_external_tasks"))
    return bool(report.get("ok") or report.get("ready_for_external_tasks"))


def _install_service(config: AppConfig, store: EventStore, backup_database: bool = False) -> Dict[str, Any]:
    preflight = doctor(config, store)
    from .pool_dispatch import _mock_agents
    if _mock_agents(config):
        primaries = preflight.get("primary_agents", preflight.get("enabled_agents", []))
        enough_agents = len(primaries) >= int(preflight.get("minimum_successful_agents", 0)) > 0
    else:
        enough_agents = True
    paths = preflight.get("paths")
    base_ready = (
        # Fail closed like the other checks: a missing or empty paths report is
        # not "all paths fine" (round-23 review).
        isinstance(paths, dict) and bool(paths) and all(paths.values())
        and preflight.get("event_chain", {}).get("ok", False)
        and preflight.get("local_cli_routes", {}).get("ok", False)
        and preflight.get("memory_probe", {}).get("ok", False)
        and enough_agents
    )
    if not base_ready:
        problems = preflight.get("local_cli_routes", {}).get("problems", [])
        detail = "；".join(problems) if problems else "安装前基础检查未通过"
        raise RuntimeError(f"安装前检查未通过：{detail}")
    _secure_directory(config.runtime_dir)
    _secure_directory(config.runtime_dir / "logs")
    runner = _verify_runner_app(_runner_app_path())
    if not runner["ok"]:
        # Keep service installation self-contained.  A stale plist pointing
        # at a missing Runner app makes the worker look installed while it can
        # never start; build and verify the signed helper before bootstrapping
        # launchd instead.  `runner` is kept: a rebuilt Runner may have had to
        # leave the replaced bundle outside the Trash, and that notice has to
        # reach the user through this command's own result.
        runner = _build_runner_app(config)
    # A rebuilt Runner may have had to leave the replaced bundle in the runtime
    # directory when the Trash was unavailable, and says so only through
    # ``previous_runner``.  That notice used to travel with the success result
    # alone, so every later failure dropped it and the user never learned that an
    # old Runner is still sitting in the runtime directory.  Take it once here
    # and append it to every error raised from this point on.
    runner_notice = runner.get("previous_runner")
    notice_suffix = f"；旧 Runner 提示：{runner_notice}" if runner_notice else ""
    try:
        rendered = _render_launchagent(config)
        lint = subprocess.run(["/usr/bin/plutil", "-lint", str(rendered)], text=True, capture_output=True, check=False)
        if lint.returncode != 0:
            raise RuntimeError(f"{lint.stderr or lint.stdout}{notice_suffix}")
        destination = Path.home() / "Library" / "LaunchAgents" / rendered.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        # The previous LaunchAgent is kept in memory only, for rolling back this
        # invocation; no backup file is written (user rule, B03).
        backup: Optional[bytes] = destination.read_bytes() if destination.exists() else None
    except Exception as exc:
        # The Runner notice must survive these early failures too: a rerun finds
        # the rebuilt Runner valid and never reports it again (round 33).
        if not notice_suffix or notice_suffix in str(exc):
            raise
        raise RuntimeError(f"准备服务配置失败：{exc}{notice_suffix}") from exc
    domain = f"gui/{os.getuid()}"
    label = "com.xiaoba.agentos"
    loaded = _launchctl_print(domain, label).returncode == 0
    if loaded:
        stopped = subprocess.run(
            ["/bin/launchctl", "bootout", f"{domain}/{label}"],
            text=True,
            capture_output=True,
            check=False,
        )
        if stopped.returncode != 0:
            raise RuntimeError(f"旧 LaunchAgent 无法停止，未覆盖：{stopped.stderr or stopped.stdout}{notice_suffix}")
        if not _wait_service_unloaded(domain, label):
            raise RuntimeError(f"旧 LaunchAgent 在 10 秒内未完全退出，未覆盖；请稍后重试{notice_suffix}")
    try:
        database_backup = _prepare_database_for_install(config, store, backup_database)
    except Exception as exc:
        restart_note = _RESTART_FAILED if _restart_existing_service(domain, destination, loaded) is False else ""
        # Re-raise the original exception, type and text intact, with the Runner
        # notice appended: this path is a refusal or an OSError from the database
        # step, and rewriting it into a RuntimeError would hide what went wrong.
        if not notice_suffix and not restart_note:
            raise
        if len(exc.args) == 1 and isinstance(exc.args[0], str):
            exc.args = (exc.args[0] + restart_note + notice_suffix,)
            raise
        # An OSError carries (errno, strerror): keep it as the cause and say both
        # what failed and where the old Runner is (round-23 review).
        raise RuntimeError(f"数据库准备失败：{exc}{restart_note}{notice_suffix}") from exc
    try:
        store.initialize()
        migrated = doctor(config, store)
        if not _service_ready(config, migrated):
            # Only an authorized migration (it always leaves a backup) may be
            # called one; a current database was just initialized (round-42 review).
            stage = "迁移后" if database_backup else "初始化后（数据库无需迁移）"
            raise RuntimeError(f"{stage} doctor 未通过：{_doctor_failure_reason(migrated)}")
    except Exception as exc:
        if database_backup:
            _restore_database(store, database_backup)
        restart_note = _RESTART_FAILED if _restart_existing_service(domain, destination, loaded) is False else ""
        recovery = "已恢复数据库备份" if database_backup else "本次未创建数据库备份"
        raise RuntimeError(
            f"数据库初始化或校验失败，{recovery}；已执行原服务恢复步骤{restart_note}：{exc}{notice_suffix}"
        ) from exc
    staged = destination.with_suffix(".plist.agentos-new")
    try:
        rotated_logs = _rotate_worker_logs(config)
        shutil.copy2(rendered, staged)
        os.replace(str(staged), str(destination))
    except Exception as exc:
        # The old service is already stopped here: put its plist back from
        # memory, bring it up again, then report (round-23 review).
        # The staged file is a half-written temporary copy of `rendered` (which
        # stays where it is), never an installed configuration: it is cleared,
        # not moved to .runtime/backups. Only an installed new plist that then
        # fails to start is moved aside, by _rollback_service (round-43 review).
        if staged.exists():
            staged.unlink()
        if backup is not None and (not destination.exists() or destination.read_bytes() != backup):
            restored = destination.with_suffix(".plist.agentos-restore")
            restored.write_bytes(backup)
            os.replace(str(restored), str(destination))
        restarted = _restart_existing_service(domain, destination, loaded)
        if restarted is False:
            restart_text = "已写回原服务配置" + _RESTART_FAILED
        elif restarted is None:
            restart_text = "已写回原服务配置（原服务此前未加载，未重启）"
        else:
            restart_text = "已写回原服务配置并重启原服务"
        raise RuntimeError(f"写入新的服务配置前失败，{restart_text}：{exc}{notice_suffix}") from exc
    started = subprocess.run(["/bin/launchctl", "bootstrap", domain, str(destination)], text=True, capture_output=True, check=False)
    if started.returncode != 0:
        failed_copy = _rollback_service(config, destination, backup, domain, label, loaded)
        # The rollback leaves the new plist aside for diagnosis; that path is the
        # user's only way back to it.  The 8-second branch below already reports
        # it, and this branch used to drop it, so say the same thing here.
        kept = f"失败副本：{failed_copy}" if failed_copy else "没有留下失败副本"
        raise RuntimeError(
            f"LaunchAgent 启动失败，已回滚：{started.stderr or started.stdout}；{kept}{notice_suffix}"
        )
    deadline = time.monotonic() + 8
    snapshot = ""
    while time.monotonic() < deadline:
        current = _launchctl_print(domain, label)
        snapshot = current.stdout
        if current.returncode == 0 and "state = running" in snapshot and "pid =" in snapshot:
            runtime = _worker_runtime_status(config)
            if runtime.get("ok"):
                return {
                    "status": "installed_and_running",
                    "path": str(destination),
                    "previous_launchagent_kept_in_memory": backup is not None,
                    # A replaced Runner that could not be trashed is kept in the
                    # runtime directory; the notice from the rebuild travels with
                    # the install result instead of being dropped here.
                    "previous_runner": runner_notice,
                    "database_backup": str(database_backup) if database_backup else None,
                    "runtime": runtime,
                    "rotated_logs": rotated_logs,
                }
        time.sleep(0.5)
    # Sample before rolling back: _rollback_service boots the job out, after
    # which launchctl has nothing left to report.  Take one line per key rather
    # than the first line matching either key, otherwise `state = spawn
    # scheduled` shadows `last exit code = 78: EX_CONFIG` and the only clue
    # that distinguishes a launchd-level spawn failure from a Runner-level one
    # is silently dropped.
    final = _launchctl_print(domain, label).stdout or snapshot
    final_lines = [line.strip() for line in final.splitlines()]

    def _first(key: str) -> Optional[str]:
        return next((line for line in final_lines if line.startswith(key)), None)

    state = "；".join(
        fact for fact in (_first("state ="), _first("pid ="), _first("last exit code =")) if fact
    ) or "no launchd state"
    failed_copy = _rollback_service(config, destination, backup, domain, label, loaded)
    error_log = config.runtime_dir / "logs" / "worker.err.log"
    diagnostic = (
        error_log.read_text(encoding="utf-8", errors="replace")[-4000:] if error_log.is_file() else ""
    )
    hint = ""
    if ("can't open input file" in diagnostic or "Operation not permitted" in diagnostic) and "/Documents/" in diagnostic:
        hint = "；macOS 隐私控制拒绝 Runner 读取 Documents，请在完全磁盘访问权限中启用“小八 AgentOS Runner”"
    elif "ModuleNotFoundError" in diagnostic:
        hint = "；后台 Python 未找到 AgentOS 模块，请检查服务入口与安装目录"
    elif not diagnostic.strip() and _first("last exit code ="):
        hint = "；worker.err.log 为空说明进程在写下任何日志前就退出了，该退出码来自 launchd/xpcproxy 而非 Runner 自身"
    kept = f"失败副本：{failed_copy}" if failed_copy else "没有留下失败副本"
    raise RuntimeError(f"LaunchAgent 未保持运行，已回滚（{state}{hint}；{kept}）{notice_suffix}")



from .cli_groups.parser_spec import (
    CLI_COMMAND_GROUPS,
    RETIRED_COMMANDS,
    _GroupedHelpFormatter,
    _RETIRED_COMMAND_NOTICE,
    build_parser,
    format_grouped_commands_epilog,
    retired_command_message,
)
from .cli_groups.daily_ops import dispatch_daily_and_tasks
from .cli_groups.maintenance_ops import dispatch_maintenance_and_ops
from .cli_groups.memory_ops import dispatch_memory_and_review



def _content_index_command(config: AppConfig, args: Any) -> int:
    """内容的发布动作。价值文件直接重建索引；技能走草稿、审查、发布三步，正式位置的技能不就地改。"""
    from . import content_index

    try:
        if args.command == "knowledge-index-rebuild":
            index = content_index.build_knowledge_index(config.knowledge_index.parent)
            content_index.write_index(config.knowledge_index, index)
            _print({"ok": True, "index": str(config.knowledge_index), "entries": len(index["entries"])}, True)
            return _content_notes_sync(config, False)
        override = None
        if args.entry is not None:
            try:
                override = json.loads(args.entry.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise content_index.ContentIndexError(f"技能条目文件不可读：{exc}") from exc
            if not isinstance(override, dict):
                raise content_index.ContentIndexError("技能条目文件必须是一个 JSON 对象")
        entry = content_index.draft_entry(config.skills_root, args.skill_id, args.draft, override)
        prompt = content_index.review_prompt(entry, args.draft)
        if args.command == "skill-review-prompt":
            print(prompt, end="")
            return 0
        from .pool_dispatch import active_names
        reviewers = active_names(config, purpose="review")
        problems = content_index.review_problems(config.database_path, args.review_task, prompt, reviewers)
        result = {"ok": not problems, "skill": args.skill_id, "review_task": args.review_task, "index": str(config.skills_index)}
        if problems:
            result.update(status="blocked", problems=problems)
            _print(result, True)
            return 1
        result["changes"] = content_index.publish_skill(config.skills_root, config.skills_index, entry, args.draft, args.review_task)
        _print(result, True)
        return _content_notes_sync(config, False)
    except (content_index.ContentIndexError, SkillRegistryError, OSError) as exc:
        _print({"ok": False, "status": "blocked", "error": str(exc)}, True)
        return 1


def _content_notes_sync(config: AppConfig, verify_only: bool) -> int:
    """索引是事实，笔记是它的派生：两份索引各自自检通过后才生成笔记。"""
    from . import content_notes

    skills = SkillRegistry(config).verify()
    knowledge = KnowledgeCatalog(config).verify()
    if not skills["ok"] or not knowledge["ok"]:
        _print({"ok": False, "status": "blocked", "error": "技能索引或价值文件索引自检未通过，不生成笔记",
                "problems": list(skills["problems"]) + list(knowledge["problems"])}, True)
        return 1
    vault = Path(str(config.memory["obsidian_vault"])).expanduser().resolve()
    try:
        notes = content_notes.derived_notes(config.skills_index, config.skills_root, config.knowledge_index)
        planned = content_notes.plan(vault, notes)
    except content_notes.ContentNotesError as exc:
        _print({"ok": False, "status": "blocked", "error": str(exc)}, True)
        return 1
    result = {"ok": planned["ok"], "in_sync": planned["in_sync"], "notes": len(notes),
              "pending": [item["path"] for item in planned["writes"]], "problems": planned["problems"], "written": []}
    if verify_only or not planned["ok"] or planned["in_sync"]:
        _print(result, True)
        return 0 if planned["in_sync"] else 1
    previous = {item["path"]: (vault / item["path"]).read_bytes() if (vault / item["path"]).is_file() else None
                for item in planned["writes"]}
    try:
        result["written"] = content_notes.apply(vault, planned)
        MemoryBridge(config)._rebuild_and_verify_index(vault)
    except (RuntimeError, OSError) as exc:
        # 写到一半出错或索引重建不过，就把笔记放回原样（原内容只在本进程内存里，不落盘）。
        for path, data in previous.items():
            target = vault / path
            if data is None:
                target.unlink(missing_ok=True)
            elif not target.is_file() or target.read_bytes() != data:
                target.write_bytes(data)
        result.update(ok=False, written=[], error=f"笔记写入或索引重建失败，已恢复笔记原样：{exc}")
        _print(result, True)
        return 1
    result.update(in_sync=True, pending=[])
    _print(result, True)
    return 0



def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in RETIRED_COMMANDS:
        # Before any config load or database open: a retired command must not
        # touch state on its way to saying no.
        print(retired_command_message(args.command), file=sys.stderr)
        return 2
    if args.command in {"capability-verify", "handoff-status", "receipt-log"}:
        try:
            if args.command == "capability-verify":
                from .capabilities import CapabilityRegistry
                registry = CapabilityRegistry()
                _print({"ok": True, "tokens": registry.tokens()}, True)
            elif args.command == "handoff-status":
                from .handoff import read_handoff
                _print({"ok": True, "completed_reverified": False, "handoff": read_handoff(Path(args.path))}, True)
            else:
                config = AppConfig.load(Path(args.config))
                if Path(args.task_id).name != args.task_id or args.task_id in {".", ".."}:
                    raise ValueError("unsafe task id")
                paths = sorted((config.runtime_dir / "receipts" / args.task_id).glob("*.json"))
                _print({"task_id": args.task_id, "receipts": [json.loads(p.read_text()) for p in paths]}, True)
            return 0
        except (RuntimeError, ValueError, OSError) as exc:
            _print({"ok": False, "error": str(exc)}, True)
            return 1
    try:
        read_only_or_managed = args.command in {
            "doctor", "status", "show", "install-service",
            "skill-verify", "skill-list", "skill-match",
            "knowledge-verify", "knowledge-list", "knowledge-retrieve", "content-notes-sync",
            "skill-review-prompt", "skill-publish", "knowledge-index-rebuild",
            "memory-retrieve", "memory-retained-list", "memory-retained-resolve",
            "memory-value-list", "memory-value-dispatch", "repair-status", "workday-read",
            "review-list", "review-packet", "memory-trial-status", "memory-trial-match", "review-ui-install",
            "memory-pending-index", "memory-pending-read", "memory-application-record", "memory-delivery-record", "task-result-register", "task-delivery", "task-delivery-view", "memory-validation-basis-propose", "memory-validation-basis-revoke", "memory-revision-submit", "memory-revision-resubmit", "memory-formal-call",
        }
        read_only_or_managed = read_only_or_managed or (args.command == "intake" and bool(args.delivery_receipt))
        config, store = _load(args.config, initialize=not read_only_or_managed)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    cli_mod = sys.modules[__name__]
    for handler in (
        dispatch_memory_and_review,
        dispatch_maintenance_and_ops,
        dispatch_daily_and_tasks,
    ):
        code = handler(cli_mod, config, store, args)
        if code is not None:
            return code
    return 2



if __name__ == "__main__":
    raise SystemExit(main())
