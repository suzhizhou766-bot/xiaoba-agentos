#!/usr/bin/env python3
"""Create a deterministic, non-secret integrity manifest for project source files."""

import hashlib
import argparse
import json
from pathlib import Path


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


def review_source(root: Path):
    """Only the runtime package is projected; development fixtures never enter a release."""
    packaged = root / 'src/xiaoba_review'
    sibling = root.parent / 'review-ingress/xiaoba_review'
    for path in (packaged, sibling.parent, sibling):
        if path.is_symlink():
            raise SymlinkError(path)
    if packaged.exists():
        return None
    if sibling.exists():
        for path in (sibling.parent, sibling, *sibling.rglob('*')):
            if path.is_symlink():
                raise SymlinkError(path)
        return sibling
    return None


def collect_entries(root: Path = ROOT) -> dict:
    entries = {}
    for directory in INCLUDED:
        base = root / directory
        if not base.exists():
            continue
        if base.is_symlink():
            raise SymlinkError(base)
        for path in sorted(base.rglob("*")):
            if path.is_symlink():
                raise SymlinkError(path)
            if path.is_file() and "__pycache__" not in path.parts and path.name != ".DS_Store":
                entries[str(path.relative_to(root))] = _sha256(path)
    review = review_source(root)
    if review is not None:
        for path in sorted(review.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.name != '.DS_Store' and path.suffix != '.pyc':
                entries['src/xiaoba_review/' + path.relative_to(review).as_posix()] = _sha256(path)
    for name in ROOT_FILES:
        entries[name] = _sha256(root / name)
    return entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
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
        print(json.dumps({"ok": True, "files": len(entries)}, ensure_ascii=False))
        return 0
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
