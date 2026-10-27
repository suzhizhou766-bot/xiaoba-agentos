#!/usr/bin/env python3
"""校验技能索引是否与技能目录一致（只读）。写索引的唯一入口是 `xiaoba skill-publish`，它核对审查任务。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xiaoba_agentos.content_index import ContentIndexError, build_skill_index, render


def build(root: Path, entity_root: Path | None = None) -> dict:
    try:
        return build_skill_index(root, entity_root)
    except ContentIndexError as exc:
        raise SystemExit(str(exc)) from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True, help="技能根目录（其下有 catalog.json 与各技能目录）")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--entity-root", type=Path, help="仅开发/验收用：替换共享实体根目录（如 staging 实体）")
    args = parser.parse_args()
    root = args.root.resolve()
    # 保留原始路径：先按原始路径检查符号链接，再 resolve，避免 resolve 消除软链证据。
    entity_root = Path(args.entity_root) if args.entity_root else None
    target = root / "index.json"
    generated = build(root, entity_root)
    if args.verify:
        # 经审查发布的技能，索引条目里记着审查任务编号；那不是由目录生成的，比对时不计。
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
            for entry in current.get("entries", []):
                entry.pop("review_task", None)
        except (OSError, ValueError, AttributeError):
            raise SystemExit("skill index is stale")
        if render(current) != render(generated):
            raise SystemExit("skill index is stale")
        print(f"verified={target}")
        return 0
    raise SystemExit("本脚本只做校验（--verify）。发布技能改动请用 xiaoba skill-publish：它核对审查任务后才写索引。")


if __name__ == "__main__":
    raise SystemExit(main())
