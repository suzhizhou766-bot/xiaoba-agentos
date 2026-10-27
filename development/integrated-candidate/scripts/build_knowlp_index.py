#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


def _read_retained_ledger(ledger: Path | None) -> set:
    """Absolute paths of objects the memory rollback retained (v1.5.5 plan C).

    The ledger is a metadata-only JSONL journal, one object per line.  It is
    *additive* to the name-based rule below: a path listed here is never a graph
    node even if it somehow looks like a note, so isolation does not rest on
    guessing from a suffix.  A missing or unreadable ledger degrades to the name
    rule instead of failing the build, and a malformed line is skipped rather
    than dropping the whole file.
    """
    if ledger is None:
        return set()
    paths = set()
    try:
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return paths
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        value = str(record.get("retained_path") or "").strip()
        if value:
            paths.add(_ledger_key(value))
    return paths


def _ledger_key(path: object) -> str:
    """Canonical spelling for ledger and vault paths.

    ``realpath``, not ``abspath``: on macOS ``/var`` is a symlink to
    ``/private/var``, and the runtime directory is resolved while the vault may be
    given unresolved, so the same object would otherwise carry two keys and the
    ledger gate would silently never match.
    """
    return os.path.realpath(os.path.expanduser(str(path)))


def _is_retained(path: Path, vault: Path, retained: set) -> bool:
    """A retained object is never a graph node.

    Two independent gates: the private recovery name (``.<original>.<pid>.rollback``
    or ``.restore``) and the metadata ledger of retained objects.  Neither depends
    on the note suffix, and the ledger gate is what makes isolation survive a name
    that looks exactly like a note.
    """
    name = path.name
    if name.startswith(".") and re.search(r"\.\d+\.(rollback|restore)$", name):
        return True
    try:
        return _ledger_key(path) in retained
    except OSError:
        return False


def _metadata(vault: Path, retained: set | None = None) -> list:
    retained = retained or set()
    items = []
    for path in sorted(vault.rglob("*.md")):
        relative = path.relative_to(vault)
        if any(part.startswith(".") for part in relative.parts) or path.is_symlink():
            continue
        if _is_retained(path, vault, retained):
            continue
        text = path.read_text(encoding="utf-8")
        fm = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
        frontmatter = {}
        body = text
        if fm:
            for line in fm.group(1).splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    frontmatter[key.strip()] = value.strip().strip('"').strip("'")
            body = text[fm.end():]
        # Non-active notes remain recoverable in the Vault, but are not graph
        # nodes, chunks, summaries, or retrieval candidates.  The active tree
        # must therefore also remove any parent link to them.
        if frontmatter.get("status") != "active":
            continue
        headings = re.findall(r"^#{1,4}\s+(.+)$", body, re.MULTILINE)
        links = re.findall(r"\[\[([^\]|#]+)(?:[#|][^\]]+)?\]\]", body)
        paragraphs = [
            value.strip()
            for value in re.split(r"\n\s*\n", body)
            if value.strip() and not value.strip().startswith("#")
        ]
        cleaned = re.sub(r"\[\[([^\]]+)\]\]", r"\1", body)
        cleaned = re.sub(r"```[\s\S]*?```", "", cleaned)
        cleaned = re.sub(r"^#{1,4}\s+.*$", "", cleaned, flags=re.MULTILINE).strip()
        chunks = []
        for index in range(0, len(cleaned), 1600):
            chunk = cleaned[index:index + 1600].strip()
            if len(chunk) >= 80:
                chunks.append(
                    {
                        "id": f"{path.stem}#{len(chunks)}",
                        "text": chunk,
                        "headings": headings[:4],
                        "note_name": path.stem,
                    }
                )
        items.append(
            {
                "name": path.stem,
                "path": str(relative),
                "tags": [],
                "headings": headings[:8],
                "summary": (paragraphs[0] if paragraphs else "")[:300],
                "size": len(text),
                "wikilinks": links,
                "chunks": chunks,
                "frontmatter": frontmatter,
            }
        )
    return items


