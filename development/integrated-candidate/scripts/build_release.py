#!/usr/bin/env python3
"""Build a complete immutable-release candidate without touching the active release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

# Verification and the full test run use the interpreter this build runs on.
# main() refuses to build unless that is the core's pinned Python declared in
# the source being built (src/xiaoba_agentos/interpreter.py).
CORE_PYTHON = sys.executable


def pinned_core_python(source_root: Path) -> Path:
    import ast
    tree = ast.parse((source_root / "src/xiaoba_agentos/interpreter.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "PINNED_PYTHON"):
            value = ast.literal_eval(node.value.args[0]) if isinstance(node.value, ast.Call) and len(node.value.args) == 1 else None
            if isinstance(value, str) and Path(value).is_absolute():
                return Path(value)
    raise RuntimeError("source does not declare a pinned core interpreter")


def require_core_python(source_root: Path, running: Optional[str] = None) -> None:
    pinned = pinned_core_python(source_root)
    actual = os.path.realpath(running or sys.executable)
    if not pinned.is_file() or actual != os.path.realpath(pinned):
        raise RuntimeError(f"build must run on the pinned core interpreter {pinned}, not {actual}")

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from xiaoba_agentos.standby_health import EXTERNAL_FAILURES, coverable_health

from agentos_version import software_version
from release_tier import TierError, compute_tier


SOURCE_DIRECTORIES = (
    ".agents",
    "bin",
    "config",
    "docs",
    "knowledge",
    "launchd",
    "macos",
    "scripts",
    "skills",
    "src",
    "tests",
    "artifacts",
)
SOURCE_ROOT_FILES = (".gitignore", "README.md", "SOURCE_MANIFEST.json", "pyproject.toml", "CAPABILITY.json")

NON_ANSWER_MARKERS = (
    "<invoke ",
    "</invoke>",
    "<parameter ",
    "</parameter>",
    "<｜dsml｜",
    "<|dsml|",
    "tool-call serialization",
    "tool call serialization",
)


def contains_non_answer_markers(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in NON_ANSWER_MARKERS)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"release input contains symlink: {path}")
        if path.is_file() and "__pycache__" not in path.parts and path.name != ".DS_Store":
            yield path


def manifest(root: Path) -> str:
    return "".join(
        f"{sha256(path)}  ./{path.relative_to(root).as_posix()}\n" for path in files(root)
    )


def symlinks_under(root: Path) -> list[str]:
    """包内出现软链就等于把包外内容带进不可变发布，所以任何层级都要报出来。"""
    linked: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in (*dirnames, *filenames):
            candidate = Path(dirpath) / name
            if candidate.is_symlink():
                linked.append(str(candidate))
    return sorted(linked)


def copy_tree(source: Path, target: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise RuntimeError(f"release source directory unavailable: {source}")
    linked = symlinks_under(source)
    if linked:
        raise RuntimeError("release input contains symlink: " + ", ".join(linked))
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns("__pycache__", ".DS_Store", "*.pyc"),
    )


def present_source_directories(source_root: Path) -> list[str]:
    """受管目录清单是上限：技能和价值文件索引已出包，对应目录不再存在。其余目录缺失仍是错误。

    artifacts/ 是 install-service 现算现写的输出目录（launchd 模板渲染结果），被 .gitignore
    忽略，源码树里没有；运行时校验 release.py 的受管集合本来也不含它。所以同样按"存在才复制"。
    """
    optional = ("skills", "knowledge", "artifacts")
    return [name for name in SOURCE_DIRECTORIES if name not in optional or (source_root / name).exists()]


def code_fingerprint(source_root: Path) -> str:
    records = [
        [path.relative_to(source_root).as_posix(), sha256(path)]
        for path in files(source_root / "src" / "xiaoba_agentos")
        if path.suffix == ".py"
    ]
    return hashlib.sha256(
        json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def packaged_database_schema_version(agentos_root: Path) -> int:
    """数据库结构版本只从这棵树自己声明的常量读，不写死；读不到可用整数就拒绝构包。"""
    import ast
    source = agentos_root / "src" / "xiaoba_agentos" / "store.py"
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f"database schema source is unavailable: {source}")
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"database schema source cannot be parsed: {source}: {exc}") from exc
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "DATABASE_SCHEMA_VERSION"):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (SyntaxError, TypeError, ValueError):
            value = None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    raise RuntimeError(f"source does not declare a usable database schema version: {source}")


def run_verify(command: list[str], cwd: Path) -> None:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout)[-4000:])


def _validate_acceptance_payload(payload: dict[str, Any], source_root: Path) -> dict[str, Any]:
    task = payload.get("task") if isinstance(payload, dict) else None
    runs = payload.get("runs") if isinstance(payload, dict) else None
    candidate = payload.get("memory_candidate") if isinstance(payload, dict) else None
    judge = payload.get("judge") if isinstance(payload, dict) else None
    if not isinstance(task, dict) or task.get("status") != "completed":
        raise RuntimeError("acceptance task is not completed")
    if task.get("execution_mode") != "production":
        raise RuntimeError("acceptance task did not use the production loop")
    result = str(task.get("result") or "")
    if not re.match(r"^本回答：小八 AgentOS (?:双|三|多) Agent 裁判(?:\n|$)", result):
        raise RuntimeError("acceptance result has no adjudicated provenance label")
    if contains_non_answer_markers(result):
        raise RuntimeError("acceptance result contains unexecuted tool-call markup")
    try:
        provenance = json.loads(str(task.get("provenance_json") or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("acceptance provenance is invalid") from exc
    try:
        snapshot = json.loads(str(task.get("execution_snapshot_json") or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("acceptance execution snapshot is invalid") from exc
    source_manifest_sha256 = sha256(source_root / "SOURCE_MANIFEST.json")
    if snapshot.get("source_manifest_sha256") != source_manifest_sha256:
        raise RuntimeError("acceptance task is not bound to the current source manifest")
    config = json.loads((source_root / "config" / "agentos.json").read_text(encoding="utf-8"))
    enabled_agents = {
        str(item.get("name"))
        for item in config.get("agents", [])
        if isinstance(item, dict) and item.get("enabled")
    }
    primary_agents = {
        str(item.get("name"))
        for item in config.get("agents", [])
        if isinstance(item, dict) and item.get("enabled") and item.get("standby") is not True
    }
    standby_agents = {
        str(item.get("name"))
        for item in config.get("agents", [])
        if isinstance(item, dict) and item.get("enabled") and item.get("standby") is True
    }
    minimum = int(config.get("worker", {}).get("minimum_successful_agents", 0))
    completed_agents = {
        str(item.get("agent_name"))
        for item in runs or []
        if isinstance(item, dict) and item.get("status") == "completed" and item.get("output")
    }
    if provenance.get("kind") != "agentos_multi_adjudicated" or minimum < 2:
        raise RuntimeError("acceptance provenance does not match the release policy")
    if isinstance(judge, dict):
        if int(judge.get("successful_agents", 0)) < minimum:
            raise RuntimeError("acceptance judge did not record the required success threshold")
        if str(judge.get("winner", "")) not in completed_agents:
            raise RuntimeError("acceptance judge winner is not a successful Agent")
    elif int(provenance.get("minimum_successful_agents", 0)) != minimum:
        raise RuntimeError("acceptance success threshold does not match the release policy")
    if len(completed_agents) < minimum or not completed_agents.issubset(enabled_agents):
        raise RuntimeError("acceptance evidence has insufficient independent successful Agents")

    completed_standby = completed_agents.intersection(standby_agents)
    completed_primary = completed_agents.intersection(primary_agents)

    if len(completed_standby) > 1:
        raise RuntimeError("acceptance evidence cannot contain multiple standby agents")
    if completed_standby and len(completed_primary) == 0:
        raise RuntimeError("acceptance evidence cannot consist of standby agents alone")

    sub_data = None
    if completed_standby and payload.get("from_acceptance_database") and not isinstance(payload.get("standby_event"), dict):
        # The acceptance database always holds the substitution event when a
        # standby ran (the orchestrator writes it before the standby is called);
        # the judge/task fallbacks below exist only for payload-level callers and
        # must not let the database path skip the replaced-side check (R4).
        raise RuntimeError("acceptance task with standby agent has no standby.substituted event in the database")
    if completed_standby:
        if isinstance(payload.get("standby_event"), dict):
            sub_data = payload["standby_event"]
        elif isinstance(payload.get("events"), list):
            for ev in payload["events"]:
                if isinstance(ev, dict) and ev.get("event_type") == "standby.substituted":
                    p = ev.get("payload_json")
                    sub_data = p if isinstance(p, dict) else json.loads(str(p or "{}"))
                    break
        if not sub_data and isinstance(judge, dict) and judge.get("standby_substitution"):
            sub_data = judge["standby_substitution"]
        if not sub_data and isinstance(task, dict) and task.get("standby_substitution"):
            sub_data = task["standby_substitution"]

        if not isinstance(sub_data, dict):
            raise RuntimeError("acceptance task with standby agent is missing standby substitution evidence")

        replaced = str(sub_data.get("replaced") or "")
        standby_name = str(sub_data.get("standby") or "")
        reason = str(sub_data.get("reason") or "")
        failure_class = str(sub_data.get("failure_class") or "")

        if not replaced or replaced not in primary_agents:
            raise RuntimeError(f"standby substitution replaced non-primary agent: {replaced}")
        if standby_name not in completed_standby:
            raise RuntimeError(f"standby substitution agent mismatch: {standby_name}")
        if reason not in {"health_probe_failed", "external_failure"}:
            raise RuntimeError(f"invalid standby substitution reason: {reason}")
        if reason == "external_failure" and failure_class not in EXTERNAL_FAILURES:
            raise RuntimeError(f"standby substitution must follow an external failure, not: {failure_class or 'unknown'}")
        if reason == 'health_probe_failed':
            health_source = sub_data.get('health_evidence')
            if (not coverable_health(health_source)
                    or str(health_source.get('failure_class') or '') != failure_class):
                raise RuntimeError('standby substitution has unverified health source; task degradation is not a failed probe')
        if replaced in completed_agents:
            raise RuntimeError(f"replaced agent {replaced} cannot also be a successful agent")
        # User 2026-10-21: the gate must confirm the replaced side really failed,
        # from records other than the substitution event itself.
        if payload.get("from_acceptance_database"):
            recorded = payload.get("replaced_health")
            if reason == "health_probe_failed":
                if (not isinstance(recorded, dict) or recorded.get("status") not in ("degraded", "external_blocked")
                        or str(recorded.get("failure_class") or "") != failure_class):
                    raise RuntimeError(f"replaced agent {replaced} has no recorded failed health before the substitution")
            if reason == "external_failure":
                # The failed run must carry the same external class the
                # substitution claims; any failed run is not enough (round 32).
                failed_runs = [item for item in runs or [] if isinstance(item, dict)
                               and item.get("agent_name") == replaced and item.get("status") != "completed"]
                if not failed_runs:
                    raise RuntimeError(f"replaced agent {replaced} has no failed run in this task")
                if not any(str(item.get("failure_class") or "") == failure_class for item in failed_runs):
                    raise RuntimeError(
                        f"replaced agent {replaced} has no failed run of class {failure_class} in this task")

    for item in runs or []:
        if not isinstance(item, dict) or item.get("status") != "completed":
            continue
        output = str(item.get("output") or "")
        verdicts = re.findall(r"^\s*FINAL_ACCEPTANCE: (PASS|FAIL)\s*$", output, re.MULTILINE)
        if verdicts != ["PASS"] or re.search(r"FINAL_ACCEPTANCE:\s*FAIL", output):
            raise RuntimeError("every successful acceptance Agent must explicitly return one FINAL_ACCEPTANCE: PASS")
        if contains_non_answer_markers(output):
            raise RuntimeError("acceptance Agent output contains unexecuted tool-call markup")
    audit_only = snapshot.get("experience_policy", {}).get("kind") == "audit"
    if audit_only:
        result_sha = hashlib.sha256(result.encode()).hexdigest()
        snapshot_sha = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        settlement = payload.get("audit_settlement") or {}
        if (candidate is not None or provenance.get("memory_disposition") != "audit_only"
                or provenance.get("result_sha256") != result_sha or settlement.get("result_sha256") != result_sha
                or snapshot.get("experience_policy", {}).get("version") != 1
                or task.get("execution_snapshot_hash") != snapshot_sha):
            raise RuntimeError("acceptance audit settlement is missing or not bound to the frozen task/result")
    elif not isinstance(candidate, dict) or candidate.get("status") != "pending_delivery_review":
        raise RuntimeError("acceptance task did not stage a delivery-review candidate")
    return {
        "task_id": str(task.get("id")),
        "successful_agents": sorted(completed_agents),
        "minimum_successful_agents": minimum,
        "candidate_status": "audit_only" if audit_only else str(candidate.get("status")),
        "candidate_sha256": "" if audit_only else str(candidate.get("evidence_sha256") or ""),
        "source_manifest_sha256": source_manifest_sha256,
        "standby_substituted": bool(completed_standby),
        "replaced": str(sub_data.get("replaced")) if completed_standby else None,
        "reason": str(sub_data.get("reason")) if completed_standby else None,
    }


def validate_acceptance_evidence(path: Path, source_root: Path) -> dict[str, Any]:
    """Validate exported `show` output; retained for offline verification tests."""
    evidence_path = path.resolve()
    if evidence_path.is_symlink() or not evidence_path.is_file():
        raise RuntimeError("acceptance evidence file is unavailable")
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"acceptance evidence is invalid: {exc}") from exc
    result = _validate_acceptance_payload(payload, source_root)
    result["evidence_file_sha256"] = sha256(evidence_path)
    return result


def validate_acceptance_database(
    database_path: Path, task_id: str, source_root: Path
) -> dict[str, Any]:
    """Read the immutable task facts directly from the acceptance SQLite database."""
    database = database_path.resolve()
    if database.is_symlink() or not database.is_file():
        raise RuntimeError("acceptance database is unavailable")
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
            snapshot_hash_column = ",execution_snapshot_hash " if "execution_snapshot_hash" in columns else ",NULL AS execution_snapshot_hash "
            task_row = connection.execute(
                "SELECT id,status,execution_mode,result,provenance_json,execution_snapshot_json" + snapshot_hash_column +
                "FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            run_rows = connection.execute(
                "SELECT agent_name,status,output,failure_class FROM agent_runs WHERE task_id=? ORDER BY created_at",
                (task_id,),
            ).fetchall()
            candidate_row = connection.execute(
                "SELECT task_id,status,evidence_sha256 FROM memory_candidates WHERE task_id=?",
                (task_id,),
            ).fetchone()
            judge_row = connection.execute(
                "SELECT payload_json FROM events WHERE task_id=? AND event_type='judge.decided' ORDER BY sequence DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            audit_row = connection.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='audit.settled' ORDER BY sequence DESC LIMIT 1", (task_id,)).fetchone()
            sub_row = connection.execute("SELECT sequence,payload_json FROM events WHERE task_id=? AND event_type='standby.substituted' ORDER BY sequence DESC LIMIT 1", (task_id,)).fetchone()
            # The replaced side's own health history, read independently of the
            # substitution event: the last health record written before it.
            replaced_health = None
            if sub_row:
                try:
                    replaced_name = str(json.loads(str(sub_row["payload_json"])).get("replaced") or "")
                except (TypeError, ValueError, AttributeError):
                    replaced_name = ""
                if replaced_name:
                    replaced_health = connection.execute(
                        "SELECT payload_json FROM events WHERE event_type='health.agent_probe' AND sequence<? "
                        "AND json_extract(payload_json,'$.agent')=? ORDER BY sequence DESC LIMIT 1",
                        (sub_row["sequence"], replaced_name),
                    ).fetchone()
            health_rows = connection.execute(
                """SELECT agent_name,status,provider,model,last_probe_at FROM agent_health
                   WHERE status='healthy' ORDER BY agent_name"""
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError(f"cannot read acceptance database: {exc}") from exc
    if not judge_row:
        raise RuntimeError("acceptance database has no judge decision")
    try:
        judge = json.loads(str(judge_row["payload_json"]))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("acceptance judge decision is invalid") from exc
    if not isinstance(judge, dict):
        raise RuntimeError("acceptance judge decision must be an object")
    payload = {
        "from_acceptance_database": True,
        "replaced_health": None,
        "task": dict(task_row) if task_row else None,
        "runs": [dict(row) for row in run_rows],
        "memory_candidate": dict(candidate_row) if candidate_row else None,
        "judge": judge,
        "audit_settlement": json.loads(audit_row["payload_json"]) if audit_row else None,
    }
    if sub_row:
        try:
            payload["standby_event"] = json.loads(str(sub_row["payload_json"]))
        except (TypeError, ValueError):
            payload["standby_event"] = None
        try:
            payload["replaced_health"] = json.loads(str(replaced_health["payload_json"])) if replaced_health else None
        except (TypeError, ValueError):
            payload["replaced_health"] = None
    result = _validate_acceptance_payload(payload, source_root)
    healthy_agents = {
        str(row["agent_name"])
        for row in health_rows
        if row["provider"] and row["model"] and float(row["last_probe_at"] or 0) > 0
    }
    if not result["successful_agents"] or not set(result["successful_agents"]).issubset(healthy_agents):
        raise RuntimeError("acceptance database has no current real multi-Agent health evidence")
    result["live_probe_agents"] = sorted(healthy_agents)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    result["database_record_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return result


def release_tier_record(previous_release: Optional[Path], source_root: Path) -> Optional[dict[str, Any]]:
    """Tier of this build, computed by the previous verified release's own tool; None when no baseline is given."""
    if previous_release is None:
        return None
    previous = previous_release.resolve()
    try:
        declared = json.loads((previous / "RELEASE.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"previous release declaration is unavailable: {exc}") from exc
    if not isinstance(declared, dict) or declared.get("status") != "verified_immutable_release":
        raise RuntimeError("previous release is not a verified immutable release")
    if declared.get("agentos_source_manifest_sha256") != sha256(previous / "agentos" / "SOURCE_MANIFEST.json"):
        raise RuntimeError("previous release source binding mismatch")
    # 档位必须对“将要进包的那棵树”算：源码根目录里可以有不进包的东西（接口夹具等），
    # 切换预检是拿包里的 agentos 重算的，两边对象不同就会对不上。
    with tempfile.TemporaryDirectory(prefix="xiaoba-tier-") as temporary:
        staged = Path(temporary) / "agentos"
        staged.mkdir()
        for name in present_source_directories(source_root):
            copy_tree(source_root / name, staged / name)
        for name in SOURCE_ROOT_FILES:
            shutil.copy2(source_root / name, staged / name)
        try:
            record = compute_tier(previous / "agentos", staged)
        except TierError as exc:
            raise RuntimeError(f"release tier cannot be computed: {exc}") from exc
    record["baseline_version"] = str(declared.get("version"))
    return record


def run_full_tests(source_root: Path) -> int:
    """Patch tier has no reviewers: the build itself runs the whole suite on the manifest-verified source."""
    run_verify([CORE_PYTHON, "scripts/create_manifest.py", "--verify"], source_root)
    env = dict(os.environ, PYTHONPATH="src:tests", PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run(
        [CORE_PYTHON, "-m", "unittest", "discover", "-s", "tests", "-t", "tests", "-p", "test_*.py"],
        cwd=source_root, env=env, text=True, capture_output=True, check=False,
    )
    lines = completed.stderr.strip().splitlines()
    ran = re.search(r"^Ran (\d+) tests? in ", completed.stderr, re.MULTILINE)
    if completed.returncode != 0 or not ran or not lines or lines[-1] != "OK":
        raise RuntimeError("patch-tier build requires the full test suite to pass: " + (lines[-1] if lines else "no output"))
    return int(ran.group(1))


def _assemble_package(
    args: argparse.Namespace,
    source_root: Path,
    staging: Path,
    tier_record: Optional[dict[str, Any]],
    patch_tier: bool,
    acceptance_evidence: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """把整包写进 staging，返回将要落盘的 RELEASE.json 内容。不碰目标位置。"""
    agentos_target = staging / "agentos"
    agentos_target.mkdir()
    for name in present_source_directories(source_root):
        copy_tree(source_root / name, agentos_target / name)
    for name in SOURCE_ROOT_FILES:
        source = source_root / name
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"release source file unavailable: {source}")
        shutil.copy2(source, agentos_target / name)

    declared_version = software_version(agentos_target)
    if args.version != declared_version and not args.version.startswith(declared_version + "-"):
        raise RuntimeError(
            f"release version {args.version} does not match the declared software version "
            f"{declared_version} in {agentos_target / 'pyproject.toml'}"
        )
    if args.acceptance_complete and args.version != declared_version:
        raise RuntimeError(
            "only a build whose version equals the declared software version "
            f"{declared_version} may be marked verified_immutable_release, got {args.version}"
        )

    interface_card = args.interface
    if interface_card.is_symlink():
        raise RuntimeError(f"release interface card unavailable: {interface_card} is a symlink")
    if not interface_card.is_file():
        raise RuntimeError(
            f"release interface card unavailable: expected a regular file at {interface_card}; "
            "pass --interface to point at the real interface card"
        )
    interface_target = staging / "interface"
    (interface_target / "scripts").mkdir(parents=True)
    shutil.copy2(interface_card.resolve(), interface_target / "SKILL.md")
    shutil.copy2(interface_card.resolve(), staging / "SKILL.md")
    stable_scripts = args.stable_scripts.resolve()
    for name in ("xiaoba", "verify-release"):
        source = stable_scripts / name
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"stable interface script unavailable: {source}")
        destination = interface_target / "scripts" / name
        shutil.copy2(source, destination)
        os.chmod(destination, 0o755)

    brain_target = staging / "second-brain"
    copy_tree(args.second_brain.resolve(), brain_target)

    run_verify([CORE_PYTHON, "scripts/create_manifest.py", "--verify"], agentos_target)
    run_verify(
        [
            CORE_PYTHON,
            "scripts/build_second_brain.py",
            "--agentos-root",
            str(agentos_target),
            "--published-agentos-root",
            str(args.published_agentos_root.resolve()),
            "--target",
            str(brain_target),
            "--verify",
        ],
        agentos_target,
    )

    # 结构版本要在清单校验通过之后，从真正进包的那棵树读；两边不一致说明进包的和源不是一棵树。
    schema_version = packaged_database_schema_version(agentos_target)
    source_schema_version = packaged_database_schema_version(source_root)
    if source_schema_version != schema_version:
        raise RuntimeError(
            f"packaged database schema version {schema_version} differs from the source "
            f"declaration {source_schema_version}"
        )

    interface_manifest = staging / "INTERFACE_MANIFEST.sha256"
    interface_manifest.write_text(manifest(interface_target), encoding="utf-8")
    brain_manifest = staging / "SECOND_BRAIN_MANIFEST.sha256"
    brain_manifest.write_text(manifest(brain_target), encoding="utf-8")

    source_manifest = json.loads(
        (agentos_target / "SOURCE_MANIFEST.json").read_text(encoding="utf-8")
    )
    release = {
        "product": "小八 AgentOS",
        "version": args.version,
        "status": "verified_immutable_release" if args.acceptance_complete else "release_candidate",
        "verified_date": args.verified_date,
        "release_origin": f"{declared_version}_agy_dsh_current_only",
        "agentos_source_manifest_sha256": sha256(agentos_target / "SOURCE_MANIFEST.json"),
        "second_brain_manifest_sha256": sha256(brain_manifest),
        "interface_manifest_sha256": sha256(interface_manifest),
        "code_fingerprint": code_fingerprint(agentos_target),
        "database_schema_version": schema_version,
        "source_files": len(source_manifest["files"]),
        "second_brain_markdown_files": sum(1 for path in files(brain_target) if path.suffix == ".md"),
        "contents": {
            "agentos": "Complete AgentOS source for this release",
            "second_brain": f"Single-root {declared_version} current AGY+DSH Obsidian memory tree",
            "interface": "Current xiaoba-agentos entrypoint and release verifier",
            "runtime_data_included": False,
            "credentials_included": False,
            "generated_knowlp_index_included": False,
            "historical_memory_included": False,
            "media_assets_included": False,
        },
        "tests": {
            "count": args.test_count,
            "result": args.test_result,
        },
        "acceptance": {
            "provider_priority": ["Antigravity", "DeepSeekHarness"],
            "skill_registry": "content_not_in_package",
            "knowledge_catalog": "content_not_in_package",
            "second_brain_tree": "passed",
            "knowlp_tree": "passed" if args.acceptance_complete else "not_run",
            "candidate_release_binding": "passed" if args.acceptance_complete else "pending",
            "end_to_end_multi_agent": (
                "not_required_patch_tier" if patch_tier else "passed"
            ) if args.acceptance_complete else "pending",
            "live_session_probe": (
                "not_required_patch_tier" if patch_tier else "passed_from_acceptance_database"
            ) if args.acceptance_complete else "pending",
            "frozen_task_recovery": f"covered_by_offline_{args.test_count}_suite_only" if args.acceptance_complete else "pending",
            "end_to_end_evidence": acceptance_evidence,
        },
        "final_publish_requires_human": True,
        "known_external_issues": list(args.known_external_issue),
    }
    if tier_record is not None:
        release["release_tier"] = tier_record
    (staging / "RELEASE.json").write_text(
        json.dumps(release, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (staging / "FILES.sha256").write_text(manifest(staging), encoding="utf-8")
    return release


def build(args: argparse.Namespace) -> dict:
    source_root = args.agentos_root.resolve()
    acceptance_evidence = None
    tier_record = release_tier_record(getattr(args, "previous_release", None), source_root)
    patch_tier = tier_record is not None and tier_record["tier"] == "patch"
    if args.acceptance_complete and patch_tier:
        if args.test_result != "passed" or run_full_tests(source_root) != args.test_count:
            raise RuntimeError("patch-tier build: declared test count/result differs from the actual full run")
    elif args.acceptance_complete:
        if args.acceptance_database is None or not args.acceptance_task_id:
            raise RuntimeError(
                "--acceptance-complete requires --acceptance-database and --acceptance-task-id"
            )
        acceptance_evidence = validate_acceptance_database(
            args.acceptance_database, args.acceptance_task_id, source_root
        )
        if tier_record is None:
            raise RuntimeError("--acceptance-complete requires --previous-release")
    target = args.target.resolve()
    if target.exists() and any(target.iterdir()):
        raise RuntimeError(f"target must be absent or empty: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    # 中断不留半包：整包先写同级临时目录，全部校验通过后才一次改名到目标位置。
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent))
    published = False
    try:
        release = _assemble_package(
            args, source_root, staging, tier_record, patch_tier, acceptance_evidence
        )
        os.chmod(staging, 0o755)
        os.replace(staging, target)
        published = True
    finally:
        if not published:
            shutil.rmtree(staging, ignore_errors=True)
    return {
        "ok": True,
        "target": str(target),
        "version": args.version,
        "source_files": release["source_files"],
        "release_files": sum(1 for _path in files(target)),
    }


def build_parser() -> argparse.ArgumentParser:
    """构包参数。技能与价值文件是内容，不进包，构包不再核验它们。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--agentos-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--interface",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "interface" / "SKILL.md",
        help="Interface card packed as SKILL.md; defaults to the interface directory beside the source tree.",
    )
    parser.add_argument(
        "--stable-scripts",
        type=Path,
        default=Path.home() / ".agents" / "skills" / "xiaoba-agentos" / "scripts",
    )
    parser.add_argument(
        "--second-brain",
        type=Path,
        required=True,
        help="Directory holding the pre-generated second-brain tree to pack into the release.",
    )
    parser.add_argument(
        "--published-agentos-root",
        type=Path,
        required=True,
        help="Absolute directory of the agentos content the second brain skill cards must point at.",
    )
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--verified-date", required=True)
    parser.add_argument("--test-count", type=int, required=True)
    parser.add_argument("--test-result", choices=("passed", "failed", "not_run"), default="not_run")
    parser.add_argument("--acceptance-complete", action="store_true")
    parser.add_argument("--acceptance-evidence", type=Path)
    parser.add_argument("--acceptance-database", type=Path)
    parser.add_argument("--acceptance-task-id")
    parser.add_argument(
        "--previous-release",
        type=Path,
        help="Root of the previous verified immutable release package; its own tool decides this build's tier.",
    )
    parser.add_argument("--known-external-issue", action="append", default=[])
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        require_core_python(args.agentos_root.resolve())
        result = build(args)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
