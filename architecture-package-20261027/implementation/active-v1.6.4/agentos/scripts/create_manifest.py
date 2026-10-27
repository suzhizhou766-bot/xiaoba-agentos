#!/usr/bin/env python3
"""Create a deterministic, non-secret integrity manifest for project source files."""

import hashlib
import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INCLUDED = (".agents", "bin", "config", "docs", "knowledge", "launchd", "macos", "scripts", "skills", "src", "tests")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    entries = {}
    for directory in INCLUDED:
        for path in sorted((ROOT / directory).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.name != ".DS_Store":
                entries[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("README.md", "pyproject.toml", ".gitignore", "CAPABILITY.json"):
        path = ROOT / name
        entries[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    target = ROOT / "SOURCE_MANIFEST.json"
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
