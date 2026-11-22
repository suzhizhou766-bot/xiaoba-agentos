#!/usr/bin/env python3
"""Create a deterministic, non-secret integrity manifest for project source files."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
INCLUDED = (".agents", "bin", "config", "docs", "knowledge", "launchd", "macos", "scripts", "skills", "src", "tests")
ROOT_FILES = ("README.md", "pyproject.toml", ".gitignore", "CAPABILITY.json")
MANIFEST_NAME = "SOURCE_MANIFEST.json"


class SymlinkError(RuntimeError):
    """清单只认普通文件：软链会把树外的内容当成源码指纹，也会把重算结果写到树外。"""

    def __init__(self, path: Path):
        super().__init__(str(path))
        self.path = path


def _sha256(path: Path) -> str:
    if path.is_symlink():
        raise SymlinkError(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _walk_regular_files(base: Path) -> list[Path]:
    if base.is_symlink():
        raise SymlinkError(base)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        current = Path(dirpath)
        if current.is_symlink():
            raise SymlinkError(current)
        kept_dirs = []
        for dname in sorted(dirnames):
            dpath = current / dname
            if dpath.is_symlink():
                raise SymlinkError(dpath)
            if dname != "__pycache__":
                kept_dirs.append(dname)
        dirnames[:] = kept_dirs
        for fname in sorted(filenames):
            fpath = current / fname
            if fpath.is_symlink():
                raise SymlinkError(fpath)
            if fname == ".DS_Store" or fpath.suffix in {".pyc", ".pyo"}:
                continue
            if fpath.is_file():
                found.append(fpath)
    found.sort()
    return found


def projected_sources(root: Path) -> list[tuple[Path, str]]:
    """Project sibling runtime/test directories of the integration worktree into packaged paths."""
    dev_dir = root.parent
    repo_dir = dev_dir.parent
    specs = (
        (
            dev_dir / "review-ingress/xiaoba_review",
            "src/xiaoba_review",
            (dev_dir / "review-ingress", dev_dir / "review-ingress/xiaoba_review"),
        ),
        (
            dev_dir / "review-ingress/tests",
            "tests/_external_suites/review_ingress",
            (dev_dir / "review-ingress", dev_dir / "review-ingress/tests"),
        ),
        (
            dev_dir / "architecture-center",
            "src/xiaoba_architecture_center",
            (dev_dir / "architecture-center",),
        ),
        (
            repo_dir / "architecture-package-20261027/shared/工具/小八监看",
            "src/xiaoba_monitor_shared",
            (
                repo_dir / "architecture-package-20261027",
                repo_dir / "architecture-package-20261027/shared",
                repo_dir / "architecture-package-20261027/shared/工具",
                repo_dir / "architecture-package-20261027/shared/工具/小八监看",
            ),
        ),
    )
    active: list[tuple[Path, str]] = []
    for sibling, dst_rel, guards in specs:
        packaged = root / dst_rel
        if packaged.is_symlink():
            raise SymlinkError(packaged)
        for guard in guards:
            if guard.is_symlink():
                raise SymlinkError(guard)
        if packaged.exists():
            continue
        if sibling.exists():
            _walk_regular_files(sibling)
            active.append((sibling, dst_rel))
    return active


def projected_files(root: Path) -> list[tuple[Path, str]]:
    """Project standalone sibling runtime entrypoints into packaged paths."""
    dev_dir = root.parent
    file_specs: list[tuple[Path, str, tuple[Path, ...]]] = [
        (
            dev_dir / "review-ingress/run.py",
            "src/xiaoba_review_entrypoints/run.py",
            (dev_dir / "review-ingress", dev_dir / "review-ingress/run.py"),
        ),
        (
            dev_dir / "review-ingress/workday_session.py",
            "src/xiaoba_review_entrypoints/workday_session.py",
            (dev_dir / "review-ingress", dev_dir / "review-ingress/workday_session.py"),
        ),
    ]
    # In a built release package, agentos/ sits beside interface/scripts/{xiaoba,verify-release};
    # only project development/interface/SKILL.md when not inside an assembled release root.
    if not (dev_dir / "interface/scripts").exists():
        file_specs.append(
            (
                dev_dir / "interface/SKILL.md",
                "src/xiaoba_interface_card/SKILL.md",
                (dev_dir / "interface", dev_dir / "interface/SKILL.md"),
            )
        )
    active: list[tuple[Path, str]] = []
    for src_file, dst_rel, guards in file_specs:
        packaged = root / dst_rel
        if packaged.is_symlink() or packaged.parent.is_symlink():
            raise SymlinkError(packaged if packaged.is_symlink() else packaged.parent)
        for guard in guards:
            if guard.is_symlink():
                raise SymlinkError(guard)
        if packaged.exists():
            continue
        if src_file.is_file():
            active.append((src_file, dst_rel))
    return active


def review_source(root: Path):
    """Compatibility helper returning the review-ingress runtime source if projected."""
    for sibling, dst_rel in projected_sources(root):
        if dst_rel == "src/xiaoba_review":
            return sibling
    return None


def collect_entries(root: Path = ROOT) -> dict:
    entries = {}
    for directory in INCLUDED:
        base = root / directory
        if base.is_symlink():
            raise SymlinkError(base)
        if not base.exists():
            continue
        for path in _walk_regular_files(base):
            entries[path.relative_to(root).as_posix()] = _sha256(path)
    for sibling, dst_rel in projected_sources(root):
        for path in _walk_regular_files(sibling):
            entries[dst_rel + "/" + path.relative_to(sibling).as_posix()] = _sha256(path)
    for src_file, dst_rel in projected_files(root):
        entries[dst_rel] = _sha256(src_file)
    for name in ROOT_FILES:
        entries[name] = _sha256(root / name)
    return dict(sorted(entries.items()))


def is_prose_doc_path(rel_path: str) -> bool:
    """Delegate to the single runtime rule in xiaoba_agentos.release."""
    src_dir = str(ROOT / "src")
    if (ROOT / "src" / "xiaoba_agentos" / "release.py").is_file() and src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    from xiaoba_agentos.release import is_prose_doc_path as _is_prose_doc_path
    return _is_prose_doc_path(rel_path)


def split_manifest_entries(entries: dict) -> tuple[dict, dict]:
    runtime_entries = {k: v for k, v in sorted(entries.items()) if not is_prose_doc_path(k)}
    doc_entries = {k: v for k, v in sorted(entries.items()) if is_prose_doc_path(k)}
    return runtime_entries, doc_entries


def manifest_digest(entries: dict) -> str:
    canonical = json.dumps({"algorithm": "sha256", "files": dict(sorted(entries.items()))},
                           ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def runtime_manifest_sha256_from_file(manifest_path: Path) -> str:
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return ""
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = data.get("files")
        if not isinstance(files, dict) or not files:
            return ""
        runtime_entries, _ = split_manifest_entries(files)
        return manifest_digest(runtime_entries) if runtime_entries else ""
    except (OSError, ValueError, TypeError):
        return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    target = ROOT / MANIFEST_NAME
    if target.is_symlink():
        print(json.dumps({"ok": False, "symlink": str(target)}, ensure_ascii=False))
        return 1
    try:
        entries = collect_entries()
    except SymlinkError as exc:
        print(json.dumps({"ok": False, "symlink": str(exc.path),
                          "error": "managed source contains a symlink"}, ensure_ascii=False))
        return 1
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    payload = {"algorithm": "sha256", "files": entries}
    if args.verify:
        try:
            expected = json.loads(target.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            print(f"manifest unavailable: {exc}")
            return 1
        if expected != payload:
            expected_files = expected.get("files", {})
            changed = sorted(name for name in set(entries) | set(expected_files) if entries.get(name) != expected_files.get(name))
            print(json.dumps({"ok": False, "changed": changed}, ensure_ascii=False, indent=2))
            return 1
        if args.summary:
            rt, dc = split_manifest_entries(entries)
            print(json.dumps({
                "ok": True, "files": len(entries),
                "runtime_files": len(rt), "doc_files": len(dc),
                "source_manifest_sha256": _sha256(target),
                "runtime_manifest_sha256": manifest_digest(rt),
                "doc_manifest_sha256": manifest_digest(dc),
            }, ensure_ascii=False))
        else:
            print(json.dumps({"ok": True, "files": len(entries)}))
        return 0
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
