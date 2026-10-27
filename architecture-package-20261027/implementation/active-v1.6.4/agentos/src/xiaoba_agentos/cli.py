from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import retained as retained_objects
from .config import AgentConfig, AppConfig, ConfigError
from .bridge import UnixBridge, dispatch
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
from .skills import SkillRegistry, SkillRegistryError
from .task_context import freeze_context, read_context_file
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
    workflow: Optional[dict] = None,
    experience_kind: Optional[str] = None,
    problem_key: str = "",
    experience_trial: Optional[dict] = None,
) -> str:
    kind = experience_kind or ("audit" if prompt.startswith("你是小八 AgentOS 的独立审查者之一，只读审查技能 ") else "method")
    from .experience_workflow import KINDS, match_trials
    if kind not in KINDS:
        raise ValueError("invalid experience kind")
    frozen_context = freeze_context(task_context)
    enabled_names = [agent.name for agent in config.agents if agent.enabled]
    if kind == "audit":
        assignment = {"hits": [], "contexts": {}, "receipt": None}
        hits, context = [], ""
        skills = {"selected": [], "context": "", "context_sha256": hashlib.sha256(b"").hexdigest()}
    else:
        memory = MemoryBridge(config, store)
        assignment = memory.agent_contexts(prompt, enabled_names)
        hits = assignment["hits"]
        context = memory.render_hits(hits)
        skills = SkillRegistry(config).select_context(prompt)
    snapshot = {
        "prompt": prompt,
        "task_context": frozen_context,
        "required_capabilities": list(required_capabilities),
        "needs_web": needs_web,
        "release_expected_version": config.expected_release_version,
        "source_manifest_sha256": _manifest_fingerprint(config.project_dir),
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
    }
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
    snapshot["trial_suggestions"] = match_trials(store, problem_key) if kind != "audit" else []
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
    for hit in hits:
        source_task_id = hit.get("source_task_id")
        if source_task_id and source_task_id != task_id:
            store.record_memory_reuse(task_id, str(source_task_id), str(hit.get("path") or ""))
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
        if target.exists():
            backup_dir = config.runtime_dir / "backups" / "runner"
            _secure_directory(backup_dir)
            backup = backup_dir / f"{RUNNER_APP_NAME}.{time.time_ns()}"
            shutil.move(str(target), str(backup))
        try:
            os.replace(str(staged), str(target))
        except Exception:
            if backup and backup.exists() and not target.exists():
                shutil.move(str(backup), str(target))
            raise
    result = _verify_runner_app(target)
    if not result["ok"]:
        raise RuntimeError(f"安装后的 Runner 验证失败：{result}")
    check_environment = os.environ.copy()
    check_environment.update(
        {
            "XIAOBA_PROJECT_DIR": str(config.project_dir),
            "XIAOBA_CONFIG": str(config.path),
            "XIAOBA_RUNNER_CHECK_ONLY": "1",
        }
    )
    check = subprocess.run(
        [result["executable"]], text=True, capture_output=True, timeout=10, check=False, env=check_environment
    )
    if check.returncode != 0:
        raise RuntimeError(f"Runner 自检失败：{check.stderr or check.stdout}")
    result.update({"path": str(target), "backup": str(backup) if backup else None, "self_check": check.stdout.strip()})
    return result


def _set_agents(config: AppConfig, names: list, enabled: bool) -> Dict[str, Any]:
    known = {item["name"] for item in config.raw.get("agents", [])}
    unknown = sorted(set(names) - known)
    if unknown:
        raise ConfigError("未知 Agent：" + ", ".join(unknown))
    backup = config.runtime_dir / "backups" / "config" / f"agentos-{int(time.time())}.json"
    _secure_directory(config.runtime_dir)
    _secure_directory(backup.parent)
    shutil.copy2(config.path, backup)
    raw = json.loads(json.dumps(config.raw))
    for item in raw.get("agents", []):
        if item["name"] in names:
            item["enabled"] = enabled
    temporary = config.path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(config.path))
    return {"updated": names, "enabled": enabled, "backup": str(backup)}


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
    backup: Optional[Path],
    domain: str,
    label: str,
    was_loaded: bool,
) -> str:
    current = _launchctl_print(domain, label)
    if current.returncode == 0:
        subprocess.run(
            ["/bin/launchctl", "bootout", f"{domain}/{label}"],
            text=True,
            capture_output=True,
            check=False,
        )
        _wait_service_unloaded(domain, label)
    failed_copy = config.runtime_dir / "backups" / "launchd" / f"failed-install-{time.time_ns()}.plist"
    failed_copy.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.move(str(destination), str(failed_copy))
    if backup and backup.exists():
        shutil.copy2(backup, destination)
        if was_loaded:
            subprocess.run(
                ["/bin/launchctl", "bootstrap", domain, str(destination)],
                text=True,
                capture_output=True,
                check=False,
            )
    return str(failed_copy)


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
    if not store.path.is_file():
        return None
    backup_dir = config.runtime_dir / "backups" / "database"
    _secure_directory(backup_dir)
    target = backup_dir / f"agentos-pre-migration-{time.time_ns()}.sqlite3"
    source_uri = f"file:{store.path}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=30) as source, sqlite3.connect(str(target)) as destination:
        source.backup(destination)
        check = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"数据库备份完整性检查失败：{check}")
    os.chmod(target, 0o600)
    return target


def _restore_database(store: EventStore, backup: Path) -> None:
    staged = store.path.with_suffix(".sqlite3.restore")
    shutil.copy2(backup, staged)
    os.chmod(staged, 0o600)
    os.replace(staged, store.path)


def _restart_existing_service(domain: str, destination: Path, was_loaded: bool) -> None:
    if was_loaded and destination.is_file():
        subprocess.run(
            ["/bin/launchctl", "bootstrap", domain, str(destination)],
            text=True,
            capture_output=True,
            check=False,
        )


