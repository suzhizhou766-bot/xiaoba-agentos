#!/usr/bin/env python3
"""只读盘点：扫描工作体系资产并进行明确分类。

输出明确区分为三类：
1. confirmed_unregistered ("确定漏登记")：功能组件缺少必要登记凭据，且无已决议的不接入依据。
2. manual_review ("待人工判断")：纯文档目录、日期会话输出目录、小八审美/taste 长期维护探索内容等未知或需人工决策项。
3. decided_excluded ("已决定不接入")：包内可审计的已有明确决定不随 AgentOS 发布的组件（范围准确，不等同于全体系不可用）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


INTERNAL_TOP_LEVEL = {".runtime", "skills", "工具", "工作区", "第二大脑", "系统", "配置"}
DATE_DIRECTORY = re.compile(r"20\d{2}-\d{2}-\d{2}$")
ASSET_NOTE = re.compile(r"资料入口|模块")
TASTE_OR_AESTHETICS = re.compile(r"审美|taste|视觉|摄影", re.IGNORECASE)
DOC_EXTENSIONS = {".md", ".markdown", ".txt", ".rst", ".doc", ".docx", ".pdf"}
CODE_EXTENSIONS = {".py", ".sh", ".bash", ".zsh", ".js", ".cjs", ".mjs", ".ts", ".jsx", ".tsx"}

EVIDENCE_NAMES = ("agent_entry", "brain_entry", "skill_index", "registry")
REQUIRED_EVIDENCE = {
    "top_level": ("agent_entry", "brain_entry", "registry"),
    "top_level_doc": ("agent_entry",),
    "top_level_deep": (),
    "top_level_unknown": (),
    "tool": ("agent_entry", "brain_entry", "registry"),
    "shared_skill": ("brain_entry", "skill_index"),
    "agent_skill": ("brain_entry", "skill_index"),
    "codex_output": (),
    "asset_experience": (),
}

CATEGORY_CONFIRMED = "confirmed_unregistered"  # 确定漏登记
CATEGORY_MANUAL = "manual_review"              # 待人工判断
CATEGORY_EXCLUDED = "decided_excluded"          # 已决定不接入
CATEGORY_REGISTERED = "fully_registered"        # 正常已登记


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _json(path: Path) -> dict:
    try:
        value = json.loads(_text(path))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _validate_safe_path(root: Path, target: Path | str, allow_within_root_only: bool = True) -> Path:
    p = Path(target)
    if any(part in (".", "..") for part in p.parts):
        raise ValueError(f"unsafe path traversal: {target}")

    real_root = root.resolve()

    # 展开实际软链接链路，逐级验证边界：
    # 任何处于根内的路径节点，若经软链接跳转至根外部，一律判定为越界逃逸；
    # 绝不靠单纯 resolve 折叠多级别名链，忠实保留实际软链接展开轨迹。
    if not p.is_absolute():
        current = real_root
        remaining_parts = list(p.parts)
    else:
        current = Path(p.parts[0])
        remaining_parts = list(p.parts[1:])

    depth = 0
    max_symlink_depth = 40
    visited_symlinks: set[Path] = set()
    ever_in_root = False

    while remaining_parts:
        part = remaining_parts.pop(0)
        if not part or part == ".":
            continue
        if part == "..":
            current = current.parent
            continue

        next_path = current / part

        if next_path.is_symlink():
            depth += 1
            if depth > max_symlink_depth or next_path in visited_symlinks:
                raise ValueError(f"symlink cycle or depth exceeded: {target}")
            visited_symlinks.add(next_path)

            try:
                curr_res = current.resolve()
            except (OSError, RuntimeError) as exc:
                raise ValueError(f"broken path in traversal: {target}") from exc

            is_in_root = (curr_res == real_root or real_root in curr_res.parents)
            if is_in_root:
                ever_in_root = True

            try:
                dest_res = next_path.resolve()
            except (OSError, RuntimeError) as exc:
                raise ValueError(f"broken symlink in path: {target}") from exc

            dest_in_root = (dest_res == real_root or real_root in dest_res.parents)

            # 核心安全底线：任何处于根内的节点，若经软链逃出物理根，严格拦截
            if is_in_root and not dest_in_root:
                raise ValueError(f"symlink out of bounds: {target}")

            # 展开该层软链接，继续跟踪后续链路
            raw_target = os.readlink(next_path)
            link_target = Path(raw_target)
            if link_target.is_absolute():
                current = Path(link_target.parts[0])
                remaining_parts = list(link_target.parts[1:]) + remaining_parts
            else:
                current = next_path.parent
                remaining_parts = list(link_target.parts) + remaining_parts
            continue

        current = next_path
        try:
            curr_res = current.resolve()
        except (OSError, RuntimeError):
            curr_res = current

        if curr_res == real_root or real_root in curr_res.parents:
            ever_in_root = True

    try:
        final_res = current.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"broken symlink in path: {target}") from exc

    final_in_root = (final_res == real_root or real_root in final_res.parents)

    if ever_in_root and not final_in_root:
        raise ValueError(f"symlink out of bounds: {target}")

    if final_in_root:
        return final_res
    else:
        if allow_within_root_only:
            raise ValueError(f"unsafe absolute path outside root: {target}")
        try:
            return p.resolve()
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"broken symlink in path: {target}") from exc


def _guarded_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(part in (".", "..") for part in path.parts):
        raise ValueError(f"unsafe registry manifest path: {relative}")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink in registry manifest path: {relative}")
    return current


def _source_files(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    return sorted(path for path in root.rglob("*.md") if path.is_file() and not path.is_symlink())


def _inspect_top_level_dir(directory: Path, max_depth: int = 3) -> str:
    """检查顶层目录的构成特征。
    
    返回分类标签：
    - "top_level": 包含可执行代码文件（明确为功能组件目录）
    - "top_level_doc": 纯文档目录（在深度 3 内全为文档，无代码无软链）
    - "top_level_deep": 超出契约深度（深度 > 3 且无代码，无法断言纯文档）
    - "top_level_unknown": 包含符号链接、无法读取或未观察到代码/文档的未知目录
    """
    if not directory.is_dir() or directory.is_symlink():
        return "top_level_unknown"

    has_doc = False
    has_code = False
    has_deeper = False
    has_symlink = False

    queue: list[tuple[Path, int]] = [(directory, 0)]
    while queue:
        curr, depth = queue.pop(0)
        try:
            entries = list(curr.iterdir())
        except OSError:
            return "top_level_unknown"

        for entry in entries:
            if entry.name.startswith(".") or entry.name in {".DS_Store"}:
                continue
            if entry.is_symlink():
                has_symlink = True
                continue

            entry_depth = depth + 1
            if entry_depth > max_depth:
                has_deeper = True
                continue

            if entry.is_dir():
                queue.append((entry, entry_depth))
            elif entry.is_file():
                suffix = entry.suffix.lower()
                if suffix in CODE_EXTENSIONS:
                    has_code = True
                elif suffix in DOC_EXTENSIONS:
                    has_doc = True

    if has_code:
        return "top_level"
    if has_symlink:
        return "top_level_unknown"
    if has_deeper:
        return "top_level_deep"
    if has_doc:
        return "top_level_doc"
    return "top_level_unknown"


def _inspect_doc_dir(directory: Path, max_depth: int = 3) -> tuple[bool, bool]:
    tag = _inspect_top_level_dir(directory, max_depth)
    if tag == "top_level_doc":
        return True, False
    if tag == "top_level_deep":
        return False, True
    return False, False


def _is_pure_doc_dir(directory: Path, max_depth: int = 3) -> bool:
    return _inspect_top_level_dir(directory, max_depth) == "top_level_doc"


def _current_release_date(assistant_root: Path) -> str:
    release_root = assistant_root.parent / "小八AgentOS" / ".runtime" / "releases"
    index = _json(release_root / "index.json")
    version = index.get("current_release")
    if not isinstance(version, str) or not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        return ""
    release = _json(release_root / version / "RELEASE.json")
    date = release.get("verified_date")
    return date if isinstance(date, str) and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", date) else ""


def _candidate_paths(
    assistant_root: Path,
    codex_root: Path,
    agent_skills: list[Path],
) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    if assistant_root.is_dir() and not assistant_root.is_symlink():
        # Top-level (depth 1)
        for path in sorted(assistant_root.iterdir()):
            if path.is_dir() and not path.is_symlink() and path.name not in INTERNAL_TOP_LEVEL:
                kind_tag = _inspect_top_level_dir(path, max_depth=3)
                candidates.append((kind_tag, path))

        # Tools (depth 1 under 工具)
        tools_dir = assistant_root / "工具"
        if tools_dir.is_dir() and not tools_dir.is_symlink():
            candidates.extend(
                ("tool", path) for path in sorted(tools_dir.iterdir())
                if path.is_dir() and not path.is_symlink()
            )

        # Shared skills (depth 1 under skills)
        skills_dir = assistant_root / "skills"
        if skills_dir.is_dir() and not skills_dir.is_symlink():
            candidates.extend(
                ("shared_skill", path) for path in sorted(skills_dir.iterdir())
                if path.is_dir() and not path.is_symlink()
            )

    # External agent skills (depth 1 under each agent directory)
    for directory in agent_skills:
        if directory.is_dir() and not directory.is_symlink():
            for path in sorted(directory.iterdir()):
                if (
                    path.is_dir()
                    and not path.is_symlink()
                    and not path.name.startswith(".")
                    and path.name != "xiaoba-agentos"
                ):
                    shared_path = assistant_root / "skills" / path.name
                    # 仅当外部实体与共享目录确为同一文件系统对象（真实别名/同源解析）时才去重；
                    # 同名但独立物理实体必须同时可见，互不遮蔽。
                    if shared_path.exists() and shared_path.resolve() == path.resolve():
                        continue
                    candidates.append(("agent_skill", path))

    # Second brain experience notes (depth 1 files)
    experience = assistant_root / "第二大脑" / "70_复利规则" / "经验"
    if experience.is_dir() and not experience.is_symlink():
        release_date = _current_release_date(assistant_root)
        candidates.extend(
            ("asset_experience", path) for path in sorted(experience.glob("*.md"))
            if path.is_file() and not path.is_symlink() and ASSET_NOTE.search(path.stem)
            and re.search(r'^status: ["\']?active["\']?$', _text(path), re.MULTILINE)
            and (not release_date or (
                (match := re.search(r'^last_verified: ["\']?(20\d{2}-\d{2}-\d{2})', _text(path), re.MULTILINE))
                and match.group(1) > release_date
            ))
        )

    # Codex date output directories: strictly bounded depth (codex_root -> day (1) -> session (2) -> outputs (3))
    if codex_root.is_dir() and not codex_root.is_symlink():
        for day in sorted(codex_root.iterdir()):
            if day.is_dir() and not day.is_symlink() and DATE_DIRECTORY.fullmatch(day.name):
                for session in sorted(day.iterdir()):
                    if session.is_dir() and not session.is_symlink():
                        outputs = session / "outputs"
                        if outputs.is_dir() and not outputs.is_symlink() and any(p.is_file() for p in outputs.iterdir()):
                            candidates.append(("codex_output", outputs))
    return candidates


def _collect_registry_contracts(assistant_root: Path) -> tuple[set[Path], set[str]]:
    registry_file = assistant_root / "系统" / "小八工作体系" / "registry.json"
    registry_data = _json(registry_file)
    if not isinstance(registry_data.get("modules"), list):
        raise ValueError("registry modules are unavailable")
    declared_paths: set[Path] = set()
    declared_ids: set[str] = set()
    for module in registry_data.get("modules", []):
        if not isinstance(module, dict):
            continue
        if isinstance(module.get("id"), str):
            declared_ids.add(module["id"])
        if isinstance(module.get("manifest"), str):
            manifest_path = _guarded_file(assistant_root, module["manifest"])
            manifest_data = _json(manifest_path)
            for key in ("implementation_paths", "required_paths", "data_paths", "doc_paths"):
                for p_str in manifest_data.get(key, []):
                    if isinstance(p_str, str):
                        clean_str = p_str.replace("${ROOT}/", "").replace("${ROOT}", "")
                        clean_path = Path(clean_str)
                        if any(part in (".", "..") for part in clean_path.parts):
                            raise ValueError(f"unsafe relative path in manifest: {p_str}")
                        if clean_path.is_absolute():
                            try:
                                resolved = _validate_safe_path(assistant_root, clean_path, allow_within_root_only=False)
                                declared_paths.add(resolved)
                            except ValueError:
                                pass
                        else:
                            try:
                                resolved = _validate_safe_path(assistant_root, clean_path, allow_within_root_only=True)
                                declared_paths.add(resolved)
                            except ValueError:
                                pass
    return declared_paths, declared_ids


def _collect_skill_index_contracts(skill_index: Path, assistant_root: Path | None = None) -> tuple[set[Path], set[str]]:
    index_data = _json(skill_index)
    sources: set[Path] = set()
    ids: set[str] = set()
    for entry in index_data.get("entries", []):
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("id"), str):
            ids.add(entry["id"])
        if isinstance(entry.get("entity_source_path"), str):
            src_str = entry["entity_source_path"]
            p_src = Path(src_str)
            if any(part in (".", "..") for part in p_src.parts):
                raise ValueError(f"unsafe path traversal in skill index: {src_str}")
            try:
                if p_src.is_absolute():
                    if assistant_root:
                        resolved = _validate_safe_path(assistant_root, p_src, allow_within_root_only=False)
                    else:
                        resolved = p_src.resolve()
                else:
                    if assistant_root:
                        resolved = _validate_safe_path(assistant_root, p_src, allow_within_root_only=True)
                    else:
                        resolved = (skill_index.parent / p_src).resolve()
                sources.add(resolved)
            except ValueError:
                pass
    return sources, ids


def _collect_brain_contracts(assistant_root: Path) -> tuple[set[Path], set[str]]:
    brain_dir = assistant_root / "第二大脑"
    if not brain_dir.is_dir() or brain_dir.is_symlink():
        return set(), set()
    paths: set[Path] = set()
    ids: set[str] = set()
    home_str = str(Path.home())
    for branch in ("10_治理与裁决", "20_能力与技能", "30_方法与知识", "40_工作流", "50_资产与索引", "60_当前上下文", "70_复利规则"):
        branch_dir = brain_dir / branch
        if not branch_dir.is_dir() or branch_dir.is_symlink():
            continue
        for note in _source_files(branch_dir):
            ids.add(note.stem)
            content = _text(note)
            for m in re.finditer(r'^source_path:\s*["\']?([^"\'\n]+)["\']?', content, re.MULTILINE):
                sp = m.group(1).strip()
                if sp:
                    p_sp = Path(sp)
                    if any(part in (".", "..") for part in p_sp.parts):
                        continue
                    try:
                        if p_sp.is_absolute():
                            paths.add(_validate_safe_path(assistant_root, p_sp, allow_within_root_only=False))
                        else:
                            paths.add(_validate_safe_path(assistant_root, p_sp, allow_within_root_only=True))
                    except (ValueError, OSError, RuntimeError):
                        pass
            for m in re.finditer(r'^id:\s*["\']?([^"\'\n]+)["\']?', content, re.MULTILINE):
                ids.add(m.group(1).strip())
            for m in re.finditer(r'\[\[([^\]]+)\]\]', content):
                ids.add(m.group(1).split("/")[-1])
            for m in re.finditer(r'(?:["\'`|\b])(/Users/[^\s"\'`|]+|\$HOME/Documents/私人助理/[^\s"\'`|]+|~/Documents/私人助理/[^\s"\'`|]+)', content):
                raw = m.group(1).replace("$HOME", home_str).replace("~", home_str).rstrip("/")
                p_raw = Path(raw)
                if not any(part in (".", "..") for part in p_raw.parts):
                    try:
                        paths.add(_validate_safe_path(assistant_root, p_raw, allow_within_root_only=False))
                    except (ValueError, OSError, RuntimeError):
                        pass
            for m in re.finditer(r'`([A-Za-z0-9_\u4e00-\u9fa5\-\.\/]+)`', content):
                token = m.group(1).strip()
                if "/" in token and not token.startswith("http"):
                    p_tok = Path(token)
                    if not any(part in (".", "..") for part in p_tok.parts):
                        try:
                            paths.add(_validate_safe_path(assistant_root, p_tok, allow_within_root_only=True))
                        except (ValueError, OSError, RuntimeError):
                            pass
    return paths, ids


def _collect_agent_contracts(assistant_root: Path) -> tuple[set[Path], set[str]]:
    agents_file = assistant_root / "AGENTS.md"
    content = _text(agents_file)
    paths: set[Path] = set()
    ids: set[str] = set()
    home_str = str(Path.home())

    for m in re.finditer(r'`([^`\n]+)`', content):
        token = m.group(1).strip()
        if "/" in token and not token.startswith("http"):
            p_tok = Path(token.rstrip("/"))
            if not any(part in (".", "..") for part in p_tok.parts):
                try:
                    paths.add(_validate_safe_path(assistant_root, p_tok, allow_within_root_only=True))
                except (ValueError, OSError, RuntimeError):
                    pass
        ids.add(token.strip("/").split("/")[-1])

    for m in re.finditer(r'(?:["\'`|\b])(\$HOME/Documents/私人助理/[^\s"\'`\)]+|~/Documents/私人助理/[^\s"\'`\)]+|/Users/[^\s"\'`\)]+)', content):
        raw = m.group(1).replace("$HOME", home_str).replace("~", home_str).rstrip("/")
        p_raw = Path(raw)
        if not any(part in (".", "..") for part in p_raw.parts):
            try:
                paths.add(_validate_safe_path(assistant_root, p_raw, allow_within_root_only=False))
            except (ValueError, OSError, RuntimeError):
                pass

    for m in re.finditer(r'(?:^|[\s|`"\'\(（,/])([\w\u4e00-\u9fa5\.\-]+(?:/[\w\u4e00-\u9fa5\.\-]+)+/?)(?:[\s|`"\'\)）,;:/]|$)', content):
        token = m.group(1).strip().rstrip("/")
        p_rel = Path(token)
        if not any(part in (".", "..") for part in p_rel.parts):
            try:
                paths.add(_validate_safe_path(assistant_root, p_rel, allow_within_root_only=True))
                ids.add(token.split("/")[-1])
            except (ValueError, OSError, RuntimeError):
                pass

    return paths, ids


def _path_matches(cand: Path, declared_paths: set[Path], declared_ids: set[str] = set()) -> bool:
    try:
        cand_res = cand.resolve()
    except (OSError, RuntimeError):
        cand_res = cand
    if cand_res in declared_paths:
        return True
    if any(p.is_relative_to(cand_res) for p in declared_paths if p != cand_res):
        return True
    return False


def _load_exclusions(exclusions_file: Path | None, assistant_root: Path | None = None) -> list[dict]:
    if exclusions_file and exclusions_file.is_file() and not exclusions_file.is_symlink():
        data = _json(exclusions_file)
        if isinstance(data.get("exclusions"), list):
            valid_exclusions = []
            for e in data["exclusions"]:
                if not isinstance(e, dict):
                    continue
                target_path = e.get("target_path")
                if target_path:
                    p_target = Path(target_path)
                    if any(part in (".", "..") for part in p_target.parts):
                        raise ValueError(f"unsafe target_path in exclusions: {target_path}")
                    if assistant_root is not None:
                        _validate_safe_path(assistant_root, target_path, allow_within_root_only=True)
                valid_exclusions.append(e)
            return valid_exclusions
    return []


def inventory(
    assistant_root: Path,
    codex_root: Path,
    agent_skills: list[Path],
    skill_index: Path,
    exclusions_file: Path | None = None,
) -> dict:
    assistant_root = assistant_root.resolve()
    for required in (
        assistant_root / "AGENTS.md",
        assistant_root / "系统" / "小八工作体系" / "registry.json",
        skill_index,
    ):
        if not required.is_file() or required.is_symlink():
            raise ValueError(f"required inventory source unavailable: {required}")

    # Load contracts
    reg_paths, reg_ids = _collect_registry_contracts(assistant_root)
    idx_paths, idx_ids = _collect_skill_index_contracts(skill_index, assistant_root)
    brain_paths, brain_ids = _collect_brain_contracts(assistant_root)
    agent_paths, agent_ids = _collect_agent_contracts(assistant_root)

    # Load exclusions list
    exclusions = _load_exclusions(exclusions_file, assistant_root)

    candidates = _candidate_paths(assistant_root, codex_root, agent_skills)
    records = []

    for kind, path in candidates:
        cand_res = path.resolve()
        relative = path.relative_to(assistant_root).as_posix() if path.is_relative_to(assistant_root) else ""

        # Check in-package exclusions list: exact match on kind, ID and target_path
        matched_exclusion = None
        for excl in exclusions:
            excl_kind = excl.get("kind")
            excl_id = excl.get("id")
            excl_target_rel = excl.get("target_path")
            if excl_kind != kind:
                continue
            if excl_id != path.name:
                continue
            if not excl_target_rel:
                continue
            try:
                excl_target = _validate_safe_path(assistant_root, excl_target_rel, allow_within_root_only=True)
            except ValueError:
                continue
            if cand_res == excl_target:
                matched_exclusion = excl
                break

        agent_match = _path_matches(cand_res, agent_paths, agent_ids)
        brain_match = _path_matches(cand_res, brain_paths, brain_ids)
        skill_match = _path_matches(cand_res, idx_paths, idx_ids)
        registry_match = _path_matches(cand_res, reg_paths, reg_ids)

        evidence = {
            "agent_entry": agent_match,
            "brain_entry": brain_match,
            "skill_index": skill_match,
            "registry": registry_match,
        }

        if matched_exclusion:
            records.append({
                "category": CATEGORY_EXCLUDED,
                "category_display": "已决定不接入",
                "kind": kind,
                "path": str(path),
                "evidence": evidence,
                "required": [],
                "missing": [],
                "scope": matched_exclusion.get("scope_description") or matched_exclusion.get("scope", ""),
                "reason": matched_exclusion.get("reason", "包内已登记的不接入决定"),
                "decision_source": matched_exclusion.get("decision_source", ""),
                "assessment": "decided_excluded",
            })
            continue

        if kind == "top_level_unknown":
            records.append({
                "category": CATEGORY_MANUAL,
                "category_display": "待人工判断",
                "kind": kind,
                "path": str(path),
                "evidence": evidence,
                "required": list(REQUIRED_EVIDENCE["top_level_unknown"]),
                "missing": [],
                "reason": "目录扫描受软链、无法读取或未知条件影响，无法断言纯文档亦未观察到功能组件声明；保留待人工判断。",
                "assessment": "unknown_or_symlink_directory",
            })
            continue

        if kind == "top_level_deep":
            records.append({
                "category": CATEGORY_MANUAL,
                "category_display": "待人工判断",
                "kind": kind,
                "path": str(path),
                "evidence": evidence,
                "required": list(REQUIRED_EVIDENCE["top_level_deep"]),
                "missing": [],
                "reason": "目录包含超过扫描契约限制深度（最大深度 3）的内容，无法断言为纯文档；保留待人工判断。",
                "assessment": "directory_depth_exceeded",
            })
            continue

        if kind == "asset_experience":
            is_taste = bool(TASTE_OR_AESTHETICS.search(path.stem) or TASTE_OR_AESTHETICS.search(_text(path)))
            records.append({
                "category": CATEGORY_MANUAL,
                "category_display": "待人工判断",
                "kind": kind,
                "path": str(path),
                "evidence": {"brain_entry": True},
                "required": [],
                "missing": [],
                "reason": (
                    "小八审美/taste 长期维护探索内容，非已完成待接入成品；维持探索维护状态，无需强制模块登记。"
                    if is_taste else
                    "复利经验笔记资料入口，留待人工核对来源以确认是否有待提炼资产。"
                ),
                "assessment": "review_source_for_assets",
            })
            continue

        if kind == "top_level_doc":
            # Pure document directory: must not be judged as confirmed_unregistered just because it lacks registry
            records.append({
                "category": CATEGORY_MANUAL,
                "category_display": "待人工判断",
                "kind": kind,
                "path": str(path),
                "evidence": evidence,
                "required": list(REQUIRED_EVIDENCE["top_level_doc"]),
                "missing": [],
                "reason": "纯文档目录，按规范不强制进入模块 registry 登记；由 AGENTS.md 规范管理，留待人工判断是否需建资产索引卡。",
                "assessment": "pure_doc_directory",
            })
            continue

        if kind == "codex_output":
            # Date output directory: strictly bounded, not confirmed_unregistered
            records.append({
                "category": CATEGORY_MANUAL,
                "category_display": "待人工判断",
                "kind": kind,
                "path": str(path),
                "evidence": evidence,
                "required": [],
                "missing": [],
                "reason": "日期会话输出目录，不属于长期系统模块；留待人工判断是否有需提取归档的成果或临时文件。",
                "assessment": "uncataloged_output",
            })
            continue

        if kind == "agent_skill":
            req = list(REQUIRED_EVIDENCE["agent_skill"])
            missing = [name for name in req if not evidence[name]]
            records.append({
                "category": CATEGORY_MANUAL,
                "category_display": "待人工判断",
                "kind": kind,
                "path": str(path),
                "evidence": evidence,
                "required": req,
                "missing": missing,
                "reason": "外部 Agent 私有技能目录，留待人工判断是否确需引入小八工作体系统一管理。",
                "assessment": "external_agent_skill",
            })
            continue

        # Standard runnable components: shared_skill, tool, top_level
        req = list(REQUIRED_EVIDENCE.get(kind, ()))
        missing = [name for name in req if not evidence[name]]

        if missing:
            records.append({
                "category": CATEGORY_CONFIRMED,
                "category_display": "确定漏登记",
                "kind": kind,
                "path": str(path),
                "evidence": evidence,
                "required": req,
                "missing": missing,
                "reason": f"功能组件缺少必要契约登记凭据（缺：{','.join(missing)}），且无已决议排除依据，判定为确定漏登记。",
                "assessment": "unregistered" if len(missing) == len(req) else "incomplete_registration",
            })
        else:
            records.append({
                "category": CATEGORY_REGISTERED,
                "category_display": "正常已登记",
                "kind": kind,
                "path": str(path),
                "evidence": evidence,
                "required": req,
                "missing": [],
                "reason": "所有必需契约凭据均已精确匹配，登记完整。",
                "assessment": "registered",
            })

    # Summary counts
    summary = {
        CATEGORY_CONFIRMED: sum(1 for r in records if r["category"] == CATEGORY_CONFIRMED),
        CATEGORY_MANUAL: sum(1 for r in records if r["category"] == CATEGORY_MANUAL),
        CATEGORY_EXCLUDED: sum(1 for r in records if r["category"] == CATEGORY_EXCLUDED),
        CATEGORY_REGISTERED: sum(1 for r in records if r["category"] == CATEGORY_REGISTERED),
    }

    # For backward compatibility in items list:
    # Only return items that require attention or review (i.e. not fully registered) unless requested
    active_items = [r for r in records if r["category"] != CATEGORY_REGISTERED]

    return {
        "read_only": True,
        "assistant_root": str(assistant_root),
        "scanned": len(candidates),
        "summary": summary,
        "items": active_items,
        "all_records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assistant-root", type=Path, default=Path.home() / "Documents" / "私人助理")
    parser.add_argument("--codex-root", type=Path, default=Path.home() / "Documents" / "Codex")
    parser.add_argument("--agent-skills", type=Path, action="append")
    parser.add_argument("--skill-index", type=Path)
    parser.add_argument("--exclusions-file", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    roots = args.agent_skills if args.agent_skills is not None else [
        Path.home() / ".codex" / "skills",
        Path.home() / ".gemini" / "config" / "skills",
        Path.home() / ".claude" / "skills",
    ]
    index = args.skill_index or args.assistant_root / "skills" / "index.json"
    exclusions = args.exclusions_file or Path(__file__).resolve().parents[1] / "config" / "inventory_exclusions.json"

    report = inventory(args.assistant_root, args.codex_root, roots, index, exclusions)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        summary = report["summary"]
        print(f"只读盘点：扫描 {report['scanned']} 项")
        print(f"  - 确定漏登记：{summary[CATEGORY_CONFIRMED]} 项")
        print(f"  - 待人工判断：{summary[CATEGORY_MANUAL]} 项")
        print(f"  - 已决定不接入：{summary[CATEGORY_EXCLUDED]} 项")
        print(f"  - 正常已登记：{summary[CATEGORY_REGISTERED]} 项\n")

        for cat_key, cat_name in [
            (CATEGORY_CONFIRMED, "【确定漏登记】"),
            (CATEGORY_MANUAL, "【待人工判断】"),
            (CATEGORY_EXCLUDED, "【已决定不接入】"),
        ]:
            group = [r for r in report["items"] if r["category"] == cat_key]
            if not group:
                continue
            print(f"{cat_name} ({len(group)} 项):")
            for item in group:
                missing_str = f" [缺：{','.join(item['missing'])}]" if item["missing"] else ""
                scope_str = f" | 范围：{item['scope']}" if item.get("scope") else ""
                print(f"  - [{item['kind']}] {item['path']}{missing_str}")
                print(f"    理由：{item['reason']}{scope_str}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
