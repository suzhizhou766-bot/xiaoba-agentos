from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .config import AppConfig


class SkillRegistryError(RuntimeError):
    pass


_DISALLOWED_ACTIVE_CONTEXT_MARKERS = (
    "historical memory:",
    "migration note:",
    "deprecated deployment:",
    "legacy route:",
    "历史记忆：",
    "迁移说明：",
    "弃用部署：",
    "旧路由：",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# 受限实体 metadata：只有列明技能可以声明共享实体，身份与路径规则集中在这里，不散布到调用方。
ENTITY_SKILL_IDS = ("aesthetics",)
ENTITY_SHARED_ROOTS = {"aesthetics": "/Users/suzhizhou/Documents/私人助理/skills"}
ENTITY_FILENAMES = {"aesthetics": "SKILL.md"}
ENTITY_METADATA_KEYS = ("entity_source_path", "entity_source_sha256")


def entity_source_path(skill_id: str) -> str:
    """实体 metadata 的正式路径唯一来源：固定共享技能根下 <id>/<入口文件名>，不接受条目自带值。"""
    if skill_id not in ENTITY_SHARED_ROOTS or skill_id not in ENTITY_FILENAMES:
        raise SkillRegistryError(f"实体 metadata 只允许技能：{', '.join(ENTITY_SKILL_IDS)}")
    return str(Path(ENTITY_SHARED_ROOTS[skill_id]) / skill_id / ENTITY_FILENAMES[skill_id])


def entity_root_for(skill_id: str, entity_root: Optional[Path]) -> Path:
    """实体读取根：缺省即集中声明的正式共享根；显式根只替换根目录，保持写出形态不 resolve。"""
    if skill_id not in ENTITY_SHARED_ROOTS or skill_id not in ENTITY_FILENAMES:
        raise SkillRegistryError(f"实体 metadata 只允许技能：{', '.join(ENTITY_SKILL_IDS)}")
    return Path(ENTITY_SHARED_ROOTS[skill_id]) if entity_root is None else Path(entity_root)


def entity_file_for(skill_id: str, entity_root: Optional[Path]) -> Path:
    """实际校验读取的实体文件：默认正式共享位置；显式 staging/测试根只替换根目录，不替换文件名或相对结构。"""
    return entity_root_for(skill_id, entity_root) / skill_id / ENTITY_FILENAMES[skill_id]


def entity_metadata(entry: Dict[str, Any]) -> List[str]:
    """条目携带的实体键清单：未知 entity_* 键一律视为不合规。"""
    return sorted(
        key for key in entry
        if key.startswith("entity_") and key not in ENTITY_METADATA_KEYS
    )


def declared_relative_path(value: Any, label: str) -> Path:
    """声明的相对路径：拒绝绝对路径、空路径与 .. 段。"""
    path = Path(str(value))
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise SkillRegistryError(f"{label}非法：{value}")
    return path


def guarded_path(root: Path, relative: Path, label: str) -> Path:
    """先按原始路径逐段拒绝符号链接，再 resolve 并确认仍在根内。

    顺序不能颠倒：先 resolve 会把符号链接证据消掉，根内软链就会被当成正常文件。
    """
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SkillRegistryError(f"{label}不得经过符号链接：{current}")
    resolved = current.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise SkillRegistryError(f"{label}越过根目录：{resolved}")
    return resolved


def hashed_file_tree(directory: Path, label: str) -> Dict[str, str]:
    """目录内文件哈希表：文件或目录符号链接一律拒绝；忽略 __pycache__ 与 .DS_Store。"""
    result: Dict[str, str] = {}
    for current, dirnames, filenames in os.walk(directory, followlinks=False):
        base = Path(current)
        for name in list(dirnames):
            candidate = base / name
            if candidate.is_symlink():
                raise SkillRegistryError(f"{label}含符号链接目录：{candidate.relative_to(directory)}")
        for name in filenames:
            candidate = base / name
            relative = candidate.relative_to(directory)
            if "__pycache__" in relative.parts or candidate.name == ".DS_Store":
                continue
            if candidate.is_symlink():
                raise SkillRegistryError(f"{label}含符号链接文件：{relative}")
            result[str(relative)] = _sha256(candidate)
    # 稳定排序：索引字节与遍历顺序无关，任何等价副本重导都逐字节相同。
    return dict(sorted(result.items()))


def guarded_root_path(root: Path, label: str) -> Path:
    """根路径按写出顺序逐段 lstat：任何一段是符号链接都拒绝，包括根本身、上级段与中间多级段。

    与 guarded_path 相反，这里绝不先 resolve，也不返回 resolve 后的路径：先 resolve 会把软链
    证据消掉，`alias/nested`（alias 指向真实目录）会被当成正常根。逐段前缀是实际被访问的路径，
    所以 `..` 写法同样拦得住：`link/x/..` 与 `../link/x` 都会在 `link` 那一段被拒绝，跨段无法
    消除证据。契约：根必须是真实目录链，不接受任何软链段；也不接受空路径（`.` 之类没有可检查
    段，位置不明）。绝对根会一直检查到文件系统根部，正式默认根因此同样受这条约束。
    """
    parts = Path(root).parts
    if not parts:
        raise SkillRegistryError(f"{label}必须是明确的非空路径：{root}")
    if Path(root).is_absolute():
        current = Path(parts[0])
        start = 1
    else:
        current = Path()
        start = 0
    for part in parts[start:]:
        current = current / part
        if current.is_symlink():
            raise SkillRegistryError(f"{label}不得为符号链接：{current}")
    return Path(root)


def verified_entity(skill_id: str, entity_root: Optional[Path]) -> tuple:
    """受限实体读取的唯一入口：根各段、目录与入口逐段拒绝符号链接，目录内恰为入口文件，返回 (路径, 哈希)。

    索引生成与运行核验共用这一处约束；可信 staging 根只替换根目录，不改变任何规则。
    缺省正式根与显式根走同一个 guarded_root_path，不存在只校验显式根的分支。
    """
    root = guarded_root_path(entity_root_for(skill_id, entity_root), "共享实体根")
    entity_file = root / skill_id / ENTITY_FILENAMES[skill_id]
    entity_dir = entity_file.parent
    guarded_path(entity_dir.parent, Path(skill_id), "共享实体目录")
    if entity_dir.is_symlink() or not entity_dir.is_dir():
        raise SkillRegistryError(f"共享实体目录缺失或为符号链接：{skill_id}")
    if entity_file.is_symlink() or not entity_file.is_file():
        raise SkillRegistryError(f"共享实体入口缺失或为符号链接：{skill_id}")
    files = hashed_file_tree(entity_dir, "共享实体目录")
    if set(files) != {entity_file.name}:
        raise SkillRegistryError(f"共享实体目录文件集合不合规：{skill_id}: {', '.join(sorted(files))}")
    return entity_file, files[entity_file.name]


def _public_entry(entry: Dict[str, Any], score: Optional[int] = None) -> Dict[str, Any]:
    result = {
        "id": entry["id"],
        "name": entry["name"],
        "description": entry["description"],
        "category": entry["category"],
        "capabilities": list(entry["capabilities"]),
        "status": entry["status"],
        "callable": bool(entry["callable"]),
    }
    if score is not None:
        result["score"] = score
    return result


class SkillRegistry:
    def __init__(self, config: AppConfig, entity_root: Optional[Path] = None):
        self.config = config
        self.entity_root = Path(entity_root) if entity_root is not None else None

    def _load(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.config.skills_index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SkillRegistryError(f"无法读取技能索引：{exc}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise SkillRegistryError("技能索引 schema_version 必须为 1")
        if not isinstance(value.get("entries"), list):
            raise SkillRegistryError("技能索引缺少 entries")
        return value

    def _skill_file(self, entry: Dict[str, Any]) -> Path:
        skill_id = str(entry.get("id", "<unknown>"))
        relative = declared_relative_path(entry.get("directory", ""), f"技能目录 {skill_id}")
        return guarded_path(self.config.skills_root, relative / "SKILL.md", f"技能入口 {skill_id}")

    def verify(self) -> Dict[str, Any]:
        try:
            index = self._load()
            problems: List[str] = []
            ids = set()
            names = set()
            callable_count = 0
            for entry in index["entries"]:
                if not isinstance(entry, dict):
                    problems.append("技能条目必须是对象")
                    continue
                skill_id = str(entry.get("id", ""))
                name = str(entry.get("name", ""))
                if not skill_id or skill_id in ids:
                    problems.append(f"技能 id 为空或重复：{skill_id}")
                if not name or name in names:
                    problems.append(f"技能 name 为空或重复：{name}")
                ids.add(skill_id)
                names.add(name)
                if entry.get("status") != "active" or entry.get("callable") is not True:
                    problems.append(f"正式索引只允许 active/callable 技能：{skill_id}")
                if not entry.get("triggers") or not entry.get("capabilities"):
                    problems.append(f"技能缺少触发词或能力：{skill_id}")
                try:
                    skill_file = self._skill_file(entry)
                except SkillRegistryError as exc:
                    problems.append(str(exc))
                    continue
                if skill_file.is_symlink() or not skill_file.is_file():
                    problems.append(f"技能入口缺失或为符号链接：{skill_id}")
                    continue
                if _sha256(skill_file) != str(entry.get("entrypoint_sha256", "")):
                    problems.append(f"技能入口哈希不匹配：{skill_id}")
                try:
                    content = skill_file.read_text(encoding="utf-8")
                except OSError as exc:
                    problems.append(f"无法读取技能入口 {skill_id}：{exc}")
                    continue
                lowered = content.lower()
                found = [marker for marker in _DISALLOWED_ACTIVE_CONTEXT_MARKERS if marker in lowered]
                if found:
                    problems.append(f"技能含非当前态内容：{skill_id}: {', '.join(found)}")
                if len(content) > 30000:
                    problems.append(f"技能入口超过 30000 字符：{skill_id}")
                files = entry.get("files_sha256")
                if not isinstance(files, dict) or not files:
                    problems.append(f"技能缺少文件清单：{skill_id}")
                    continue
                skill_dir = skill_file.parent
                try:
                    actual_files = hashed_file_tree(skill_dir, f"技能目录 {skill_id}")
                except SkillRegistryError as exc:
                    problems.append(str(exc))
                    continue
                if set(actual_files) != set(files):
                    problems.append(f"技能文件集合不匹配：{skill_id}")
                    continue
                for relative, expected in files.items():
                    if actual_files.get(relative) != expected:
                        problems.append(f"技能文件哈希不匹配：{skill_id}/{relative}")
                if entity_metadata(entry) or any(key in entry for key in ENTITY_METADATA_KEYS):
                    problems.extend(self._verify_entity(entry, skill_file))
                callable_count += 1
            problems.extend(self._publication_problems(index))
            return {
                "ok": not problems,
                "index": str(self.config.skills_index),
                "root": str(self.config.skills_root),
                "skills": len(index["entries"]),
                "callable": callable_count,
                "entity_root": str(self.entity_root) if self.entity_root is not None else None,
                "entity_root_mode": "staging" if self.entity_root is not None else "formal",
                "problems": problems,
            }
        except SkillRegistryError as exc:
            return {
                "ok": False,
                "index": str(self.config.skills_index),
                "root": str(self.config.skills_root),
                "skills": 0,
                "callable": 0,
                "entity_root": str(self.entity_root) if self.entity_root is not None else None,
                "entity_root_mode": "staging" if self.entity_root is not None else "formal",
                "problems": [str(exc)],
            }

    def _publication_problems(self, index: Dict[str, Any]) -> List[str]:
        """技能在包外，索引谁都能改：每个条目要么与随包基线一致，要么带着核对得过的审查任务。"""
        from . import content_index

        project_dir = getattr(self.config, "project_dir", None)
        if project_dir is None:
            return []
        try:
            # The same reviewers skill-publish checked: enabled primaries only. A
            # registered standby never reviewed these skills, and counting it made
            # every published skill fail once the standby was enabled (round-50
            # rehearsal).
            reviewers = [agent.name for agent in getattr(self.config, "agents", [])
                         if agent.enabled and not getattr(agent, "standby", False)]
            return content_index.publication_problems(
                index, project_dir, self.config.skills_root,
                getattr(self.config, "database_path", Path("/nonexistent")), reviewers)
        except content_index.ContentIndexError as exc:
            return [str(exc)]

    def _verify_entity(self, entry: Dict[str, Any], skill_file: Path) -> List[str]:
        """受限实体一致性：路径只认集中声明；两份副本逐字节相同；目录不越界、无符号链接、无额外文件。"""
        skill_id = str(entry.get("id", ""))
        problems: List[str] = []
        unknown = entity_metadata(entry)
        if unknown:
            problems.append(f"实体 metadata 不合规：{skill_id}: {', '.join(unknown)}")
        for key in ENTITY_METADATA_KEYS:
            if key not in entry:
                problems.append(f"实体 metadata 缺少 {key}：{skill_id}")
        try:
            expected_path = entity_source_path(skill_id)
        except SkillRegistryError as exc:
            problems.append(f"实体 metadata 不合规：{skill_id}: {exc}")
            return problems
        if str(entry.get("entity_source_path", "")) != expected_path:
            problems.append(f"实体 metadata 路径不合规：{skill_id}: 只允许 {expected_path}")
            return problems
        try:
            _entity_file, entity_sha = verified_entity(skill_id, self.entity_root)
        except SkillRegistryError as exc:
            problems.append(str(exc))
            return problems
        if entity_sha != str(entry.get("entity_source_sha256", "")):
            problems.append(f"共享实体哈希不匹配：{skill_id}")
        if entity_sha != _sha256(skill_file):
            problems.append(f"共享实体与发布副本不一致：{skill_id}")
        return problems

    def entries(self) -> List[Dict[str, Any]]:
        verification = self.verify()
        if not verification["ok"]:
            raise SkillRegistryError("；".join(verification["problems"]))
        return list(self._load()["entries"])

    def list(self) -> List[Dict[str, Any]]:
        return [_public_entry(entry) for entry in self.entries()]

    def match(self, prompt: str) -> List[Dict[str, Any]]:
        normalized = re.sub(r"\s+", " ", prompt.casefold()).strip()
        ranked = []
        for entry in self.entries():
            score = 0
            for trigger in entry["triggers"]:
                needle = str(trigger).casefold().strip()
                if needle and needle in normalized:
                    score += max(2, len(needle))
            name = str(entry["name"]).casefold()
            if name and name in normalized:
                score += len(name) + 3
            if score:
                ranked.append((score, str(entry["id"]), entry))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        maximum = int(self.config.skills["max_selected"])
        return [_public_entry(entry, score) for score, _skill_id, entry in ranked[:maximum]]

    def context(self, selected: Iterable[Dict[str, Any]]) -> str:
        by_id = {entry["id"]: entry for entry in self.entries()}
        blocks = []
        for item in selected:
            skill_id = str(item["id"])
            entry = by_id.get(skill_id)
            if not entry:
                raise SkillRegistryError(f"技能不在正式索引中：{skill_id}")
            skill_file = self._skill_file(entry)
            blocks.append(
                f"## 已验证技能：{entry['name']}\n"
                f"入口：{skill_file}\n"
                f"{skill_file.read_text(encoding='utf-8').strip()}"
            )
        if not blocks:
            return ""
        return "正式 v1.4 能力上下文（只执行匹配技能，不扩展权限）：\n\n" + "\n\n".join(blocks)

    def select_context(self, prompt: str) -> Dict[str, Any]:
        selected = self.match(prompt)
        context = self.context(selected)
        return {
            "selected": selected,
            "context": context,
            "context_sha256": hashlib.sha256(context.encode("utf-8")).hexdigest(),
        }