def _install_service(config: AppConfig, store: EventStore) -> Dict[str, Any]:
    preflight = doctor(config, store)
    enough_agents = len(preflight.get("enabled_agents", [])) >= int(preflight.get("minimum_successful_agents", 0)) > 0
    base_ready = (
        all(preflight.get("paths", {}).values())
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
        # launchd instead.
        runner = _build_runner_app(config)
    rendered = _render_launchagent(config)
    lint = subprocess.run(["/usr/bin/plutil", "-lint", str(rendered)], text=True, capture_output=True, check=False)
    if lint.returncode != 0:
        raise RuntimeError(lint.stderr or lint.stdout)
    destination = Path.home() / "Library" / "LaunchAgents" / rendered.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup: Optional[Path] = None
    if destination.exists():
        backup = config.runtime_dir / "backups" / "launchd" / f"{destination.stem}-{time.time_ns()}.plist"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(destination, backup)
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
            raise RuntimeError(f"旧 LaunchAgent 无法停止，未覆盖：{stopped.stderr or stopped.stdout}")
        if not _wait_service_unloaded(domain, label):
            raise RuntimeError("旧 LaunchAgent 在 10 秒内未完全退出，未覆盖；请稍后重试")
    database_backup = _backup_database(config, store)
    try:
        store.initialize()
        migrated = doctor(config, store)
        if not migrated.get("ready_for_external_tasks"):
            raise RuntimeError(f"迁移后 doctor 未通过：{migrated.get('database_schema')}")
    except Exception as exc:
        if database_backup:
            _restore_database(store, database_backup)
        _restart_existing_service(domain, destination, loaded)
        raise RuntimeError(f"数据库迁移失败，已恢复备份：{exc}") from exc
    rotated_logs = _rotate_worker_logs(config)
    staged = destination.with_suffix(".plist.agentos-new")
    shutil.copy2(rendered, staged)
    os.replace(str(staged), str(destination))
    started = subprocess.run(["/bin/launchctl", "bootstrap", domain, str(destination)], text=True, capture_output=True, check=False)
    if started.returncode != 0:
        failed_copy = _rollback_service(config, destination, backup, domain, label, loaded)
        raise RuntimeError(f"LaunchAgent 启动失败，已回滚：{started.stderr or started.stdout}")
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
                    "backup": str(backup) if backup else None,
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
    raise RuntimeError(f"LaunchAgent 未保持运行，已回滚（{state}{hint}；失败副本：{failed_copy}）")


# Strategy evolution was retired by the user on 2026-10-11. The seven commands
# it owned are gone; they stay in the parser only so an existing script gets a
# plain "retired" answer instead of argparse's generic "invalid choice".
RETIRED_COMMANDS = {
    "evolve": "已退役：策略进化（2026-10-11 用户裁决）",
    "eval": "已退役：策略进化（2026-10-11 用户裁决）",
    "replay": "已退役：策略进化（2026-10-11 用户裁决）",
    "regression": "已退役：策略进化（2026-10-11 用户裁决）",
    "judge-baseline": "已退役：策略进化（2026-10-11 用户裁决）",
    "promote": "已退役：策略进化（2026-10-11 用户裁决）",
    "rollback": "已退役：策略进化（2026-10-11 用户裁决）",
}

_RETIRED_COMMAND_NOTICE = (
    "命令 {name} 已退役：策略进化于 2026-10-11 由用户裁决退役，{name} 不再存在，"
    "本次没有执行任何操作，也没有改动数据库。原 24 条评估用例已改为离线测试，"
    "每次全量测试都会跑（tests/test_offline_judge_cases.py）。"
)


def retired_command_message(name: str) -> str:
    return _RETIRED_COMMAND_NOTICE.format(name=name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentos", description="小八 AgentOS 控制平面")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("capability-verify")
    handoff_parser = sub.add_parser("handoff-status")
    handoff_parser.add_argument("path", nargs="?", default=str(PROJECT_DIR / "HANDOFF.json"))
    receipt_parser = sub.add_parser("receipt-log")
    receipt_parser.add_argument("task_id")
    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.add_argument("--entity-root", type=Path, help="仅候选只读验收：显式共享实体根（默认核验正式部署位置；执行入口不接受该参数）")
    session_start = sub.add_parser("session-start")
    session_start.add_argument("--timezone", default="Asia/Shanghai")
    task_resume = sub.add_parser("task-resume")
    task_resume.add_argument("task_id")
    task_resume.add_argument("--freeze-hash", required=True)
    repair_status = sub.add_parser("repair-status")
    repair_status.add_argument("--limit", type=int, default=50)
    session_close = sub.add_parser("session-close")
    session_close.add_argument("--session-id")
    intake = sub.add_parser("intake")
    intake.add_argument("prompt")
    intake.add_argument("--message-id")
    intake.add_argument("--active-task-id")
    intake.add_argument("--relation", choices=("auto", "related", "unrelated"), default="auto")
    intake.add_argument("--risk", choices=("auto", "low", "high"), default="auto")
    intake.add_argument("--requires-action", action="store_true")
    intake.add_argument("--memory-worthy", action="store_true")
    intake.add_argument("--explicit-agentos", action="store_true")
    intake.add_argument("--force-route", choices=(DIRECT, AGENTOS_MULTI, REVIEW, ASK_USER))
    intake.add_argument("--submit", action="store_true")
    intake.add_argument(
        "--queue-when-blocked", action="store_true",
        help="后台当前无法执行时仍然入队等待（默认拒绝入队并说明原因）",
    )
    intake.add_argument("--priority", type=int, default=0)
    submit = sub.add_parser("submit")
    submit.add_argument("prompt")
    submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--memory-kind", choices=("audit", "rule", "method"))
    submit.add_argument("--problem-key", default="")
    trials = sub.add_parser("memory-trial-run", help="在已授权任务中执行本机隔离测试并提交独立双审；不自动批准")
    trials.add_argument("task_id")
    trials.add_argument("--card", required=True)
    ui_install = sub.add_parser("review-ui-install", help="用户终端：先只读核对，再安装源清单绑定的裁决页面适配")
    ui_mode = ui_install.add_mutually_exclusive_group(required=True)
    ui_mode.add_argument("--plan", action="store_true")
    ui_mode.add_argument("--apply", action="store_true")
    trial_status = sub.add_parser("memory-trial-status")
    trial_status.add_argument("task_id")
    trial_match = sub.add_parser("memory-trial-match", help="只建议同类试验，不执行候选命令")
    match_selector = trial_match.add_mutually_exclusive_group(required=True)
    match_selector.add_argument("--problem-key")
    match_selector.add_argument("--query", help="只按任务标题/原问题关键词建议，不读取候选正文")
    for name in ("collaborate", "collaborate-review"):
        command = sub.add_parser(name)
        command.add_argument("prompt")
        command.add_argument("--context-file", required=True)
        command.add_argument("--message-id", required=True)
        if name == "collaborate":
            command.add_argument("--assignments-file", required=True)
        else:
            command.add_argument("--work-task-id", required=True)
            command.add_argument("--artifact", action="append", required=True)
    for command in (submit, intake):
        command.add_argument("--requires-capability", action="append", default=[])
        command.add_argument("--needs-web", action="store_true")
        command.add_argument("--context-file", help="UTF-8 task summary and sourced evidence, max 64 KiB")
    run = sub.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--max-idle-polls", type=int, default=0)
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    status.add_argument("--limit", type=int, default=20)
    show = sub.add_parser("show")
    show.add_argument("task_id")
    review_list = sub.add_parser("review-list")
    review_list.add_argument("--stage", choices=("delivery", "final", "all"), default="all")
    review_list.add_argument("--date")
    review = sub.add_parser("review")
    review.add_argument("task_id")
    review.add_argument("--stage", choices=("delivery", "final"), required=True)
    review.add_argument("--decision", choices=("approve", "reject", "revise", "retire"), required=True)
    review.add_argument("--note", default="")
    hold_release = sub.add_parser("constraint-hold-release")
    hold_release.add_argument("task_id")
    memory_retire = sub.add_parser("memory-retire")
    memory_retire.add_argument("task_id")
    memory_retire.add_argument("--reason", required=True)
    memory_rename = sub.add_parser(
        "memory-rename-notes",
        help="把按整串编号命名的已发布经验笔记改成可读名字（标题 + 编号前 8 位）",
    )
    memory_rename.add_argument("--dry-run", action="store_true", help="只列出会怎么改，不动任何文件")
    memory_receipt = sub.add_parser("memory-receipt")
    memory_receipt.add_argument("task_id")
    for name in sorted(RETIRED_COMMANDS):
        # Registered only so an old script or habit gets a plain answer instead
        # of argparse's "invalid choice". Every argument is swallowed and the
        # command can never do anything.
        retired = sub.add_parser(name, add_help=False, help=RETIRED_COMMANDS[name])
        retired.add_argument("rest", nargs=argparse.REMAINDER)
    bridge = sub.add_parser("bridge")
    bridge.add_argument("--socket", default=None)
    authored = sub.add_parser(
        "memory-candidate-create",
        help="登记一条由宿主撰写的经验候选，进入正常的初审队列（不发布）",
    )
    authored.add_argument("--title", required=True)
    authored.add_argument("--body-file", required=True)
    authored.add_argument("--author", default="host_assistant")
    authored.add_argument("--source-task-id", action="append", default=[])
    authored.add_argument("--idempotency-key", default=None)
    authored.add_argument("--kind", choices=("rule", "method"), default="method")
    authored.add_argument("--problem-key", default="")
    review_packet = sub.add_parser("review-packet")
    review_packet.add_argument("path", help="JSON 裁决包路径，使用 - 从标准输入读取")
    publish_memory = sub.add_parser("publish-memory")
    publish_memory.add_argument("--task-id")
    sub.add_parser("install-plan")
    sub.add_parser("install-runner")
    sub.add_parser("install-service")
    enable = sub.add_parser("agent-enable")
    enable.add_argument("names", nargs="+")
    disable = sub.add_parser("agent-disable")
    disable.add_argument("names", nargs="+")
    skill_verify = sub.add_parser("skill-verify")
    skill_verify.add_argument("--entity-root", type=Path, help="仅候选只读验收：显式共享实体根（默认核验正式部署位置；执行入口不接受该参数）")
    skill_list = sub.add_parser("skill-list")
    skill_list.add_argument("--entity-root", type=Path, help="仅候选只读验收：显式共享实体根（默认核验正式部署位置；执行入口不接受该参数）")
    skill_match = sub.add_parser("skill-match")
    skill_match.add_argument("prompt")
    skill_match.add_argument("--entity-root", type=Path, help="仅候选只读验收：显式共享实体根（默认核验正式部署位置；执行入口不接受该参数）")
    sub.add_parser("knowledge-verify")
    content_sync = sub.add_parser(
        "content-notes-sync",
        help="按技能索引和价值文件索引更新第二大脑里的技能卡与价值文件索引笔记；--verify 只检查不写",
    )
    content_sync.add_argument("--verify", action="store_true")
    for name, text in (("skill-review-prompt", "为草稿目录里的一个技能生成审查说明，供提交给两个后台"),
                       ("skill-publish", "技能改动的发布动作：核对审查任务后把草稿装到正式位置、写索引并同步技能卡")):
        skill_command = sub.add_parser(name, help=text)
        skill_command.add_argument("skill_id")
        skill_command.add_argument("--draft", type=Path, required=True, help="放着这个技能全部文件的草稿目录（不是正式技能目录）")
        skill_command.add_argument("--entry", type=Path, help="技能条目 JSON（触发词、说明等）；只在新增技能或改条目时需要")
        if name == "skill-publish":
            skill_command.add_argument("--review-task", required=True, help="两个后台都通过的审查任务编号")
    sub.add_parser("knowledge-index-rebuild", help="价值文件改动的发布动作：重建索引、自检并同步笔记")
    sub.add_parser("knowledge-list")
    knowledge_retrieve = sub.add_parser("knowledge-retrieve")
    knowledge_retrieve.add_argument("query")
    memory_retrieve = sub.add_parser("memory-retrieve")
    memory_retrieve.add_argument("query")
    retained_list = sub.add_parser(
        "memory-retained-list",
        help="list every retained original object the self-check can still find",
    )
    retained_list.add_argument(
        "--write-report",
        action="store_true",
        help="also write the self-check report next to the retention journal",
    )
    retained_list.add_argument(
        "--show-resolved",
        action="store_true",
        help="also show the objects the user already handled (history only)",
    )
    retained_resolve = sub.add_parser(
        "memory-retained-resolve",
        help="handle exactly one retained object named on the command line (user-run)",
    )
    retained_resolve.add_argument(
        "retained_path",
        help="the exact retained path from memory-retained-list, one object only",
    )
    retained_action = retained_resolve.add_mutually_exclusive_group(required=True)
    retained_action.add_argument(
        "--trash",
        action="store_true",
        help="move that one object into the system trash, keeping the object itself",
    )
    retained_action.add_argument(
        "--confirm-deleted",
        dest="confirm_deleted",
        action="store_true",
        help="record that you deleted that object yourself; refuses if it is still there",
    )
    retained_resolve.add_argument(
        "--expect-dev",
        dest="expect_dev",
        type=int,
        default=None,
        help=(
            "the device number memory-retained-list reports for this path; only needed when the "
            "discovery record carries no identity, and it can never override the record"
        ),
    )
    retained_resolve.add_argument(
        "--expect-ino",
        dest="expect_ino",
        type=int,
        default=None,
        help=(
            "the inode number memory-retained-list reports for this path; the object at the path "
            "must carry it or the command refuses to move anything"
        ),
    )
    demo = sub.add_parser("demo")
    # 策略进化的那半圈循环已随功能退役，默认题面只说剩下的一半。
    demo.add_argument("--prompt", default="验证小八 AgentOS 的生产循环")
    return parser


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
        reviewers = [agent.name for agent in config.agents if agent.enabled]
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
            "review-list", "memory-trial-status", "memory-trial-match", "review-ui-install",
        }
        config, store = _load(args.config, initialize=not read_only_or_managed)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.command == "init":
        _secure_directory(config.runtime_dir)
        _secure_directory(config.runtime_dir / "logs")
        _print({"status": "initialized", "database": str(config.database_path), "event_chain": store.verify_event_chain()})
        return 0
    if args.command == "doctor":
        result = doctor(config, store, entity_root=args.entity_root)
        _print(result, True)
        return 0 if result["ok"] else 1
    if args.command == "session-start":
        local_health = doctor(config, store)
        if not local_health.get("ok"):
            _print(
                {
                    "status": "blocked_local_health",
                    "ready_for_external_tasks": False,
                    "doctor": local_health,
                },
                True,
            )
            return 1
        try:
            session = store.start_session(args.timezone)
        except ValueError as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 2
        probe = start_session_health(config, store)
        notifications = store.pending_notifications(acknowledge=True)
        # A pending manual review must reach the user even on a fully successful
        # session start, so the reminder rides along with the success payload.  The
        # live probe above still runs in full: the reminder is never a reason to
        # skip a real background probe.
        retained = local_health.get("retained_objects") or {}
        _print(
            {
                "status": "ready" if probe["ok"] else probe["session_mode"],
                "session": session,
                "doctor_ok": True,
                "live_probe": probe,
                "notifications": notifications,
                "retained_manual_review": {
                    "required": bool(retained.get("manual_action_required")),
                    "pending_count": int(retained.get("pending_count") or 0),
                    "resolved_count": int(retained.get("resolved_count") or 0),
                    "pending_move_intent_count": int(
                        retained.get("pending_move_intent_count") or 0
                    ),
                    "list_command": "memory-retained-list",
                    "resolve_command": (
                        "memory-retained-resolve <retained_path> --trash|--confirm-deleted "
                        "[--expect-dev <dev> --expect-ino <ino>]"
                    ),
                    "enumeration_ok": bool(retained.get("enumeration_ok", False)),
                    "note": (
                        "有文件待人工核对：请亲自查看 memory-retained-list；"
                        "这一项不阻断本次会话或接单，但仍需你逐个核对保留物"
                    ),
                },
                "ready_for_external_tasks": bool(probe["ok"]),
            },
            True,
        )
        return 0 if probe["ok"] else 1
    if args.command == "task-resume":
        try:
            result = store.resume_task(
                args.task_id,
                args.freeze_hash,
                receipt_root=config.runtime_dir / "receipts",
                enabled_names=[agent.name for agent in config.agents if agent.enabled],
                health_max_age_seconds=float(
                    config.worker.get("health_max_age_seconds", 86400)
                ),
            )
        except (KeyError, ValueError) as exc:
            _print({"status": "blocked", "error": str(exc), "task_id": args.task_id}, True)
            return 1
        _print(result, True)
        return 0
    if args.command == "repair-status":
        _print(
            {
                **_health_report(config, store),
                # Peer auto-repair is retired: these rows are history, and no
                # poll leases, retries or terminates them any more.
                "peer_auto_repair": "retired",
                "repair_jobs": store.repair_jobs(args.limit),
                "notifications": store.pending_notifications(acknowledge=False),
            },
            True,
        )
        return 0
    if args.command == "session-close":
        session_id = args.session_id
        if not session_id:
            active = store.active_session()
            session_id = str(active["id"]) if active else None
        if not session_id:
            _print({"status": "no_active_session", "tasks": [], "pending_final_reviews": 0}, True)
            return 0
        try:
            packet = store.session_review_packet(session_id)
        except KeyError:
            _print({"status": "blocked", "error": "session not found"}, True)
            return 1
        packet["status"] = (
            "closed" if packet["session"].get("status") == "closed"
            else "awaiting_final_review" if packet["pending_final_reviews"]
            else "closing_without_final_reviews"
        )
        packet["answer_provenance"] = "agentos_review"
        _print(packet, True)
        return 0
    if args.command == "intake":
        try:
            decision = decide_route(
                args.prompt,
                active_task_id=args.active_task_id,
                relation=args.relation,
                risk=args.risk,
                requires_action=args.requires_action or args.needs_web or bool(args.requires_capability),
                memory_worthy=args.memory_worthy,
                explicit_agentos=args.explicit_agentos,
                force_route=args.force_route,
            )
        except ValueError as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 2
        session = store.active_session() or store.start_session()
        signals = {
            "relation": args.relation,
            "risk": args.risk,
            "requires_action": args.requires_action,
            "memory_worthy": args.memory_worthy,
            "explicit_agentos": args.explicit_agentos,
            "forced": args.force_route is not None,
        }
        try:
            route = store.record_route(
                prompt=args.prompt,
                route=decision.route,
                reason_code=decision.reason_code,
                confidence=decision.confidence,
                signals=signals,
                session_id=str(session["id"]),
                message_id=args.message_id,
                active_task_id=args.active_task_id,
            )
        except ValueError as exc:
            _print({"status": "conflict", "error": str(exc)}, True)
            return 2
        response = {
            "status": "routed",
            "route_id": route["id"],
            "route": route["route"],
            "reason_code": route["reason_code"],
            "confidence": route["confidence"],
            "session_id": session["id"],
            "answer_provenance": _answer_provenance(str(route["route"])),
            "response_label": (
                "本回答：会话模型直接回答"
                if route["route"] == DIRECT
                else adjudicated_label(len([agent for agent in config.agents if agent.enabled]))
                if route["route"] == AGENTOS_MULTI
                else "本回答：小八 AgentOS 用户审核流程"
                if route["route"] == REVIEW
                else "需要用户补充任务范围"
            ),
        }
        blockers = execution_blockers(config, store) if args.submit and route["route"] == AGENTOS_MULTI else []
        if blockers and not args.queue_when_blocked:
            response.update({
                "status": "blocked_local_health",
                "submission": "not_submitted_execution_blocked",
                "blockers": blockers,
                "hint": "后台当前无法执行，任务未入队。修复后重新提交，或加 --queue-when-blocked 明确要求先入队等待。",
            })
            _print(response, True)
            return 1
        if args.submit and route["route"] == AGENTOS_MULTI:
            try:
                task_id = _submit_task(
                    config,
                    store,
                    args.prompt,
                    args.priority,
                    session_id=str(session["id"]),
                    route_decision_id=str(route["id"]),
                    idempotency_key=f"intake:{route['id']}",
                    required_capabilities=tuple(args.requires_capability), needs_web=args.needs_web,
                task_context=read_context_file(args.context_file),
                )
            except (SkillRegistryError, ValueError, OSError) as exc:
                _print({"status": "blocked", "error": str(exc), **response}, True)
                return 1
            response.update({"status": "pending", "task_id": task_id})
            if blockers:
                response.update({"execution_blocked": True, "blockers": blockers})
        elif args.submit:
            response["submission"] = "not_submitted_for_selected_route"
        _print(response, True)
        return 0
    if args.command == "skill-verify":
        result = SkillRegistry(config, entity_root=args.entity_root).verify()
        _print(result, True)
        return 0 if result["ok"] else 1
    if args.command == "skill-list":
        try:
            _print({"skills": SkillRegistry(config, entity_root=args.entity_root).list()}, True)
            return 0
        except SkillRegistryError as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 1
    if args.command == "skill-match":
        try:
            _print({"prompt": args.prompt, "matches": SkillRegistry(config, entity_root=args.entity_root).match(args.prompt)}, True)
            return 0
        except SkillRegistryError as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 1
    if args.command == "content-notes-sync":
        return _content_notes_sync(config, args.verify)
    if args.command in ("skill-review-prompt", "skill-publish", "knowledge-index-rebuild"):
        return _content_index_command(config, args)
    if args.command == "knowledge-verify":
        result = KnowledgeCatalog(config).verify()
        _print(result, True)
        return 0 if result["ok"] else 1
    if args.command == "knowledge-list":
        try:
            _print({"knowledge": KnowledgeCatalog(config).list()}, True)
            return 0
        except RuntimeError as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 1
    if args.command == "knowledge-retrieve":
        receipt = KnowledgeCatalog(config).retrieve_with_receipt(args.query)
        _print(receipt, True)
        return 0 if receipt["retrieval_status"] != "degraded" else 1
    if args.command == "memory-retrieve":
        receipt = MemoryBridge(config, store).retrieve_with_receipt(args.query)
        _print(receipt, True)
        return 0 if receipt["retrieval_status"] != "degraded" else 1
    if args.command == "memory-retained-list":
        # The self-check the user reads before deciding on any cleanup: it
        # enumerates the retained original objects from the journal *and* from a
        # fresh filesystem scan, so it also works from a brand-new process after
        # a restart and never depends on in-memory bookkeeping.
        payload = retained_objects.collect(config)
        if args.write_report:
            report_path, report_error = retained_objects.write_report(config, payload)
            payload["report_written"] = str(report_path) if report_path else None
            if report_error is not None:
                payload["report_error"] = report_error
        if not args.show_resolved:
            # The default view highlights what still needs the user; the objects
            # already handled stay out of it and are shown only when asked for.
            payload["resolved_objects_hidden"] = len(payload.get("resolved_objects") or [])
            payload.pop("resolved_objects", None)
            payload["resolved_objects_note"] = (
                "已处理对象的历史默认隐藏；加 --show-resolved 可显式查看"
            )
        _print(payload, True)
        return 0 if payload["ok"] else 1
    if args.command == "memory-retained-resolve":
        # One object, named on the command line by the user in their own terminal.
        # There is no batch form and no automatic cleanup: this handles exactly the
        # path it was given, and reports the real location on every outcome.
        action = "trash" if args.trash else "confirm_deleted"
        expected_identity = None
        if args.expect_dev is not None or args.expect_ino is not None:
            expected_identity = {
                "dev": args.expect_dev,
                "ino": args.expect_ino,
                "source": "--expect-dev/--expect-ino on the command line",
            }
        outcome = retained_objects.resolve_retained(
            config,
            retained_path=args.retained_path,
            action=action,
            expected_identity=expected_identity,
        )
        _print(outcome, True)
        return 0 if outcome["ok"] else 1
    if args.command in ("collaborate", "collaborate-review"):
        from .workflow import work_plan, review_plan
        try:
            context = read_context_file(args.context_file)
            if args.command == "collaborate":
                plan = work_plan(config, json.loads(Path(args.assignments_file).read_text(encoding="utf-8")), context)
            else:
                plan = review_plan(config, store, args.work_task_id, context, args.artifact)
            session = store.active_session() or store.start_session()
            route = store.record_route(prompt=args.prompt, route=AGENTOS_MULTI,
                reason_code="explicit_collaboration_" + plan["phase"], confidence=1.0,
                signals={"explicit_agentos": True, "workflow_phase": plan["phase"]},
                session_id=str(session["id"]), message_id=args.message_id)
            task_id = _submit_task(config, store, args.prompt, session_id=str(session["id"]),
                route_decision_id=str(route["id"]), idempotency_key="workflow:" + str(route["id"]),
                task_context=context, workflow=plan)
            _print({"status": "pending", "task_id": task_id, "phase": plan["phase"], "route_id": route["id"]}, True)
            return 0
        except (SkillRegistryError, ValueError, OSError, KeyError) as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 1
    if args.command == "review-ui-install":
        from .review_ui_install import run
        try:
            _print(run(config, apply=args.apply), True)
            return 0
        except (OSError, ValueError, RuntimeError) as exc:
            _print({"ok": False, "error": str(exc)}, True)
            return 1
    if args.command in {"memory-trial-run", "memory-trial-status", "memory-trial-match"}:
        from .experience_workflow import execute_trial, prepare_trial, trial_summary, policy, save_execution_receipt
        receipt_path = None
        try:
            if args.command == "memory-trial-match":
                from .experience_workflow import match_trials
                matches = match_trials(store, args.problem_key, args.query)
                _print({"matches": matches, "action": "suggest_only", "normal_retrieval": False}, True)
            elif args.command == "memory-trial-status":
                candidate = store.get_memory_candidate(args.task_id)
                if not candidate:
                    raise ValueError("candidate not found")
                with store.read_only() as conn:
                    _print(trial_summary(conn, candidate), True)
            else:
                card = json.loads(Path(args.card).read_text())
                # Check approval and source bytes before executing; recheck after it.
                prepare_trial(store, args.task_id, card)
                execution = execute_trial(config, card)
                receipt_path = save_execution_receipt(config, args.task_id, card, execution)
                trial, prompt = prepare_trial(store, args.task_id, card, execution)
                session = store.active_session() or store.start_session()
                task_id = _submit_task(config, store, prompt, session_id=str(session["id"]),
                                       experience_kind="audit", experience_trial=trial,
                                       idempotency_key="experience-trial:" + trial["execution_sha256"])
                _print({"source_task_id": args.task_id, "validation_task_id": task_id,
                        "execution_passed": execution["passed"], "execution_receipt": str(receipt_path), "status": "pending_independent_review"}, True)
            return 0
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            _print({"status": "blocked", "error": str(exc), "execution_receipt": str(receipt_path) if receipt_path else None}, True)
            return 1
    if args.command == "submit":
        session = store.active_session() or store.start_session()
        decision = decide_route(args.prompt, explicit_agentos=True)
        route = store.record_route(
            prompt=args.prompt,
            route=decision.route,
            reason_code="explicit_submit_command",
            confidence=1.0,
            signals={"explicit_submit": True},
            session_id=str(session["id"]),
        )
        try:
            task_id = _submit_task(
                config,
                store,
                args.prompt,
                args.priority,
                session_id=str(session["id"]),
                route_decision_id=str(route["id"]),
                required_capabilities=tuple(args.requires_capability), needs_web=args.needs_web,
                task_context=read_context_file(args.context_file),
                experience_kind=args.memory_kind, problem_key=args.problem_key,
            )
        except (SkillRegistryError, ValueError, OSError) as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 1
        _print({
            "task_id": task_id,
            "status": "pending",
            "route_id": route["id"],
            "trial_suggestions": json.loads(store.get_task(task_id)["execution_snapshot_json"]).get("trial_suggestions", []),
            "answer_provenance": "agentos_multi_adjudicated",
        })
        return 0
    if args.command == "status":
        schema = store.schema_status()
        if not schema["ok"]:
            _print({"database_schema": schema, "runtime": _worker_runtime_status(config)}, True)
            return 1
        _print({
            "summary": store.summary(),
            "runtime": _worker_runtime_status(config),
            **_health_report(config, store),
            "repair_jobs": store.repair_jobs(args.limit),
            "notifications": store.pending_notifications(acknowledge=False),
            "tasks": store.list_tasks(args.limit),
        }, True)
        return 0
    if args.command == "show":
        if not store.schema_status()["ok"]:
            print("数据库需要先迁移", file=sys.stderr)
            return 1
        task = store.get_task(args.task_id)
        if not task:
            print("任务不存在", file=sys.stderr)
            return 1
        _print({
            "task": task,
            "runs": store.task_runs(args.task_id),
            "memory_candidate": store.get_memory_candidate(args.task_id),
            "constraint_hold": store.get_constraint_hold(args.task_id),
        }, True)
        return 0
    if args.command == "memory-candidate-create":
        try:
            body = Path(args.body_file).expanduser().read_text(encoding="utf-8")
            active = store.active_session()
            created = store.create_authored_candidate(
                args.title, body, config.runtime_dir / "memory-outbox",
                author=args.author, source_task_ids=args.source_task_id,
                experience_kind=args.kind, problem_key=args.problem_key,
                idempotency_key=args.idempotency_key,
                session_id=str(active["id"]) if active else None,
            )
        except KeyError as exc:
            _print({"status": "blocked", "error": f"source task not found: {exc.args[0]}"}, True)
            return 1
        except (OSError, UnicodeDecodeError, ValueError, sqlite3.Error) as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 1
        _print(created, True)
        return 0
    if args.command == "constraint-hold-release":
        hold = store.get_constraint_hold(args.task_id, include_result=True)
        task = store.get_task(args.task_id)
        if not hold or hold.get("status") != "pending" or not task:
            _print({"status": "blocked", "error": "pending constraint hold not found"}, True)
            return 1
        # A published or retired candidate keeps frozen evidence, and the store
        # can only refuse inside its own transaction, after the staging below has
        # already rewritten the file.  Refuse here first, before anything is
        # written, and keep the previous bytes so a refusal in the race window
        # (another connection finishing the candidate while we stage) can be
        # undone.  The store guard stays as the concurrency backstop.
        frozen = store.terminal_memory_candidate_status(args.task_id)
        if frozen is not None:
            _print({
                "status": "blocked",
                "task_id": args.task_id,
                "error": str(terminal_reregistration_error(args.task_id, frozen)),
                "hold_status": hold.get("status"),
                "retry_safe": False,
            }, True)
            return 1
        from .experience_workflow import policy, complete_audit
        if policy(task)["kind"] == "audit":
            complete_audit(store, args.task_id, str(hold["result"]), release_hold=True)
            _print({"task_id": args.task_id, "status": "released", "memory_disposition": "audit_only"}, True)
            return 0
        bridge = MemoryBridge(config)
        target = bridge.staged_path(args.task_id)
        evidence_snapshot = _read_evidence(target)
        if evidence_snapshot[0] and evidence_snapshot[1] is None:
            # The file exists but its bytes could not be captured.  Writing over
            # it would destroy evidence we could never put back, so stop here.
            _print({
                "status": "blocked",
                "task_id": args.task_id,
                "error": f"cannot snapshot the existing evidence file {target}; refusing to overwrite it",
                "hold_status": hold.get("status"),
                "retry_safe": False,
                "manual_action_required": True,
            }, True)
            return 1
        evidence_sha256 = None
        try:
            # Stage the file while the hold is still pending, then release the
            # hold and queue the candidate in one transaction.
            runs = store.task_runs(args.task_id)
            staged = bridge.stage_experience(
                args.task_id, task["prompt"], str(hold["result"]), runs
            )
            evidence_sha256 = hashlib.sha256(staged.read_bytes()).hexdigest()
            store.release_constraint_hold(args.task_id, str(staged), evidence_sha256)
        except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error) as exc:
            # An exception does not prove a rollback (the store does
            # maintenance after commit); decide from the database.  Three
            # outcomes stay distinct: committed, provably not committed, and
            # unknown.
            readback_error = None
            try:
                after = store.get_constraint_hold(args.task_id)
                candidate = store.get_memory_candidate(args.task_id)
            except (OSError, RuntimeError, sqlite3.Error) as readback_exc:
                after, candidate = None, None
                readback_error = str(readback_exc)
            committed = bool(
                after and after.get("status") == "released" and candidate
                and candidate.get("evidence_sha256") == evidence_sha256
            )
            if not committed and readback_error is not None:
                # The release may have committed and we cannot tell.  Never put
                # the previous bytes back over evidence we cannot prove we
                # overwrote: that is exactly how the database and the file drift
                # apart.  Keep the current evidence and hand it to a human.
                _print({
                    "status": "reconciliation_required",
                    "task_id": args.task_id,
                    "error": str(exc),
                    "readback_error": readback_error,
                    "hold_status": "unknown",
                    "retry_safe": False,
                    "manual_action_required": True,
                }, True)
                return 1
            if not committed:
                # Provably not committed: put the evidence bytes back as they
                # were, and only if they are still this command's own write.  A
                # restore that cannot finish is reported, never called recovered.
                # Under 受限方案丙 the object this command replaced is kept, so a
                # non-empty retained list is reported as manual action too: the
                # live path is back, but the command does not claim that nothing
                # is left behind.
                try:
                    retained_paths = _restore_evidence(
                        target,
                        evidence_snapshot,
                        evidence_sha256,
                        config=config,
                        task_id=args.task_id,
                        operation="evidence.staged",
                    )
                except EvidenceRestoreError as restore_exc:
                    payload = {
                        "status": "reconciliation_required",
                        "task_id": args.task_id,
                        "error": str(exc),
                        "restore_error": str(restore_exc),
                        "hold_status": after.get("status") if after else "unknown",
                        "retry_safe": False,
                        "manual_action_required": True,
                    }
                    retained_paths = list(getattr(restore_exc, "retained", []) or [])
                    if retained_paths:
                        payload["retained_objects"] = retained_paths
                        payload["retained_note"] = RETAINED_NOTE
                    _print(payload, True)
                    return 1
                still_pending = bool(after and after.get("status") == "pending")
                frozen_now = bool(candidate and candidate.get("status") in MEMORY_TERMINAL_STATES)
                payload = {
                    "status": "blocked" if still_pending else "reconciliation_required",
                    "task_id": args.task_id,
                    "error": str(exc),
                    "hold_status": after.get("status") if after else "unknown",
                    "retry_safe": bool(still_pending and not frozen_now),
                }
                if retained_paths:
                    payload["manual_action_required"] = True
                    payload["retained_objects"] = retained_paths
                    payload["retained_note"] = RETAINED_NOTE
                _print(payload, True)
                return 1
        _print({
            "status": "released",
            "task_id": args.task_id,
            "candidate_status": "pending_delivery_review",
            "candidate_sha256": evidence_sha256,
        }, True)
        return 0
    if args.command == "review-list":
        if not store.schema_status()["ok"]:
            print("数据库需要先迁移", file=sys.stderr)
            return 1
        try:
            items = store.review_queue(args.stage, args.date)
        except ValueError as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 2
        _print({"stage": args.stage, "date": args.date, "count": len(items), "items": items}, True)
        return 0
    if args.command == "review":
        code, payload = execute_review(
            config, store, args.task_id, args.stage, args.decision, args.note
        )
        _print(payload, True)
        return code
    if args.command == "memory-rename-notes":
        bridge = MemoryBridge(config)
        renamed, skipped = [], []
        for candidate in store.memory_lifecycle_rows():
            if candidate.get("status") not in {"published", "revalidation_required"}:
                continue
            task_id, recorded = str(candidate["task_id"]), str(candidate.get("published_path") or "")
            try:
                result = bridge.rename_note(task_id, dry_run=args.dry_run)
                # 不看这次有没有改名：上次改了名但没记上的，这次补记
                if not args.dry_run and result["to"] != recorded:
                    store.record_memory_note_renamed(task_id, recorded, result["to"])
                (renamed if result["changed"] else skipped).append(result)
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                _print({"status": "blocked", "task_id": task_id, "error": str(exc), "renamed": renamed}, True)
                return 1
        _print({"status": "dry_run" if args.dry_run else "renamed", "renamed": renamed, "unchanged": len(skipped)}, True)
        return 0
    if args.command == "memory-retire":
        candidate = store.get_memory_candidate(args.task_id)
        if not candidate:
            _print({"status": "blocked", "error": "memory candidate not found"}, True)
            return 1
        published_path = candidate.get("published_path")
        if not published_path:
            _print({"status": "blocked", "error": "memory is not currently published"}, True)
            return 1
        if candidate.get("status") == "retired":
            archive = Path(str(published_path)).expanduser()
            if archive.is_file():
                _print({"status": "retired", "archive_path": str(archive), "idempotent": True}, True)
                return 0
        elif candidate.get("status") not in {"published", "revalidation_required"}:
            _print({"status": "blocked", "error": "memory is not currently published"}, True)
            return 1
        try:
            retirement = MemoryBridge(config).retire_candidate(
                args.task_id, str(published_path)
            )
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            _print(
                _reconciliation_payload(
                    args.task_id,
                    "explicit_retirement_filesystem",
                    [{"task_id": args.task_id, "error": str(exc)}],
                    f"agentos memory-retire {args.task_id} --reason <reason>",
                ),
                True,
            )
            return 1
        try:
            updated = store.mark_memory_retired(
                args.task_id, retirement["archive_path"], args.reason
            )
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            _print(
                _reconciliation_payload(
                    args.task_id,
                    "explicit_retirement_database_settlement",
                    [{
                        "task_id": args.task_id,
                        "error": str(exc),
                        "archive_path": retirement["archive_path"],
                    }],
                    f"agentos memory-retire {args.task_id} --reason <reason>",
                ),
                True,
            )
            return 1
        _print({"status": updated["status"], "retirement": retirement}, True)
        return 0
    if args.command == "memory-receipt":
        try:
            _print(store.memory_review_receipt(args.task_id), True)
            return 0
        except KeyError:
            _print({"status": "blocked", "error": "memory candidate not found"}, True)
            return 1
    if args.command == "bridge":
        UnixBridge(Path(args.socket) if args.socket else config.runtime_dir / "agentos.sock", store, config).serve_once()
        return 0
    if args.command == "review-packet":
        raw = sys.stdin.read() if args.path == "-" else Path(args.path).read_text(encoding="utf-8")
        packet = json.loads(raw)
        result = dispatch({"command": "review_decision", "packet": packet}, store, config)
        _print(result, True)
        return 0 if result.get("ok") else 1
    if args.command == "publish-memory":
        if not args.task_id:
            _print(MemoryBridge(config).publish(), True)
            return 1
        candidate = store.get_memory_candidate(args.task_id)
        if not candidate or candidate["status"] not in {
            "pending_final_review", "publication_failed", "published"
        }:
            _print({"status": "blocked", "error": "task has not passed final review"}, True)
            return 1
        try:
            reviewed_sha256 = _candidate_sha256(candidate)
            if reviewed_sha256 != candidate["evidence_sha256"]:
                raise ValueError("memory candidate changed after final review")
            # The shared publication path checks the final approval binding
            # under the store write lock before any Vault file or index byte is
            # written, then settles on that same locked connection.
            publication, published = _publish_approved_candidate(
                config, store, args.task_id, reviewed_sha256
            )
        except PublicationUncertain as exc:
            # The outcome could not be read back: report reconciliation and leave
            # the candidate alone instead of demoting a publication that may
            # already be committed.
            _print(_reconciliation_payload(
                args.task_id,
                "memory_publication_uncertain",
                [{"task_id": args.task_id, "error": str(exc)}],
            ), True)
            return 1
        except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
            errors = [{"task_id": args.task_id, "error": str(exc)}]
            try:
                store.mark_memory_publication_failed(args.task_id, str(exc))
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as mark_exc:
                errors.append({"task_id": args.task_id, "error": f"failure status settlement failed: {mark_exc}"})
            _print(_reconciliation_payload(
                args.task_id,
                "memory_publication",
                errors,
                manual_action=isinstance(exc, MemoryRecoveryError),
            ), True)
            return 1
        reused, validation_updates, validation_errors = _reconcile_validated_reuses(
            config, store, args.task_id
        )
        response = {
            "status": published["status"],
            "candidate_sha256": reviewed_sha256,
            "publication": publication,
            "validated_source_memories": reused,
            "source_memory_index_updates": validation_updates,
        }
        if validation_errors:
            _print({
                **_reconciliation_payload(
                    args.task_id, "source_memory_reactivation", validation_errors
                ),
                **response,
                "status": "reconciliation_required",
            }, True)
            return 1
        _print(response, True)
        return 0
    if args.command == "install-plan":
        path = _render_launchagent(config)
        _print({"status": "rendered_only", "path": str(path), "next": f"审核后复制到 ~/Library/LaunchAgents/{path.name} 并执行 launchctl bootstrap"})
        return 0
    if args.command == "install-runner":
        try:
            result = _build_runner_app(config)
            result["next"] = "在系统设置 → 隐私与安全性 → 完全磁盘访问权限中添加并启用“小八 AgentOS Runner”，然后执行 install-service"
            _print(result, True)
            return 0
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.command == "install-service":
        try:
            _print(_install_service(config, store), True)
            return 0
        except (OSError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.command in ("agent-enable", "agent-disable"):
        try:
            _print(_set_agents(config, args.names, args.command == "agent-enable"), True)
            return 0
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.command == "demo":
        demo_config = _demo_config(config)
        demo_store = EventStore(demo_config.database_path)
        demo_store.initialize()
        task_id = demo_store.submit(args.prompt, 1)
        result = Orchestrator(demo_config, demo_store).run_once()
        _print({"task_id": task_id, "production_loop": result, "event_chain": demo_store.verify_event_chain(), "database": str(demo_store.path)}, True)
        return 0
    if args.command == "run":
        if args.once:
            result = Orchestrator(config, store).run_once()
            _print(result, True)
            return 0 if result["status"] in (
                "idle", "completed", "retry", "frozen_paused", "degraded_health"
            ) else 1
        worker_state = _write_worker_state(config, store)
        print(json.dumps({"status": "worker_started", **worker_state}, ensure_ascii=False), flush=True)
        stopping = False

        def stop(_signum: int, _frame: Any) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        idle_polls = 0
        total_polls = 0
        last_report_key = None
        while not stopping:
            try:
                reloaded = AppConfig.load(config.path)
                if reloaded.database_path != store.path:
                    result = {
                        "status": "blocked_configuration",
                        "error": "运行时数据库路径已改变；为避免跨库执行，请重启 Worker",
                    }
                else:
                    config = reloaded
                    heartbeat_polls = max(1, int(config.worker.get(
                        "preflight_heartbeat_polls", config.worker.get("log_heartbeat_polls", 60)
                    )))
                    # Full preflight on the first poll and on every heartbeat;
                    # in between, an empty queue costs one read-only query.
                    result = Orchestrator(config, store).run_once(
                        preflight_when_idle=(total_polls % heartbeat_polls == 0)
                    )
            except ConfigError as exc:
                result = {"status": "blocked_configuration", "error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - the worker must outlive one bad poll
                # Exiting here would only hide the failure behind launchd
                # restarts. The lease is released below so the task is
                # recovered on the next poll instead of after its full lease.
                result = {
                    "status": "worker_error",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
                try:
                    # Do not make the task wait out its whole lease.
                    result["leases_released"] = store.expire_running_leases()
                except Exception as release_exc:  # noqa: BLE001
                    result["lease_release_error"] = type(release_exc).__name__
            total_polls += 1
            report_key = (result.get("status"), result.get("error"))
            heartbeat = max(1, int(config.worker.get("log_heartbeat_polls", 60)))
            quiet_statuses = ("idle", "blocked_configuration", "degraded_health", "worker_error")
            if result["status"] not in quiet_statuses or report_key != last_report_key or total_polls % heartbeat == 0:
                print(json.dumps(result, ensure_ascii=False), flush=True)
                last_report_key = report_key
            if result["status"] in quiet_statuses:
                idle_polls += 1
                if args.max_idle_polls and idle_polls >= args.max_idle_polls:
                    break
                time.sleep(int(config.worker["poll_seconds"]))
            else:
                idle_polls = 0
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
