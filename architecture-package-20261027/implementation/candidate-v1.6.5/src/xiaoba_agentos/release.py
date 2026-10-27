from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .config import AppConfig


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
)
SOURCE_ROOT_FILES = ("README.md", "pyproject.toml", ".gitignore", "CAPABILITY.json")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path, label: str, problems: list[str]) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f"{label} unavailable: {exc}")
        return None
    if not isinstance(payload, dict):
        problems.append(f"{label} must be a JSON object")
        return None
    return payload


def _safe_relative(value: str, label: str, problems: list[str]) -> Optional[Path]:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or path == Path("."):
        problems.append(f"{label} is not a safe relative path")
        return None
    return path


def _source_manifest_payload(root: Path, problems: list[str]) -> Optional[Dict[str, Any]]:
    entries: Dict[str, str] = {}
    try:
        for directory in SOURCE_DIRECTORIES:
            base = root / directory
            if not base.exists():
                continue
            if base.is_symlink():
                problems.append(f"runtime source contains symlink: {directory}")
                return None
            for path in sorted(base.rglob("*")):
                if "__pycache__" in path.parts or path.name == ".DS_Store" or not path.is_file():
                    continue
                if path.is_symlink():
                    problems.append(f"runtime source contains symlink: {path.relative_to(root)}")
                    return None
                entries[str(path.relative_to(root))] = _sha256(path)
        for name in SOURCE_ROOT_FILES:
            path = root / name
            if not path.is_file() or path.is_symlink():
                problems.append(f"runtime source file unavailable: {name}")
                return None
            entries[name] = _sha256(path)
    except OSError as exc:
        problems.append(f"runtime source cannot be hashed: {exc}")
        return None
    return {"algorithm": "sha256", "files": entries}


def _verify_source_tree(root: Path, label: str, problems: list[str]) -> Optional[str]:
    manifest_path = root / "SOURCE_MANIFEST.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        problems.append(f"{label} SOURCE_MANIFEST.json unavailable")
        return None
    actual_hash = _sha256(manifest_path)
    manifest = _load_json(manifest_path, f"{label} source manifest", problems)
    computed = _source_manifest_payload(root, problems)
    if manifest is not None and computed is not None and manifest != computed:
        recorded = manifest.get("files", {}) if isinstance(manifest.get("files"), dict) else {}
        current = computed["files"]
        changed = sorted(
            name for name in set(recorded) | set(current) if recorded.get(name) != current.get(name)
        )
        suffix = ", ".join(changed[:20])
        if len(changed) > 20:
            suffix += f", ... ({len(changed)} total)"
        problems.append(f"{label} source manifest mismatch: {suffix or 'invalid manifest payload'}")
    return actual_hash


def _release_files(release_dir: Path, manifest_path: Path, problems: list[str]) -> None:
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        problems.append(f"FILES.sha256 unavailable: {exc}")
        return

    entries: Dict[str, str] = {}
    for line_number, line in enumerate(lines, 1):
        digest, separator, raw_path = line.partition("  ")
        if not separator or not _SHA256.fullmatch(digest) or not raw_path.startswith("./"):
            problems.append(f"FILES.sha256 has invalid line {line_number}")
            continue
        relative = _safe_relative(raw_path[2:], f"FILES.sha256 line {line_number}", problems)
        if relative is None:
            continue
        name = relative.as_posix()
        if name == "FILES.sha256":
            problems.append("FILES.sha256 must not contain itself")
            continue
        if name in entries:
            problems.append(f"FILES.sha256 contains duplicate path: {name}")
            continue
        entries[name] = digest

    try:
        actual_paths = set()
        for path in release_dir.rglob("*"):
            if path.is_symlink():
                problems.append(f"immutable release contains symlink: {path.relative_to(release_dir)}")
                continue
            if path.is_file() and path != manifest_path:
                actual_paths.add(path.relative_to(release_dir).as_posix())
    except OSError as exc:
        problems.append(f"immutable release cannot be enumerated: {exc}")
        return

    missing = sorted(set(entries) - actual_paths)
    unlisted = sorted(actual_paths - set(entries))
    if missing:
        problems.append("FILES.sha256 missing files: " + ", ".join(missing[:20]))
    if unlisted:
        problems.append("FILES.sha256 has unlisted files: " + ", ".join(unlisted[:20]))

    for name, expected in entries.items():
        path = release_dir / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            actual = _sha256(path)
        except OSError as exc:
            problems.append(f"release file cannot be hashed ({name}): {exc}")
            continue
        if actual != expected:
            problems.append(f"release file hash mismatch: {name}")


def _is_schema_one_mock(config: AppConfig) -> bool:
    agents = config.raw.get("agents")
    return (
        config.raw.get("schema_version") == 1
        and isinstance(agents, list)
        and bool(agents)
        and all(isinstance(agent, dict) and agent.get("type") == "mock" for agent in agents)
    )