def diagnose(vault: Path, retained: set | None = None) -> dict:
    meta = _metadata(vault, retained)
    notes_count = len(meta)
    by_link = {str(Path(item["path"]).with_suffix("")): item for item in meta}
    id_map: dict[str, list] = {}
    for item in meta:
        node_id = item["frontmatter"].get("id", "").strip()
        if node_id:
            id_map.setdefault(node_id, []).append(item)

    total_links = sum(len(item["wikilinks"]) for item in meta)
    expected_links = notes_count - 1
    problems = []

    # missing_id & duplicate_id
    for item in meta:
        node_id = item["frontmatter"].get("id", "").strip()
        if not node_id:
            problems.append(
                {
                    "kind": "missing_id",
                    "note": item["path"],
                    "target": "",
                    "detail": "缺少 id，请在 frontmatter 中添加唯一的 id 字段",
                }
            )

    for node_id, items in id_map.items():
        if len(items) > 1:
            for item in items:
                problems.append(
                    {
                        "kind": "duplicate_id",
                        "note": item["path"],
                        "target": node_id,
                        "detail": f"id '{node_id}' 重复，请修改为唯一 id",
                    }
                )

    # multiple_roots & no_root
    roots = [item for item in meta if not item["frontmatter"].get("parent", "").strip()]
    if len(roots) == 0:
        problems.append(
            {
                "kind": "no_root",
                "note": "",
                "target": "",
                "detail": "未找到根笔记，整棵树必须有一个 parent 为空的根笔记",
            }
        )
    elif len(roots) > 1:
        for item in roots:
            problems.append(
                {
                    "kind": "multiple_roots",
                    "note": item["path"],
                    "target": "",
                    "detail": "存在多个根笔记，请为非主根笔记指定 parent",
                }
            )

    # missing_parent
    for item in meta:
        parent = item["frontmatter"].get("parent", "").strip()
        if parent and parent not in id_map:
            problems.append(
                {
                    "kind": "missing_parent",
                    "note": item["path"],
                    "target": parent,
                    "detail": f"引用的父笔记 id '{parent}' 不存在，请修正 parent 字段或创建对应笔记",
                }
            )

    # unresolved_link, extra_link, duplicate_link
    for item in meta:
        source_id = item["frontmatter"].get("id", "").strip()
        seen_child_targets = set()
        for link in item["wikilinks"]:
            target_item = by_link.get(link)
            if not target_item:
                problems.append(
                    {
                        "kind": "unresolved_link",
                        "note": item["path"],
                        "target": link,
                        "detail": f"链接目标 '{link}' 不存在或未激活，请修正链接路径或移除",
                    }
                )
                continue

            target_parent = target_item["frontmatter"].get("parent", "").strip()
            if not source_id or target_parent != source_id:
                problems.append(
                    {
                        "kind": "extra_link",
                        "note": item["path"],
                        "target": link,
                        "detail": f"链接目标 '{link}' 不是直接子笔记，第二大脑为严格树结构，请移除横向或逆向链接",
                    }
                )
            else:
                if link in seen_child_targets:
                    problems.append(
                        {
                            "kind": "duplicate_link",
                            "note": item["path"],
                            "target": link,
                            "detail": f"父笔记多次链接了子笔记 '{link}'，请保留单次链接",
                        }
                    )
                else:
                    seen_child_targets.add(link)

    # missing_parent_link
    for item in meta:
        parent = item["frontmatter"].get("parent", "").strip()
        if not parent or parent not in id_map or len(id_map[parent]) != 1:
            continue
        parent_item = id_map[parent][0]
        expected = str(Path(item["path"]).with_suffix(""))
        if parent_item["wikilinks"].count(expected) == 0:
            problems.append(
                {
                    "kind": "missing_parent_link",
                    "note": parent_item["path"],
                    "target": expected,
                    "detail": f"父笔记未链接直接子笔记 '{expected}'，请在父笔记中添加 [[{expected}]]",
                }
            )

    return {
        "ok": len(problems) == 0,
        "notes": notes_count,
        "links": total_links,
        "expected_links": expected_links,
        "problems": problems,
    }


