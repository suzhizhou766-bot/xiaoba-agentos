from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import AppConfig


_ALLOWED_MODES = {"semantic_source", "machine_metadata", "runtime_binding"}
_SENSITIVE_SUFFIXES = {".env", ".pem", ".key", ".p12", ".sqlite", ".db"}
_SENSITIVE_PARTS = {".runtime", "logs", "cache", "cookies", "credentials"}
_DISALLOWED_CONTEXT_MARKERS = (
    "historical memory:",
    "migration note:",
    "deprecated deployment:",
    "legacy route:",
    "历史记忆：",
    "迁移说明：",
    "弃用部署：",
    "旧路由：",
)
_SECRET_PATTERN = re.compile(r"(?i)(sk-[a-z0-9_-]{16,}|bearer\s+[a-z0-9._~+/=-]{20,})")


def _inside(path: Path, roots: List[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


class KnowledgeCatalog:
    def __init__(self, config: AppConfig):
        self.config = config

    def _load(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.config.knowledge_index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"schema_version": 0, "entries": [], "load_error": str(exc)}
        return value if isinstance(value, dict) else {"schema_version": 0, "entries": []}

    def verify(self) -> Dict[str, Any]:
        index = self._load()
        problems = []
        if index.get("schema_version") != 1:
            problems.append("价值文件索引 schema_version 必须为 1")
        if index.get("load_error"):
            problems.append(f"无法读取价值文件索引：{index['load_error']}")
        entries = index.get("entries") if isinstance(index.get("entries"), list) else []
        ids = set()
        paths = set()
        for entry in entries:
            if not isinstance(entry, dict):
                problems.append("价值文件条目必须是对象")
                continue
            item_id = str(entry.get("id", ""))
            raw_path = str(entry.get("path", ""))
            mode = str(entry.get("index_mode", ""))
            if not item_id or item_id in ids:
                problems.append(f"价值文件 id 为空或重复：{item_id}")
            ids.add(item_id)
            if not raw_path or raw_path in paths:
                problems.append(f"价值文件路径为空或重复：{raw_path}")
            paths.add(raw_path)
            if mode not in _ALLOWED_MODES:
                problems.append(f"价值文件索引模式非法：{item_id}")
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                problems.append(f"价值文件必须使用绝对路径：{item_id}")
                continue
            resolved = path.resolve()
            if not _inside(resolved, self.config.knowledge_allowed_roots):
                problems.append(f"价值文件越过允许根目录：{item_id}")
            if path.is_symlink() or not path.is_file():
                problems.append(f"价值文件缺失或为符号链接：{item_id}")
                continue
            lowered_parts = {part.casefold() for part in resolved.parts}
            if path.suffix.casefold() in _SENSITIVE_SUFFIXES or lowered_parts & _SENSITIVE_PARTS:
                problems.append(f"价值文件命中敏感排除边界：{item_id}")
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != str(entry.get("sha256", "")):
                problems.append(f"价值文件哈希不匹配：{item_id}")
            if mode == "runtime_binding" and not os.access(path, os.X_OK):
                problems.append(f"运行时绑定不可执行：{item_id}")
            if path.suffix.casefold() in {".md", ".txt", ".py", ".sh", ".json"}:
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    problems.append(f"价值文件无法安全读取：{item_id}: {exc}")
                    continue
                lowered = content.casefold()
                found = [marker for marker in _DISALLOWED_CONTEXT_MARKERS if marker in lowered]
                if found:
                    problems.append(f"价值文件含非当前态内容：{item_id}: {', '.join(found)}")
                if _SECRET_PATTERN.search(content):
                    problems.append(f"价值文件疑似含凭据：{item_id}")
        return {
            "ok": not problems,
            "index": str(self.config.knowledge_index),
            "entries": len(entries),
            "semantic_sources": sum(1 for item in entries if item.get("index_mode") == "semantic_source"),
            "machine_metadata": sum(1 for item in entries if item.get("index_mode") == "machine_metadata"),
            "runtime_bindings": sum(1 for item in entries if item.get("index_mode") == "runtime_binding"),
            "problems": problems,
        }

    def index_sha256(self) -> Optional[str]:
        index_path = getattr(self.config, "knowledge_index", None)
        if index_path and Path(index_path).is_file():
            try:
                return hashlib.sha256(Path(index_path).read_bytes()).hexdigest()
            except OSError:
                return None
        return None

    def retrieve_with_receipt(self, query: str) -> Dict[str, Any]:
        verified_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        index_sha = self.index_sha256()

        if hasattr(self.config, "knowledge_enabled") and not self.config.knowledge_enabled:
            return {
                "retrieval_status": "disabled",
                "query": query,
                "hits": [],
                "index_sha256": index_sha,
                "verified_at": verified_at,
            }

        report = self.verify()
        if not report.get("ok"):
            return {
                "retrieval_status": "degraded",
                "query": query,
                "hits": [],
                "problems": report.get("problems", []),
                "index_sha256": index_sha,
                "verified_at": verified_at,
            }

        data = self._load()
        entries = data.get("entries") if isinstance(data.get("entries"), list) else []

        tokens = [t.strip() for t in query.split() if t.strip()]
        hits = []
        if tokens:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                entry_status = entry.get("status")
                if entry_status not in (None, "", "active"):
                    continue
                raw_authority = entry.get("authority", True)
                if isinstance(raw_authority, bool):
                    authority = raw_authority
                else:
                    authority = str(raw_authority).strip().lower() in {"1", "true", "yes"}
                searchable = f"{entry.get('id', '')} {entry.get('title', '')} {entry.get('summary', '')} {entry.get('category', '')}"
                if all(token in searchable for token in tokens):
                    hits.append(
                        {
                            "id": str(entry.get("id", "")),
                            "title": str(entry.get("title", "")),
                            "category": str(entry.get("category", "")),
                            "index_mode": str(entry.get("index_mode", "")),
                            "path": str(entry.get("path", "")),
                            "source_sha256": str(entry.get("sha256", "")),
                            "is_active": entry_status in (None, "", "active"),
                            "authority": authority,
                            "summary": str(entry.get("summary", "")),
                        }
                    )

        status = "canonical_match" if hits else "miss"
        return {
            "retrieval_status": status,
            "query": query,
            "hits": hits,
            "index_sha256": index_sha,
            "verified_at": verified_at,
        }

    def list(self) -> List[Dict[str, Any]]:
        result = self.verify()
        if not result["ok"]:
            raise RuntimeError("；".join(result["problems"]))
        return [
            {
                "id": entry["id"],
                "title": entry["title"],
                "category": entry["category"],
                "index_mode": entry["index_mode"],
                "path": entry["path"],
                "summary": entry["summary"],
            }
            for entry in self._load()["entries"]
        ]

    def retrieve(self, query: str) -> List[Dict[str, Any]]:
        return self.retrieve_with_receipt(query)["hits"]
