#!/usr/bin/env python3
"""生成或校验价值文件索引（内容，不随发布）。正式入口是 `xiaoba knowledge-index-rebuild`。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xiaoba_agentos.content_index import ContentIndexError, build_knowledge_index, render, write_index


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True, help="价值文件索引目录（其下有 catalog.json）")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    target = root / "index.json"
    try:
        generated = build_knowledge_index(root)
    except ContentIndexError as exc:
        raise SystemExit(str(exc)) from exc
    if args.verify:
        if not target.is_file() or target.read_text(encoding="utf-8") != render(generated):
            raise SystemExit("knowledge index is stale")
        print(f"verified={target}")
        return 0
    write_index(target, generated)
    print(f"created={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