def _format_error(message: str, vault: Path, retained: set | None = None) -> str:
    diag = diagnose(vault, retained)
    problems = diag.get("problems", [])
    if not problems:
        return message
    suffix = "".join(
        f"；问题：{p['note']} -> {p['target']}：{p['detail']}"
        for p in problems[:5]
    )
    return message + suffix


def build(vault: Path, retained: set | None = None) -> dict:
    meta = _metadata(vault, retained)
    by_id = {item["frontmatter"].get("id", ""): item for item in meta}
    by_link = {str(Path(item["path"]).with_suffix("")): item for item in meta}
    if len(by_id) != len(meta) or "" in by_id:
        raise RuntimeError(_format_error("第二大脑 id 缺失或重复", vault, retained))
    roots = [item for item in meta if not item["frontmatter"].get("parent")]
    if len(roots) != 1:
        raise RuntimeError(_format_error("第二大脑必须只有一个根", vault, retained))
    prerequisite = {}
    weights = {}
    edge_count = 0
    for item in meta:
        source = item["name"]
        targets = []
        for link in item["wikilinks"]:
            target = by_link.get(link)
            if not target:
                raise RuntimeError(
                    _format_error(f"未解析 WikiLink：{item['path']} -> {link}", vault, retained)
                )
            targets.append(target["name"])
            weights[f"{source}||{target['name']}"] = {
                "type": "prerequisite",
                "weight": 1.0,
                "use_count": 0,
                "tag": "decree" if item["frontmatter"].get("authority") == "true" else "default",
                "source": "v1.4_tree",
            }
            edge_count += 1
        if targets:
            prerequisite[source] = targets
    if edge_count != len(meta) - 1:
        raise RuntimeError(
            _format_error(
                f"结构边数量错误：{edge_count}，预期 {len(meta) - 1}", vault
            )
        )
    for item in meta:
        parent = item["frontmatter"].get("parent", "")
        if not parent:
            continue
        if parent not in by_id:
            raise RuntimeError(_format_error(f"父节点不存在：{item['path']}", vault, retained))
        parent_item = by_id[parent]
        expected = str(Path(item["path"]).with_suffix(""))
        if parent_item["wikilinks"].count(expected) != 1:
            raise RuntimeError(
                _format_error(f"父节点未唯一链接子节点：{item['path']}", vault, retained)
            )
    compact_meta = [
        {key: value for key, value in item.items() if key != "frontmatter"}
        for item in meta
    ]
    return {
        "dual_graph.json": {"prerequisite": prerequisite, "similarity": {}, "weights": weights},
        "meta_index.json": compact_meta,
        "deep_extraction_prep.json": {
            "method": "v1.4_structural_tree_only",
            "analyzed_docs": 0,
            "nodes": len(meta),
            "edges": edge_count,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--graph-dir", type=Path, required=False, default=None)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--retained-ledger", type=Path, required=False, default=None)
    args = parser.parse_args()
    vault = args.vault.resolve()
    if not vault.is_dir() or vault.is_symlink():
        raise SystemExit("vault must be a real directory")
    if args.diagnose:
        res = diagnose(vault, _read_retained_ledger(args.retained_ledger))
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if not args.graph_dir:
        parser.error("the following arguments are required: --graph-dir")
    graph_dir = args.graph_dir.resolve()
    generated = build(vault, _read_retained_ledger(args.retained_ledger))
    rendered = {
        name: json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        for name, value in generated.items()
    }
    if args.verify:
        for name, content in rendered.items():
            path = graph_dir / name
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                raise SystemExit(f"KnowLP index differs: {name}")
        print(json.dumps({"ok": True, **generated["deep_extraction_prep.json"]}, ensure_ascii=False))
        return 0
    graph_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(graph_dir, 0o700)
    for name, content in rendered.items():
        target = graph_dir / name
        temporary = graph_dir / f".{name}.{os.getpid()}.tmp"
        temporary.write_text(content, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    print(json.dumps({"ok": True, **generated["deep_extraction_prep.json"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
