"""第二大脑里由内容索引派生的笔记：技能卡与价值文件索引。

这些笔记不随软件发布生成。技能索引或价值文件索引变化后运行同步，笔记即与索引一致。
同步只改写笔记里由索引决定的那一段；这一段之后用户追加的内容原样保留；
认不出格式的笔记不覆盖，只报告。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

DATE = "2026-09-26"
CAPABILITIES_BRANCH = "20_能力与技能.md"
VALUE_INDEX_NOTE = "50_资产与索引/价值文件索引.md"
# 这四个分支及其下属笔记归笔记库自己管理，发布不生成、不合并、不覆盖。
CONTENT_BRANCHES = ("20_能力与技能", "30_方法与知识", "40_工作流", "50_资产与索引")
_SKILL_TAIL = re.compile(r"^入口哈希：`[0-9a-f]{64}`\n", re.MULTILINE)
_VALUE_LINE = re.compile(r"^- `[^`\n]+` · [a-z_]+ · `[^`\n]+` · `[0-9a-f]{64}`\n", re.MULTILINE)
_VALUE_MARKER = "正式目录只包含当前验证通过的来源：\n\n"


class ContentNotesError(RuntimeError):
    pass


def frontmatter(node: Dict[str, Any], source_path: str = "") -> str:
    return (
        "---\n"
        f"id: {json.dumps(node['id'], ensure_ascii=False)}\n"
        f"type: {json.dumps(node['type'], ensure_ascii=False)}\n"
        f"parent: {json.dumps(node['parent'], ensure_ascii=False)}\n"
        "status: \"active\"\n"
        f"authority: {'true' if node['authority'] else 'false'}\n"
        f"source_path: {json.dumps(source_path, ensure_ascii=False)}\n"
        f"last_verified: {json.dumps(node.get('last_verified', DATE))}\n"
        "---\n\n"
    )


def render(node: Dict[str, Any], source_path: str = "", version: str = "") -> str:
    title = node["title"].replace("{version}", version)
    body = node["body"].replace("{version}", version)
    text = frontmatter(node, source_path) + f"# {title}\n\n{body.strip()}\n"
    children = node.get("children", [])
    if children:
        text += "\n" + "\n".join(f"- [[{child}]]" for child in children) + "\n"
    return text


def skill_card(entry: Dict[str, Any], source_path: str) -> Dict[str, str]:
    node = {
        "id": "skill-" + entry["id"],
        "type": "skill-card",
        "parent": "capabilities",
        "authority": False,
        "title": entry["name"],
        "body": (
            f"状态：active / callable\n\n类别：{entry['category']}\n\n"
            f"能力：{', '.join(entry['capabilities'])}\n\n{entry['description']}\n\n"
            f"入口哈希：`{entry['entrypoint_sha256']}`"
        ),
    }
    return {"path": f"20_能力与技能/{entry['name']}.md", "text": render(node, source_path)}


def value_index(entries: List[Dict[str, Any]]) -> Dict[str, str]:
    node = {
        "id": "value-files",
        "type": "asset-card",
        "parent": "assets",
        "authority": False,
        "title": "价值文件索引",
        "body": "正式目录只包含当前验证通过的来源：\n\n" + "\n".join(
            f"- `{entry['id']}` · {entry['index_mode']} · `{entry['path']}` · `{entry['sha256']}`"
            for entry in entries
        ),
    }
    return {"path": VALUE_INDEX_NOTE, "text": render(node)}


def _entries(path: Path, label: str) -> List[Dict[str, Any]]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContentNotesError(f"无法读取{label}：{exc}") from exc
    entries = value.get("entries") if isinstance(value, dict) else None
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise ContentNotesError(f"{label}缺少 entries")
    return entries


def derived_notes(skills_index: Path, skills_root: Path, knowledge_index: Path) -> List[Dict[str, str]]:
    """当前两份索引决定的全部派生笔记，按路径排序。"""
    notes = []
    for entry in _entries(skills_index, "技能索引"):
        name = str(entry.get("name", ""))
        if not name or name in (".", "..") or any(part in name for part in ("/", "\\", "\n", "[", "]")):
            raise ContentNotesError(f"技能名不能用作笔记名：{name!r}")
        source = str(entry.get("entity_source_path") or Path(skills_root) / entry["entrypoint"])
        notes.append(skill_card(entry, source))
    notes.append(value_index(_entries(knowledge_index, "价值文件索引")))
    return sorted(notes, key=lambda item: item["path"])


def _generated_end(path: str, text: str) -> Optional[int]:
    """笔记里由索引决定的那一段到哪里结束；认不出格式返回 None。"""
    if path == VALUE_INDEX_NOTE:
        # 只认紧跟在说明句之后、连续不断的那一段列表。列表后面的任何文字（包括再往后
        # 出现的同格式条目）都属于用户，不在这一段里。
        if text.count(_VALUE_MARKER) != 1:
            return None
        end = text.index(_VALUE_MARKER) + len(_VALUE_MARKER)
        while True:
            match = _VALUE_LINE.match(text, end)
            if match is None:
                return end
            end = match.end()
    matches = list(_SKILL_TAIL.finditer(text))
    return matches[0].end() if len(matches) == 1 else None


def _merged(path: str, generated: str, live: Optional[str]) -> Optional[str]:
    """把新生成的一段与线上笔记合并；返回 None 表示线上笔记不是可识别的派生笔记，不得覆盖。"""
    if live is None:
        return generated
    identity = re.search(r'^id: "[^"]+"$', generated, re.MULTILINE).group(0)
    if identity not in live.split("\n---\n", 1)[0].splitlines():
        return None
    end = _generated_end(path, live)
    if end is None:
        return None
    kept = ""
    if path == VALUE_INDEX_NOTE:
        # 列表里不在索引中的行是人工补的条目：原样保留在生成列表之后，不删除。
        listed = {match.group(0) for match in _VALUE_LINE.finditer(generated)}
        ids = {line.split("`")[1] for line in listed}
        kept = "".join(
            match.group(0) for match in _VALUE_LINE.finditer(live[:end])
            if match.group(0) not in listed and match.group(0).split("`")[1] not in ids
        )
    return generated + kept + live[end:]


def plan(vault: Path, notes: List[Dict[str, str]]) -> Dict[str, Any]:
    """比对笔记库与派生笔记，给出要写的内容；不写任何东西。"""
    vault = Path(vault)
    writes: List[Dict[str, str]] = []
    problems: List[str] = []
    for note in notes:
        destination = vault / note["path"]
        if destination.is_symlink():
            problems.append(f"派生笔记是符号链接，拒绝写入：{note['path']}")
            continue
        live = destination.read_text(encoding="utf-8") if destination.is_file() else None
        merged = _merged(note["path"], note["text"], live)
        if merged is None:
            problems.append(f"线上笔记不是可识别的派生笔记，未覆盖：{note['path']}")
        elif merged != live:
            writes.append({"path": note["path"], "text": merged, "action": "create" if live is None else "update"})
    branch = vault / CAPABILITIES_BRANCH
    cards = [note["path"][:-3] for note in notes if note["path"].startswith("20_能力与技能/")]
    if branch.is_symlink() or not branch.is_file():
        problems.append(f"缺少分支笔记：{CAPABILITIES_BRANCH}")
    else:
        text = branch.read_text(encoding="utf-8")
        links = re.findall(r"\[\[([^\]]+)\]\]", text)
        missing = [card for card in cards if links.count(card) == 0]
        duplicated = [card for card in cards if links.count(card) > 1]
        if duplicated:
            problems.append("分支笔记里技能卡链接重复：" + "、".join(duplicated))
        elif missing:
            updated = text if text.endswith("\n") else text + "\n"
            updated += "".join(f"- [[{card}]]\n" for card in missing)
            writes.append({"path": CAPABILITIES_BRANCH, "text": updated, "action": "update"})
    return {"ok": not problems, "in_sync": not problems and not writes, "writes": writes, "problems": problems}


def apply(vault: Path, planned: Dict[str, Any]) -> List[str]:
    if not planned.get("ok"):
        raise ContentNotesError("；".join(planned.get("problems") or ["同步计划无效"]))
    written = []
    for item in planned["writes"]:
        destination = Path(vault) / item["path"]
        if destination.parent.is_symlink() or not destination.parent.is_dir():
            raise ContentNotesError(f"笔记所在目录不存在或是符号链接：{item['path']}")
        temporary = destination.with_name("." + destination.name + ".content-sync")
        temporary.write_text(item["text"], encoding="utf-8")
        temporary.replace(destination)
        written.append(item["path"])
    return written
