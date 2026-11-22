#!/usr/bin/env python3
"""C11-02: deterministic, read-only preflight before dispatching release review.

One call reports *every* missing or stale declaration at once ("缺项一次列清").
It never dispatches a model, never writes a normal-retrieval event and never
mutates a file.  Version bumps, protected manifests and DEPLOYMENT binding
updates stay with the compliant desktop/user executor; this module only checks
and reports.

Self-reference note for ``docs/DEPLOYMENT.json.from_source_manifest_sha256``:
that field is the *predecessor* (live) release's ``agentos_source_manifest_sha256``
verified by ``switch_release.checked_spec`` against ``old[...]``.  It is NOT the
hash of the tree's own SOURCE_MANIFEST.  Demanding
``from_source_manifest_sha256 == sha256(current SOURCE_MANIFEST.json)`` is an
impossible equation: SOURCE_MANIFEST hashes DEPLOYMENT.json, so changing the
declaration re-hashes the manifest and the target moves.  We therefore check
it against the live predecessor's real value supplied by the caller, and flag
only a well-formed hash that wrongly points at the current tree itself.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = Path(__file__).resolve().parent
for _path in (str(_SCRIPTS), str(_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
from create_manifest import MANIFEST_NAME, collect_entries, manifest_digest, split_manifest_entries  # noqa: E402
from xiaoba_agentos.review_ui_install import TARGETS  # noqa: E402

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^\s*version\s*=\s*[\"'](\d+\.\d+\.\d+)[\"']", re.MULTILINE)


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        return ""  # absent self-manifest: no self-reference can be constructed
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _new_report() -> dict[str, Any]:
    return {"ok": True, "issues": [], "checks": {}}


def _issue(report: dict[str, Any], code: str, message: str) -> None:
    report["ok"] = False
    report["issues"].append({"code": code, "message": message})


def check_source_manifest(report: dict[str, Any], root: Path) -> None:
    """Freshness: the checked-in manifest must match every managed source file."""
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        _issue(report, "source_manifest_unavailable", f"源码清单不可用或为软链：{manifest_path}")
        return
    try:
        declared = json.loads(manifest_path.read_text(encoding="utf-8"))
        declared_files = declared.get("files", {})
        current = collect_entries(root)
    except Exception as exc:  # malformed manifest / symlink in managed tree
        _issue(report, "source_manifest_unreadable", f"源码清单无法核对：{type(exc).__name__}: {exc}")
        return
    if not isinstance(declared_files, dict):
        _issue(report, "source_manifest_invalid", "源码清单 files 字段不是对象")
        return
    changed = sorted(
        name for name in set(declared_files) | set(current)
        if declared_files.get(name) != current.get(name)
    )
    rt_entries, doc_entries = split_manifest_entries(current)
    report["checks"]["source_manifest"] = {
        "fresh": not changed,
        "changed": changed,
        "files": len(current),
        "runtime_files": len(rt_entries),
        "doc_files": len(doc_entries),
        "sha256": _sha256_file(manifest_path),
        "runtime_sha256": manifest_digest(rt_entries),
        "doc_sha256": manifest_digest(doc_entries),
    }
    if changed:
        _issue(report, "source_manifest_stale", "源码清单已过期，未覆盖文件：" + ", ".join(changed))


def check_pyproject_version(report: dict[str, Any], root: Path, expected_version: Optional[str]) -> None:
    path = root / "pyproject.toml"
    try:
        match = _VERSION_RE.search(path.read_text(encoding="utf-8"))
    except OSError as exc:
        _issue(report, "pyproject_unreadable", f"pyproject.toml 无法读取：{exc}")
        return
    version = match.group(1) if match else None
    report["checks"]["pyproject"] = {"version": version, "expected": expected_version}
    if version is None:
        _issue(report, "pyproject_version_missing", "pyproject.toml 未声明有效的三段式版本号")
    elif expected_version is not None and version != expected_version:
        _issue(report, "pyproject_version_mismatch",
               f"源码版本 {version} 与目标版本 {expected_version} 不一致；版本变更须由合规执行者落地")


def check_deployment(report: dict[str, Any], root: Path, predecessor_sha256: Optional[str]) -> None:
    """DEPLOYMENT predecessor binding — never compare against self manifest hash."""
    path = root / "docs" / "DEPLOYMENT.json"
    try:
        declaration = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _issue(report, "deployment_unreadable", f"DEPLOYMENT.json 无法读取：{exc}")
        return
    declared = str(declaration.get("from_source_manifest_sha256") or "")
    if not _SHA256_RE.match(declared):
        _issue(report, "deployment_binding_malformed", "DEPLOYMENT.json 的 from_source_manifest_sha256 不是 sha256")
        return
    self_manifest_sha = _sha256_file(root / MANIFEST_NAME)
    if declared == self_manifest_sha:
        _issue(report, "deployment_self_reference",
               "DEPLOYMENT.json 的前置哈希指向当前树自身清单，构成不可能的自引用")
    entry: dict[str, Any] = {
        "declared": declared,
        "self_manifest_sha256": self_manifest_sha,
        "is_self_reference": declared == self_manifest_sha,
        "meaning": "predecessor(live) release source-manifest sha256, not a self-hash",
    }
    if predecessor_sha256 is not None:
        entry["predecessor_sha256"] = predecessor_sha256
        if declared != predecessor_sha256:
            _issue(report, "deployment_predecessor_mismatch",
                   f"DEPLOYMENT 前置哈希 {declared} 与活动发布 {predecessor_sha256} 不符；"
                   "须由合规执行者按实际线上版本更新，不能只改版本字符串")
    else:
        entry["predecessor_verified"] = False
    report["checks"]["deployment"] = entry


def check_ui_manifest(
    report: dict[str, Any], root: Path, *, project: Optional[Path], assistant: Optional[Path]
) -> None:
    """Bidirectional hash check: bundled asset -> after_sha256 (forward), and
    external target -> before/after_sha256 (reverse, when roots are supplied)."""
    assets = root / "src" / "xiaoba_agentos" / "review_ui_assets"
    path = assets / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _issue(report, "ui_manifest_unreadable", f"UI 资产清单无法读取：{exc}")
        return
    entries = manifest.get("files")
    if (manifest.get("version") != 1 or not isinstance(entries, list)
            or len(entries) != len(TARGETS)
            or {e.get("asset") for e in entries if isinstance(e, dict)} != set(TARGETS)):
        _issue(report, "ui_manifest_structure", "UI 资产清单版本/条目数/资产集合不合法或有漏项")
        return
    forward: dict[str, bool] = {}
    for entry in entries:
        asset = entry.get("asset")
        raw_before = entry.get("before_sha256")
        after = str(entry.get("after_sha256") or "")
        before_valid = raw_before is None or (isinstance(raw_before, str) and bool(_SHA256_RE.match(raw_before)))
        if not before_valid or not _SHA256_RE.match(after):
            _issue(report, "ui_hash_malformed", f"UI 资产 {asset} 的 before/after 哈希格式错误")
            continue
        if (entry.get("base"), entry.get("relative")) != TARGETS.get(asset):
            _issue(report, "ui_manifest_target", f"UI 资产 {asset} 声明了不支持的安装目标")
            continue
        asset_path = assets / asset
        if not asset_path.is_file() or asset_path.is_symlink():
            _issue(report, "ui_asset_missing", f"UI 资产文件不可用：{asset}")
            continue
        actual = _sha256_file(asset_path)
        forward[asset] = actual == after
        if actual != after:
            _issue(report, "ui_asset_after_mismatch",
                   f"UI 资产 {asset} 实际哈希 {actual} 与 after_sha256 {after} 不符")
    info: dict[str, Any] = {"forward": forward}
    if project is not None and assistant is not None:
        try:
            from xiaoba_agentos.review_ui_install import prepare as ui_prepare
            ui_prepare(project, assistant, assets)  # reverse: target must equal before or after
            info["reverse"] = "ok"
        except (ValueError, OSError) as exc:
            _issue(report, "ui_target_drift", f"UI 安装目标与清单不一致，需 rebase：{exc}")
    else:
        info["reverse"] = "not_checked_no_roots"
    report["checks"]["ui_manifest"] = info


def check_reviewer_conflict(
    report: dict[str, Any], *, reviewers: Iterable[str], authors: Iterable[str], executors: Iterable[str]
) -> None:
    reviewer_ids = {str(v) for v in reviewers if v}
    author_ids = {str(v) for v in authors if v}
    executor_ids = {str(v) for v in executors if v}
    conflicts = sorted(reviewer_ids & (author_ids | executor_ids))
    report["checks"]["reviewer_conflict"] = {
        "reviewers": sorted(reviewer_ids),
        "authors": sorted(author_ids),
        "executors": sorted(executor_ids),
        "conflicts": conflicts,
        "checked": bool(reviewer_ids),
    }
    # Conflict is only meaningful when the dispatch layer actually declares the
    # reviewers it is about to ask. Absence is recorded as "not checked" and is
    # reported by the wrapper so an operator can supply them; it is not silently
    # treated as a pass, nor does it block a tree that has nothing to compare.
    if conflicts:
        _issue(report, "reviewer_author_conflict", "审查员与作者/执行者重叠：" + ", ".join(conflicts))


def preflight(
    source_root: Path,
    *,
    expected_version: Optional[str] = None,
    predecessor_sha256: Optional[str] = None,
    project: Optional[Path] = None,
    assistant: Optional[Path] = None,
    reviewers: Iterable[str] = (),
    authors: Iterable[str] = (),
    executors: Iterable[str] = (),
) -> dict[str, Any]:
    """Return one report listing every issue. Read-only; never dispatches."""
    root = Path(source_root).resolve()
    report = _new_report()
    check_source_manifest(report, root)
    check_pyproject_version(report, root, expected_version)
    check_deployment(report, root, predecessor_sha256)
    check_ui_manifest(report, root, project=project, assistant=assistant)
    check_reviewer_conflict(report, reviewers=reviewers, authors=authors, executors=executors)
    report["report_sha256"] = hashlib.sha256(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agentos-root", type=Path, default=_ROOT)
    parser.add_argument("--expected-version")
    parser.add_argument("--predecessor-sha256")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--assistant", type=Path)
    parser.add_argument("--reviewer", action="append", default=[])
    parser.add_argument("--author", action="append", default=[])
    parser.add_argument("--executor", action="append", default=[])
    args = parser.parse_args(argv)
    report = preflight(
        args.agentos_root,
        expected_version=args.expected_version,
        predecessor_sha256=args.predecessor_sha256,
        project=args.project,
        assistant=args.assistant,
        reviewers=args.reviewer,
        authors=args.author,
        executors=args.executor,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