def release_binding_status(config: AppConfig) -> Dict[str, Any]:
    """Bind the mutable runtime workspace to one indexed immutable release."""

    if _is_schema_one_mock(config):
        return {
            "ok": True,
            "skipped": True,
            "reason": "schema_v1_mock_test_config",
        }

    problems: list[str] = []
    policy = config.raw.get("release")
    if not isinstance(policy, dict):
        return {"ok": False, "skipped": False, "problems": ["release policy is missing"]}

    index_value = str(policy.get("index_path", "")).strip()
    expected_version = str(policy.get("expected_version", "")).strip()
    acceptance_mode = policy.get("acceptance_mode") is True
    index_path = Path(index_value).expanduser()
    result: Dict[str, Any] = {
        "ok": False,
        "skipped": False,
        "index_path": str(index_path),
        "expected_version": expected_version,
        "acceptance_mode": acceptance_mode,
        "current_release": None,
        "release_dir": None,
        "files_manifest_sha256": None,
        "release_source_manifest_sha256": None,
        "runtime_source_manifest_sha256": None,
        "problems": problems,
    }
    if not index_value or not index_path.is_absolute():
        problems.append("release.index_path must be absolute")
        return result
    if not expected_version:
        problems.append("release.expected_version is missing")
        return result
    if index_path.is_symlink():
        problems.append("release index must not be a symlink")
        return result

    index = _load_json(index_path, "release index", problems)
    if index is None:
        return result
    current_release = str(index.get("current_release", ""))
    result["current_release"] = current_release
    if current_release != expected_version:
        problems.append(
            f"release index current_release={current_release or '<missing>'}, expected={expected_version}"
        )

    releases = index.get("releases")
    matches = (
        [item for item in releases if isinstance(item, dict) and item.get("version") == expected_version]
        if isinstance(releases, list)
        else []
    )
    if len(matches) != 1:
        problems.append(f"release index must contain exactly one {expected_version} entry")
        return result
    entry = matches[0]
    entry_status = str(entry.get("status", ""))
    if acceptance_mode:
        temp_roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()}
        temporary_paths = (index_path.resolve(), config.runtime_dir.resolve())
        if not all(
            any(path == root or root in path.parents for root in temp_roots)
            for path in temporary_paths
        ):
            problems.append("acceptance mode is restricted to temporary index and runtime paths")
        if config.memory.get("publish_enabled") is not False:
            problems.append("acceptance mode requires memory.publish_enabled=false")
        if entry_status != "release_candidate":
            problems.append("acceptance mode requires a release_candidate index entry")
    elif entry_status not in {"verified_immutable", "verified_immutable_baseline"}:
        problems.append("active release index entry is not verified immutable")
    relative_release = _safe_relative(str(entry.get("path", "")), "release entry path", problems)
    if relative_release is None:
        return result
    releases_root = index_path.parent.resolve()
    release_dir = (index_path.parent / relative_release).resolve()
    result["release_dir"] = str(release_dir)
    try:
        release_dir.relative_to(releases_root)
    except ValueError:
        problems.append("release entry escapes the releases directory")
        return result
    if not release_dir.is_dir() or (index_path.parent / relative_release).is_symlink():
        problems.append("indexed release directory is unavailable or is a symlink")
        return result

    expected_files_hash = str(entry.get("files_manifest_sha256", ""))
    if not _SHA256.fullmatch(expected_files_hash):
        problems.append("release entry has no valid files_manifest_sha256")
        return result
    files_manifest = release_dir / "FILES.sha256"
    if not files_manifest.is_file() or files_manifest.is_symlink():
        problems.append("indexed release FILES.sha256 is unavailable")
        return result
    actual_files_hash = _sha256(files_manifest)
    result["files_manifest_sha256"] = actual_files_hash
    if actual_files_hash != expected_files_hash:
        problems.append("indexed FILES.sha256 fingerprint mismatch")
        return result
    _release_files(release_dir, files_manifest, problems)

    release_record = _load_json(release_dir / "RELEASE.json", "RELEASE.json", problems)
    if release_record is None:
        return result
    if str(release_record.get("version", "")) != expected_version:
        problems.append("RELEASE.json version does not match expected release")
    release_status = str(release_record.get("status", ""))
    if acceptance_mode:
        if release_status != "release_candidate":
            problems.append("acceptance mode requires RELEASE.json status=release_candidate")
    elif release_status != "verified_immutable_release":
        problems.append("active RELEASE.json is not a verified immutable release")
    expected_source_hash = str(release_record.get("agentos_source_manifest_sha256", ""))
    if not _SHA256.fullmatch(expected_source_hash):
        problems.append("RELEASE.json has no valid agentos_source_manifest_sha256")
        return result

    release_source_hash = _verify_source_tree(release_dir / "agentos", "release", problems)
    result["release_source_manifest_sha256"] = release_source_hash
    if release_source_hash != expected_source_hash:
        problems.append("release SOURCE_MANIFEST.json fingerprint mismatch")

    runtime_source_hash = _verify_source_tree(config.project_dir, "runtime", problems)
    result["runtime_source_manifest_sha256"] = runtime_source_hash
    if runtime_source_hash != expected_source_hash:
        problems.append("runtime SOURCE_MANIFEST.json is not bound to the immutable release")

    result["ok"] = not problems
    return result
