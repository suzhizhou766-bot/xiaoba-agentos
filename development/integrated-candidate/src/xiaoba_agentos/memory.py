from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import fcntl
from contextlib import contextmanager, closing
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import AppConfig
from . import retained as retained_objects
from .store import MEMORY_ACTIVE_RETRIEVAL_STATES, b19_write_boundary_reason


class MemoryRecoveryError(RuntimeError):
    """A failed publication could not be rolled back completely.

    Raised instead of the original error when the Vault note, the catalog or an
    index output file is left half-written.  It exists so callers report manual
    action and never claim a clean rollback that did not happen.
    """


def _reserve_private_name(path: Path, suffix: str) -> Path:
    """Atomically acquire a free private sibling name for ``path``.

    Every private name the rollback stages into (``.{name}.{pid}.rollback`` and
    ``.{name}.{pid}.restore``) is *acquired*, not assumed free: ``os.open`` with
    ``O_CREAT|O_EXCL`` creates it in one atomic step, so there is no
    check-with-exists-then-overwrite window, and the call fails instead of
    proceeding when the name is taken.  The default names are deterministic on
    purpose (not random strings): a name that is already occupied is reported,
    never silently stepped over and never reused, because it can hold a copy an
    earlier call deliberately kept at a known location for manual inspection --
    that content is left byte-for-byte intact.

    On success the name holds an empty placeholder file this call owns; the
    caller either moves the real object onto it with ``os.replace`` or releases
    it with :func:`_release_reserved_name`.
    """
    candidate = path.parent / f".{path.name}.{os.getpid()}.{suffix}"
    try:
        handle = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise OSError(
            f"cannot reserve a private staging name for {path}: {candidate} is "
            "already occupied; its content is left untouched and nothing was "
            "changed; resolve it manually"
        ) from exc
    os.close(handle)
    return candidate


def _release_reserved_name(reserved: Path) -> None:
    """Remove the empty placeholder :func:`_reserve_private_name` just created.

    Only a name this process reserved moments ago is removed, so a failed claim
    never leaves a private name occupied by our own placeholder.  A private name
    found already occupied before the reservation is never touched here.
    """
    try:
        reserved.unlink()
    except OSError:
        pass


class MemoryBridge:
    def __init__(self, config: AppConfig, store: Any = None):
        """Bind the Vault bridge to the task database for retrieval gating.

        `store` only needs `get_memory_candidate(task_id)`.  When provided,
        retrieval is fail-closed against the candidate lifecycle: a Vault
        note claiming an AgentOS origin (frontmatter `source_task_id`) is
        served only if its database row is in MEMORY_ACTIVE_RETRIEVAL_STATES
        ("published").  Unpublished (provisional), quarantined, retired or
        untracked candidates never reach agent contexts, even if the file
        still says `status: "active"`.  Notes without `source_task_id` are
        Vault-native pages outside the memory_candidates lifecycle and keep
        the frontmatter-only gate.

        Retrieval call sites should always pass the live store; omitting it
        is kept only for backwards compatibility and file-only flows.
        """
        self.config = config
        self.store = store
        self.outbox = config.runtime_dir / "memory-outbox"
        # Replacement record for the index outputs one publication installed:
        # relative path -> {"installed": sha256 of the bytes this call wrote,
        # "original": (bytes, mode) or None}.  Held in memory for this call's own
        # rollback only; it is what lets a rollback prove a file is still ours.
        self._index_installs: Dict[str, Dict[str, Any]] = {}
        # Context for objects this call's rollback retains (v1.5.5 restricted plan
        # C): the task whose write is being undone, so the metadata-only discovery
        # record can name it.  Empty means "unknown" rather than a guess.
        self._retention_context: Dict[str, Any] = {}

    def _secure_runtime_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)

    @contextmanager
    def _publication_lock(self):
        self._secure_runtime_dir(self.config.runtime_dir)
        lock_path = self.config.runtime_dir / "memory-publication.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def probe(self) -> Dict[str, Any]:
        if not self.config.memory.get("knowlp_enabled"):
            return {"ok": True, "enabled": False, "hit_count": 0}
        argv = self._query_command("AgentOS health probe", 1)
        try:
            result = subprocess.run(argv, text=True, capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "enabled": True, "error": str(exc)}
        if result.returncode != 0:
            return {"ok": False, "enabled": True, "error": (result.stderr or result.stdout)[-1000:]}
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return {"ok": False, "enabled": True, "error": f"KnowLP 返回无效 JSON：{exc}"}
        hits = payload.get("hits", [])
        if not isinstance(hits, list):
            return {"ok": False, "enabled": True, "error": "KnowLP hits 不是列表"}
        return {
            "ok": True,
            "enabled": True,
            "hit_count": len(hits),
            "warning": result.stderr.strip()[-1000:] or None,
        }

    def audit_lifecycle(self, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Audit DB-facing lifecycle rows against Vault and archive state.

        This method is read-only so health checks can fail closed after a
        process interruption without trying to repair state implicitly.
        """
        vault = Path(self.config.memory["obsidian_vault"]).expanduser().resolve()
        catalog = vault / self.config.memory.get("catalog_note", "70_复利规则/经验索引.md")
        problems = []
        try:
            catalog_text = catalog.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return {"ok": False, "checked": 0, "problems": [f"catalog_unavailable:{exc}"]}

        checked = 0
        for candidate in candidates:
            status = str(candidate.get("status") or "")
            if status not in {"published", "revalidation_required", "retired"}:
                continue
            checked += 1
            task_id = str(candidate.get("task_id") or "")
            found = self.find_note(vault, task_id)
            active_target = (found or vault / self.config.memory["obsidian_subdir"] / f"{task_id}.md").resolve()
            linked = active_target
            if status != "retired" and candidate.get("published_path"):
                try:
                    recorded = Path(str(candidate["published_path"])).expanduser().resolve()
                    recorded.relative_to(vault)
                    linked = recorded
                except (OSError, ValueError):
                    pass
            link_line = f"- [[{linked.relative_to(vault).with_suffix('')}]]"
            link_count = sum(
                1 for line in catalog_text.splitlines() if line.strip() == link_line
            )
            if status == "retired":
                archive = Path(str(candidate.get("published_path") or "")).expanduser()
                if not archive.is_file() or archive.is_symlink():
                    problems.append(f"{task_id}:retired_archive_missing")
                if active_target.exists():
                    problems.append(f"{task_id}:retired_target_still_active")
                if link_count:
                    problems.append(f"{task_id}:retired_catalog_link_present")
                continue

            raw_path = str(candidate.get("published_path") or "")
            path_input = Path(raw_path).expanduser() if raw_path else active_target
            try:
                target = path_input.resolve()
                target.relative_to(vault)
            except (OSError, ValueError):
                problems.append(f"{task_id}:{status}_path_outside_vault")
                continue
            if path_input.is_symlink() or not target.is_file():
                problems.append(f"{task_id}:{status}_target_missing")
                continue
            try:
                text = target.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                problems.append(f"{task_id}:{status}_target_unreadable")
                continue
            match = re.search(r'^status:\s*["\']?([^"\'\n]+)', text, re.MULTILINE)
            file_status = match.group(1).strip() if match else ""
            if status == "published":
                if file_status != "active":
                    problems.append(f"{task_id}:published_file_not_active")
                if link_count != 1:
                    problems.append(f"{task_id}:published_catalog_link_count={link_count}")
            else:
                if file_status != "revalidation_required":
                    problems.append(f"{task_id}:revalidation_file_still_active")
                if link_count:
                    problems.append(f"{task_id}:revalidation_catalog_link_present")
        return {"ok": not problems, "checked": checked, "problems": problems}

    def _query_command(self, query: str, limit: int) -> List[str]:
        memory = self.config.memory
        bridge = self.config.project_dir / "scripts" / "knowlp_query.py"
        module_path = Path(memory["knowlp_module"]).expanduser().resolve()
        default_graph_dir = module_path.parent.parent if len(module_path.parents) > 1 else module_path.parent
        return [
            memory["knowlp_python"],
            str(bridge),
            "--module",
            memory["knowlp_module"],
            "--vault",
            memory.get("knowlp_vault", memory["obsidian_vault"]),
            "--graph-dir",
            memory.get("knowlp_graph_dir", str(default_graph_dir)),
            "--query",
            query[:500],
            "--limit",
            str(limit),
        ]

    def index_sha256(self) -> Optional[str]:
        memory = self.config.memory
        graph_dir_val = memory.get("knowlp_graph_dir")
        if not graph_dir_val:
            knowlp_mod = memory.get("knowlp_module")
            if knowlp_mod:
                mod_path = Path(knowlp_mod).expanduser().resolve()
                graph_dir_val = mod_path.parent.parent if len(mod_path.parents) > 1 else mod_path.parent
        if not graph_dir_val:
            return None
        graph_dir = Path(graph_dir_val).expanduser().resolve()
        if not graph_dir.is_dir():
            return None
        try:
            files = [
                p for p in sorted(graph_dir.iterdir())
                if not p.name.startswith(".")
                and not p.is_symlink()
                and p.is_file()
                # 保留物不参与索引指纹：私有保留名与实际发现结果两道门。
                and not retained_objects.is_retained_path(self.config, p)
            ]
        except OSError:
            return None
        if not files:
            return None
        digest = hashlib.sha256()
        for file_path in files:
            digest.update(file_path.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(file_path.read_bytes())
        return digest.hexdigest()

    def _sanitize_query(self, query: str) -> str:
        if self._sensitive_reason(query):
            return "[REDACTED_SENSITIVE_QUERY]"
        return query[:500]

    def _verified_source(self, hit: Dict[str, Any], limit: int = 1200) -> Optional[Dict[str, Any]]:
        """Return an active KnowLP source read from the configured Vault.

        KnowLP is only a locator.  Its snippet/content fields are deliberately
        ignored: a missing, unreadable, non-Markdown, non-active, or escaping
        source is not memory context.
        """
        raw_path = str(hit.get("path") or "").strip()
        if not raw_path:
            return None

        vault = Path(
            self.config.memory.get("knowlp_vault", self.config.memory["obsidian_vault"])
        ).expanduser().resolve()
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = vault / candidate
        try:
            source = candidate.resolve()
            source.relative_to(vault)
        except (OSError, ValueError):
            return None
        if candidate.is_symlink() or source.suffix.lower() != ".md" or not source.is_file():
            return None
        # 保留物隔离：被认领后留在私有名字下的对象哪怕正文是一篇完整笔记，也不是
        # 可检索的 Vault 来源（私有保留名 + 保留物发现结果两道门）。
        if retained_objects.is_retained_path(self.config, source):
            return None
        try:
            if source.stat().st_size > 1_000_000:
                return None
            raw_bytes = source.read_bytes()
            text = raw_bytes.decode("utf-8-sig")
        except (OSError, UnicodeError):
            return None
        frontmatter_match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.DOTALL)
        if not frontmatter_match:
            return None
        frontmatter: Dict[str, str] = {}
        for line in frontmatter_match.group(1).splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip().strip('"').strip("'")
        if frontmatter.get("status") != "active":
            return None
        try:
            relative_path = str(source.relative_to(vault))
        except ValueError:
            return None
        source_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        raw_task_id = frontmatter.get("source_task_id") or frontmatter.get("task_id")
        source_task_id = str(raw_task_id).strip() if raw_task_id else None
        if source_task_id and not re.fullmatch(r"[0-9a-f-]{36}", source_task_id, re.IGNORECASE):
            source_task_id = None
        authority = str(frontmatter.get("authority", "")).lower() == "true"
        return {
            "path": relative_path,
            "excerpt": text.strip()[:limit],
            "source_task_id": source_task_id,
            "source_sha256": source_sha256,
            "is_active": True,
            "authority": authority,
        }

    def _database_active(self, source: Dict[str, Any]) -> bool:
        """Gate a verified Vault source on the candidate lifecycle database.

        The file frontmatter alone is not proof of review: only the task
        database records that a candidate passed delivery review, final
        review and publication.  A hit bound to a `source_task_id` is
        therefore served only when its database row is currently `published`
        (see MEMORY_ACTIVE_RETRIEVAL_STATES).  Missing rows, quarantined,
        retired, failed-publication or provisional candidates are dropped,
        as are lookups that cannot be completed (fail closed).
        """
        source_task_id = source.get("source_task_id")
        if not source_task_id:
            # Vault-native note outside the memory_candidates lifecycle.
            return True
        if self.store is None:
            # A task-bound memory claim cannot be verified without the
            # lifecycle database.  Keep the retrieval path fail-closed;
            # callers that only need Vault-native notes remain supported.
            return False
        try:
            row = self.store.get_memory_candidate(str(source_task_id))
            status = str(row.get("status")) if row else ""
        except (OSError, ValueError, KeyError, sqlite3.Error):
            return False
        return status in MEMORY_ACTIVE_RETRIEVAL_STATES

    def retrieve_with_receipt(self, query: str) -> Dict[str, Any]:
        sanitized_query = self._sanitize_query(query)
        index_sha = self.index_sha256()
        verified_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")

        memory = self.config.memory
        if not memory.get("knowlp_enabled"):
            return {
                "retrieval_status": "disabled",
                "query": sanitized_query,
                "hits": [],
                "index_sha256": index_sha,
                "verified_at": verified_at,
            }

        argv = self._query_command(sanitized_query, int(memory.get("knowlp_limit", 6)))
        try:
            result = subprocess.run(argv, text=True, capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "retrieval_status": "degraded",
                "query": sanitized_query,
                "hits": [],
                "problems": [str(exc)],
                "index_sha256": index_sha,
                "verified_at": verified_at,
            }
        if result.returncode != 0:
            return {
                "retrieval_status": "degraded",
                "query": sanitized_query,
                "hits": [],
                "problems": [(result.stderr or result.stdout)[-1000:]],
                "index_sha256": index_sha,
                "verified_at": verified_at,
            }
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return {
                "retrieval_status": "degraded",
                "query": sanitized_query,
                "hits": [],
                "problems": [f"KnowLP 返回无效 JSON：{exc}"],
                "index_sha256": index_sha,
                "verified_at": verified_at,
            }
        raw_hits = payload.get("hits", [])
        if not isinstance(raw_hits, list):
            return {
                "retrieval_status": "degraded",
                "query": sanitized_query,
                "hits": [],
                "problems": ["KnowLP hits 不是列表"],
                "index_sha256": index_sha,
                "verified_at": verified_at,
            }

        verified = []
        for hit in raw_hits:
            if not isinstance(hit, dict):
                continue
            source = self._verified_source(hit)
            if not source:
                continue
            if not self._database_active(source):
                continue
            title = hit.get("title") or Path(source["path"]).name
            verified.append(
                {
                    "title": str(title),
                    "path": source["path"],
                    "excerpt": source["excerpt"],
                    "source_task_id": source["source_task_id"],
                    "source_sha256": source["source_sha256"],
                    "is_active": source["is_active"],
                    "authority": source["authority"],
                }
            )

        status = "canonical_match" if verified else "miss"
        return {
            "retrieval_status": status,
            "query": sanitized_query,
            "hits": verified,
            "index_sha256": index_sha,
            "verified_at": verified_at,
        }

    def retrieve_hits(self, query: str) -> List[Dict[str, Any]]:
        receipt = self.retrieve_with_receipt(query)
        return receipt["hits"]

    @staticmethod
    def render_hits(hits: List[Dict[str, Any]]) -> str:
        return "\n".join(f"- {hit['title']}:\n{hit['excerpt']}" for hit in hits)

    def retrieve(self, query: str) -> str:
        return self.render_hits(self.retrieve_hits(query))

    def agent_contexts(self, query: str, agent_names: List[str]) -> Dict[str, Any]:
        """Return reproducible but independent memory assignments.

        The first Agent receives the strongest hit. The second receives all
        verified hits and an explicit contrast instruction. Additional Agents
        receive a deterministic rotation. Agents still reason independently;
        no generated answer is shared before adjudication.
        """
        if self.retrieve_hits != MemoryBridge.retrieve_hits:
            hits = self.retrieve_hits(query)
            receipt = {
                "retrieval_status": "canonical_match" if hits else "miss",
                "query": self._sanitize_query(query),
                "hits": hits,
                "index_sha256": self.index_sha256(),
                "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
        else:
            receipt = self.retrieve_with_receipt(query)
            hits = receipt["hits"]
        contexts: Dict[str, str] = {}
        if not hits:
            return {"hits": [], "contexts": contexts, "receipt": receipt}
        for index, name in enumerate(agent_names):
            if index == 0:
                selected = hits[:1]
                prefix = "优先复核最相关的历史案例，验证其适用边界："
            elif index == 1:
                selected = hits
                prefix = "对比全部历史案例，明确成功方式、错误方式、冲突与当前适用边界："
            else:
                offset = index % len(hits)
                selected = hits[offset:] + hits[:offset]
                prefix = "独立复核以下历史案例，不采信其他 Agent 的结论："
            contexts[name] = prefix + "\n" + self.render_hits(selected)
        return {"hits": hits, "contexts": contexts, "receipt": receipt}

    def staged_path(self, task_id: str) -> Path:
        """The one file that holds this task's candidate evidence.

        Single source of the deterministic outbox name: staging writes it and
        publication reads it back by the same rule, so a caller that has to
        snapshot the bytes before an overwrite asks the bridge instead of
        restating the pattern.
        """
        return self.outbox / f"{task_id}.md"

    def stage_experience(self, task_id: str, prompt: str, result: str, runs: List[Dict[str, Any]]) -> Path:
        self._secure_runtime_dir(self.config.runtime_dir)
        self._secure_runtime_dir(self.outbox)
        target = self.staged_path(task_id)
        lines = [
            "---",
            f"task_id: {task_id}",
            f"created_at: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
            "status: pending_review",
            'type: "experience-candidate"',
            'authority: false',
            "---",
            "",
            "# AgentOS 经验候选",
            "",
            "## 任务",
            "",
            prompt,
            "",
            "## 结果",
            "",
            result,
            "",
            "## 运行证据",
            "",
        ]
        for run in runs:
            lines.append(f"- {run['agent_name']}: {run['status']}, {run.get('latency_ms') or 0} ms")
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(target, 0o600)
        return target

    @staticmethod
    def _sensitive_reason(text: str) -> Optional[str]:
        patterns = {
            "private_key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            "authorization": r"(?i)authorization\s*[:=]\s*[^\n]{8,}",
            "oauth_token": r"(?i)(?:access[_-]?token|refresh[_-]?token|id[_-]?token|oauth[_-]?token)\s*[:=]\s*[\"']?[^\s\"']{8,}",
            "secret_field": r"(?i)(?:api[_-]?key|secret[_-]?key|client[_-]?secret|password|passwd)\s*[:=]\s*[\"']?[^\s\"']{8,}",
            "environment_secret": r"(?im)^[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)\s*=\s*[^\s]{8,}$",
            "known_token_prefix": r"(?i)\b(?:sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{12,}|xox[baprs]-[a-z0-9-]{12,})\b",
            "bearer_jwt": r"(?i)\bbearer[ \t]+[a-z0-9_-]{8,}={0,2}\.[a-z0-9_-]{8,}={0,2}\.[a-z0-9_-]{8,}={0,2}(?![a-z0-9_.=-])",
            "google_oauth_token": r"(?i)(?<![a-z0-9_-])ya29\.[a-z0-9_-]{20,}(?![a-z0-9_-])",
            "aws_access_key_id": r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])",
            "cookie": r"(?i)(?:cookie|set-cookie)\s*:\s*[^\n]{12,}",
            "identity_number": r"(?i)(?:身份证|identity[_ -]?number)\s*[:：=]\s*\d{15,18}[0-9xX]?",
            "bank_card": r"(?:银行卡|bank[_ -]?card)\s*[:：=]\s*\d{16,19}",
        }
        for name, pattern in patterns.items():
            if re.search(pattern, text):
                return name
        return None

    _GENERIC_TITLES = {"AgentOS 经验候选", "已审核经验"}
    # 文件名标题部分的上限按 Unicode 字符计，不是字节。
    _FILENAME_TITLE_LIMIT = 40
    _FENCE_LINE = re.compile(r"[ ]{0,3}(?P<marker>`{3,}|~{3,})(?P<info>.*)\Z")
    # 末尾一整对同种括号、括号内不再有任何括号、且含「待审核」才是阶段标注。
    _STAGE_MARK = re.compile(
        r"(?:（[^（()）]*待审核[^（()）]*）|\([^（()）]*待审核[^（()）]*\))\s*\Z"
    )
    # 一个词 = 字母/数字片段，中间的连字符、撇号、下划线把片段绑成同一个词。
    _WORD = re.compile(r"[0-9A-Za-z]+(?:[-'’_][0-9A-Za-z]+)*")

    @classmethod
    def _prose_lines(cls, lines: List[str]) -> List[str]:
        """丢掉 Markdown 围栏内部的行；反引号和波浪线都认，长度匹配才算闭合。

        围栏未闭合时，它之后的余下内容一律当代码，不作标题来源。
        """
        prose: List[str] = []
        opened: Optional[tuple] = None
        for line in lines:
            match = cls._FENCE_LINE.match(line)
            if opened is not None:
                char, length = opened
                if (
                    match
                    and match.group("marker")[0] == char
                    and len(match.group("marker")) >= length
                    and not match.group("info").strip()
                ):
                    opened = None
                continue
            if match:
                marker = match.group("marker")
                if marker[0] == "`" and "`" in match.group("info"):
                    # 只有反引号围栏禁止信息串含反引号；波浪线围栏的信息串允许 ` 和 ~，仍是围栏起点。
                    prose.append(line)
                    continue
                opened = (marker[0], len(marker))
                continue
            prose.append(line)
        return prose

    @classmethod
    def note_title(cls, text: str, task_id: str) -> str:
        """给人看的标题：围栏外第一个一级标题；自动生成的候选没有自己的标题，取“任务”一节围栏外的第一行。"""
        body_match = re.search(r"\A---[ \t]*\n.*?\n---[ \t]*\n", text, re.DOTALL)
        body = text[body_match.end():] if body_match else text
        prose = cls._prose_lines(body.splitlines())
        title = ""
        for line in prose:
            heading = re.match(r"#\s+(.+)", line)
            if not heading:
                continue
            candidate = cls._without_stage_mark(heading.group(1).replace(task_id, "").strip())
            if candidate and candidate not in cls._GENERIC_TITLES:
                title = candidate
                break
        if not title:
            # Host-authored candidates may have no heading. Preserve the title
            # submitted with their reviewed bytes instead of consulting a task
            # prompt that is outside the candidate hash.
            if body_match:
                metadata_title = re.search(r"^title:[ \t]*(.+)$", body_match.group(0), re.MULTILINE)
                if metadata_title:
                    try:
                        value = json.loads(metadata_title.group(1))
                    except json.JSONDecodeError:
                        value = None
                    if isinstance(value, str):
                        title = cls._without_stage_mark(re.sub(r"\s+", " ", value).strip())
        if not title:
            # “## 任务”一节里第一个非空、且不以 # 开头的行才是任务；直到下一节标题都没有就当没取到
            in_task = False
            for line in prose:
                if re.match(r"##\s*任务\s*$", line):
                    in_task = True
                    continue
                if in_task and re.match(r"#", line):
                    break
                if in_task and line.strip():
                    title = cls._without_stage_mark(line.strip())
                    break
        return title or "经验"

    @classmethod
    def _without_stage_mark(cls, title: str) -> str:
        """标题末尾“（宿主整理，待审核）”这类候选阶段的标注，发布后不再适用。

        只剥末尾完整且同种配对的一整对括号；错配、缺括号、在中间、被别的括号包住的都原样留着。
        连着几对阶段标注就剥几对，剥到末尾不是阶段标注为止——这样同一个标题再读一次结果不变。
        """
        while True:
            stripped = cls._STAGE_MARK.sub("", title).strip()
            if stripped == title:
                return title
            title = stripped

    @classmethod
    def _cut_point(cls, title: str, limit: int) -> int:
        """截断点落在词内部时退回该词起点，否则用 limit。"""
        for match in cls._WORD.finditer(title):
            start, end = match.span()
            if start < limit < end:
                return start
            if start >= limit:
                break
        return limit

    @classmethod
    def note_filename(cls, text: str, task_id: str) -> str:
        """Vault 里的文件名：可读标题 + 任务编号前 8 位（保证不重名，也能对回任务）。

        标题部分最多 40 个 Unicode 字符；40 字位置落在英文/数字词内部时退回词前，不留半个词。
        开头那个词本身就超上限、退回后什么都不剩时，才用旧的「经验」兜底，正文一级标题仍是完整原标题。
        """
        title = re.sub(r"[\\/:*?\"<>|#^\[\]\r\n\t]+", " ", cls.note_title(text, task_id))
        title = re.sub(r"\s+", " ", title).strip(" .")
        if len(title) > cls._FILENAME_TITLE_LIMIT:
            title = title[:cls._cut_point(title, cls._FILENAME_TITLE_LIMIT)]
        title = title.strip(" .") or "经验"
        return f"{title}-{task_id[:8]}.md"

    def find_note(self, vault: Path, task_id: str) -> Optional[Path]:
        """这个任务在经验目录里的那篇笔记；旧的整串编号命名和新的可读命名都认。"""
        directory = vault / self.config.memory["obsidian_subdir"]
        legacy = directory / f"{task_id}.md"
        if legacy.is_file() and not self._is_retained(legacy):
            return legacy
        marker = f'source_task_id: "{task_id}"'
        for path in sorted(directory.glob(f"*-{task_id[:8]}.md")):
            try:
                if path.is_file() and not path.is_symlink() and marker in path.read_text(encoding="utf-8-sig"):
                    # 保留物（受限丙留在私有名字下的被认领对象）不算这个任务的笔记：
                    # 名字规则和保留物清单两道门都要过，不能只靠后缀或前缀推测。
                    if self._is_retained(path):
                        continue
                    return path
            except (OSError, UnicodeError):
                continue
        return None

    def _is_retained(self, path: Path) -> bool:
        """保留物隔离门：私有保留名，或出现在保留物发现记录/扫描结果里。"""
        return retained_objects.is_retained_path(getattr(self, "config", None), path)

    def _render_published(self, task_id: str, staged_text: str) -> str:
        body_match = re.search(r"\n---\s*\n", staged_text)
        body = staged_text[body_match.end():].strip() if body_match else staged_text.strip()
        return (
            "---\n"
            f'id: "experience-{task_id}"\n'
            'type: "experience"\n'
            'parent: "experiences"\n'
            'status: "active"\n'
            'authority: false\n'
            f'source_task_id: "{task_id}"\n'
            f'last_verified: "{time.strftime("%Y-%m-%d")}"\n'
            "---\n\n"
            f"{self._titled_body(task_id, body, self.note_title(staged_text, task_id))}\n"
        )

    @classmethod
    def _titled_body(cls, task_id: str, body: str, title: Optional[str] = None) -> str:
        """正文以可读标题开头；正文自己带着同一个一级标题时不重复加，标题上的候选阶段标注就地去掉。"""
        title = title or cls.note_title(body, task_id)
        first = next((line for line in body.splitlines() if line.strip()), "")
        if re.fullmatch(r"#\s+" + re.escape(title) + r"\s*", first):
            return body
        own = re.match(r"#\s+(.+)", first)
        if own and cls._without_stage_mark(own.group(1).replace(task_id, "").strip()) == title:
            return body.replace(first, f"# {title}", 1)
        # 自动生成的候选：第一个非空行就是通用标题（“# AgentOS 经验候选”等），就地把这一行换成真标题，不再往前面加
        if own and cls._without_stage_mark(own.group(1).replace(task_id, "").strip()) in cls._GENERIC_TITLES:
            return body.replace(first, f"# {title}", 1)
        return f"# {title}\n\n{body}"

    def _rebuild_and_verify_index(self, vault: Path) -> Dict[str, Any]:
        graph_dir = Path(self.config.memory["knowlp_graph_dir"]).expanduser().resolve()
        builder = self.config.project_dir / "scripts" / "build_knowlp_index.py"
        # The builder is told where the retained-object journal is, so the index
        # excludes retained objects by the journal itself and not only by guessing
        # from a file name (v1.5.5 plan C isolation).
        ledger = retained_objects.journal_path(self.config)
        ledger_args = ["--retained-ledger", str(ledger)] if ledger is not None else []
        with tempfile.TemporaryDirectory(prefix="xiaoba-knowlp-", dir=str(self.config.runtime_dir)) as temporary:
            staged_graph = Path(temporary) / "graph"
            built = subprocess.run(
                [
                    sys.executable, str(builder), "--vault", str(vault),
                    "--graph-dir", str(staged_graph), *ledger_args,
                ],
                text=True, capture_output=True, timeout=60, check=False,
            )
            if built.returncode != 0:
                raise RuntimeError((built.stderr or built.stdout)[-2000:])
            verified = subprocess.run(
                [
                    sys.executable, str(builder), "--vault", str(vault),
                    "--graph-dir", str(staged_graph), "--verify", *ledger_args,
                ],
                text=True, capture_output=True, timeout=60, check=False,
            )
            if verified.returncode != 0:
                raise RuntimeError((verified.stderr or verified.stdout)[-2000:])
            self._secure_runtime_dir(graph_dir)
            # Previous index bytes are held in memory for transaction rollback;
            # an ordinary publication never authorizes a disk backup.  The
            # replacement record is what proves a file is still ours to undo.
            self._index_installs = {}
            snapshot = self._index_output_snapshot(graph_dir)
            try:
                for source in sorted(staged_graph.iterdir()):
                    if not source.is_file():
                        continue
                    self._install_index_output(graph_dir, source)
                final = subprocess.run(
                    [
                        sys.executable, str(builder), "--vault", str(vault),
                        "--graph-dir", str(graph_dir), "--verify", *ledger_args,
                    ],
                    text=True, capture_output=True, timeout=60, check=False,
                )
                if final.returncode != 0:
                    raise RuntimeError((final.stderr or final.stdout)[-2000:])
                return json.loads(final.stdout)
            except Exception as exc:
                problems = self._rollback_index_outputs(graph_dir, snapshot)
                if problems:
                    raise MemoryRecoveryError(
                        "index installation failed and could not be fully rolled back; "
                        "manual action is required for: " + "; ".join(problems)
                    ) from exc
                raise

    def _claim_for_undo(self, path: Path) -> Path:
        """Move the object at ``path`` aside to a private name, atomically.

        ``os.replace`` is the one primitive this rollback leans on, because it is
        atomic with respect to a competing writer and captures *exactly* the
        object that was at the path at a single instant.  From the moment it
        returns, this call only ever inspects or restores that captured object,
        so a write that lands on the live path afterwards is never the object we
        delete or overwrite.  This is what makes the attribution check and the
        undo one step instead of a check followed by a destructive write.

        The private name is *reserved* first (see :func:`_reserve_private_name`),
        because the same name can already hold a copy an earlier call kept at a
        known location after a put-back failed, and ``os.replace`` overwrites
        whatever is at its target: an occupied name is therefore never opened,
        truncated, replaced or removed, and this claim fails with an error
        naming the occupied location, leaving the live path untouched.  A
        placeholder this call reserved but could not use is released again.
        """
        claim = _reserve_private_name(path, "rollback")
        try:
            os.replace(path, claim)
        except OSError:
            _release_reserved_name(claim)
            raise
        return claim

    @staticmethod
    def _place_original(path: Path, content: bytes, mode: int) -> None:
        """Put ``content`` back at a path this call has just atomically emptied.

        ``os.link`` is the portable primitive that creates a name *only if it is
        free* (it raises ``FileExistsError`` otherwise), so a writer that takes
        the path after the claim can never be overwritten by the restore; the
        caller turns that error into a reported conflict.  ``os.replace`` would
        silently clobber such a writer, so it is not used here.

        The staging name is *reserved* before anything is written into it (see
        :func:`_reserve_private_name`): the old blind ``write_bytes`` would
        truncate whatever an earlier call had left at the same name.  A taken
        staging name fails through the generic ``OSError`` path (not
        ``FileExistsError``, which this caller reserves for "the live path was
        taken"), so the captured copy is put back and the occupied location is
        named instead of being overwritten.
        """
        staged = _reserve_private_name(path, "restore")
        try:
            staged.write_bytes(content)
            os.chmod(staged, mode)
            os.link(staged, path)
        finally:
            try:
                if staged.exists():
                    staged.unlink()
            except OSError:
                pass

    def _put_back_claim(
        self,
        claim: Path,
        path: Path,
        problems: List[str],
        *,
        operation: str = "unknown",
        reason: str = "the live path was taken by a newer writer, so this call's capture was not put back",
    ) -> None:
        """Return a captured object that turned out not to be this call's write.

        The object is put back only if the path is still free; whatever else may
        now be there is left untouched, and the conflict is always reported.  An
        object that cannot be put back is *kept* under its private name (受限方案丙)
        and recorded for the self-check: the live path already belongs to somebody
        else, so deleting the capture would destroy the only remaining name of
        that inode.
        """
        try:
            os.link(claim, path)
        except FileExistsError:
            self._retain_claimed_object(
                claim, path, None, problems, operation=operation,
                reason=f"{reason} (the live path is occupied)",
            )
            problems.append(f"{path}: changed after this call wrote it; leaving it in place")
            return
        except OSError as exc:
            problems.append(f"{path}: {exc}")
            self._retain_claimed_object(
                claim, path, None, problems, operation=operation,
                reason=f"{reason} ({exc})",
            )
            return
        try:
            claim.unlink()
        except OSError:
            pass
        problems.append(f"{path}: changed after this call wrote it; leaving it in place")

    def _retain_claimed_object(
        self,
        claim: Path,
        path: Path,
        installed_sha256: Optional[str],
        problems: List[str],
        *,
        operation: str,
        reason: str,
    ) -> None:
        """受限丙：把被认领的原对象留在私有名字下，绝不 unlink 它的最后路径。

        旧实现在这里调用 ``claim.unlink()``：认领之前就持有旧 fd 的写者，之后通过
        那个 fd 写进同一个 inode 的字节，会随最后一个名字一起消失。本版保留的是
        **原对象本身**（同一个 inode，只换名，没有任何字节被复制），并在
        ``<runtime_dir>/memory-retained/retained-objects.jsonl`` 追加一条只含元数据
        的发现记录，供跨重启自检列出。记录写不出来不影响保留：对象已经在私有名字
        下，报告照样给出真实绝对位置。
        """
        context = getattr(self, "_retention_context", None)
        context = context if isinstance(context, dict) else {}
        record_error = retained_objects.record_retained(
            getattr(self, "config", None),
            retained_path=claim,
            original_path=path,
            operation=operation,
            reason=reason,
            task_id=context.get("task_id"),
            written_bytes_sha256=installed_sha256,
            writer_pid=os.getpid(),
        )
        problems.append(
            f"{path}: {reason}; the captured original object is kept at {claim} and is "
            "not deleted, so bytes written through file descriptors opened before the "
            "claim are still there; manual verification is required"
        )
        if record_error is not None:
            problems.append(
                f"{claim}: the retained object could not be recorded for the self-check "
                f"({record_error}); the object is still kept at {claim} and nothing was "
                "deleted"
            )

    def _undo_installed_file(
        self,
        path: Path,
        installed_sha256: str,
        original: Optional[tuple],
        problems: List[str],
        *,
        operation: str = "unknown",
    ) -> None:
        """Undo one file this call wrote, without ever trusting a stale read.

        The check and the undo are a single step: the object is first moved aside
        atomically (:meth:`_claim_for_undo`) and attribution is decided on the
        *captured* bytes, never on a read of the live path.  A file a second
        writer changed between the check and the undo is therefore never deleted
        or overwritten -- it is either the object this call put back, or a newer
        object left in place -- and the conflict is always reported.

        v1.5.5 restricted plan C: when the captured object *is* this call's write
        and the live path cannot keep it, the object is no longer unlinked.  It is
        retained under the private name it was claimed to, so a writer holding a
        file descriptor opened before the claim does not lose the bytes it wrote
        afterwards.  The only branch that still removes the private name is the one
        that first hard-links the object back to the live path, so the object keeps
        a name either way.
        """
        try:
            current = path.read_bytes() if path.is_file() else None
        except OSError as exc:
            problems.append(f"{path}: {exc}")
            return
        if current is None:
            if original is not None:
                problems.append(f"{path}: gone after this call replaced it; not recreating it")
            return
        if hashlib.sha256(current).hexdigest() != installed_sha256:
            if original is not None and current == original[0]:
                return  # already back to the previous bytes
            problems.append(f"{path}: changed after this call wrote it; leaving it in place")
            return
        try:
            claim = self._claim_for_undo(path)
        except FileNotFoundError:
            problems.append(f"{path}: removed while this call was undoing it; leaving it in place")
            return
        except OSError as exc:
            problems.append(f"{path}: {exc}")
            return
        try:
            claimed = claim.read_bytes()
        except OSError as exc:
            problems.append(f"{path}: {exc}")
            return
        if hashlib.sha256(claimed).hexdigest() != installed_sha256:
            # The live path changed between the check and the atomic claim: the
            # captured object belongs to somebody else.
            self._put_back_claim(
                claim,
                path,
                problems,
                operation=operation,
                reason=(
                    "the object captured at this path was not this call's own write, so "
                    "it could not be put back"
                ),
            )
            return
        if original is None:
            self._retain_claimed_object(
                claim,
                path,
                installed_sha256,
                problems,
                operation=operation,
                reason=(
                    "this call created the file and its write is being removed from "
                    "the live path"
                ),
            )
            if path.exists():
                problems.append(
                    f"{path}: recreated while this call was undoing it; leaving it in place"
                )
            return
        try:
            self._place_original(path, original[0], original[1])
        except FileExistsError:
            # A newer writer took the path while we were undoing: keep its content
            # and retain our captured object instead of deleting it, so bytes a
            # pre-claim file descriptor wrote into the object survive.
            self._retain_claimed_object(
                claim,
                path,
                installed_sha256,
                problems,
                operation=operation,
                reason=(
                    "a newer writer took the live path, so the previous bytes were not "
                    "put back"
                ),
            )
            problems.append(f"{path}: changed after this call wrote it; leaving it in place")
            return
        except OSError as exc:
            # Could not put the previous bytes back for another reason (disk
            # full, ...).  Return the captured object so the scene is unchanged;
            # the object keeps a name either way, so nothing is lost.
            try:
                os.link(claim, path)
                claim.unlink()
            except OSError:
                problems.append(f"{path}: {exc}")
                self._retain_claimed_object(
                    claim,
                    path,
                    installed_sha256,
                    problems,
                    operation=operation,
                    reason=(
                        "the previous bytes could not be put back, so the object this "
                        "call replaced is kept"
                    ),
                )
                return
            problems.append(
                f"{path}: {exc}; the captured object was returned to this path with a "
                "hard link, so no object is retained elsewhere"
            )
            return
        self._retain_claimed_object(
            claim,
            path,
            installed_sha256,
            problems,
            operation=operation,
            reason=(
                "the previous bytes were put back, so the object this call replaced is "
                "retained"
            ),
        )

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix='.tmp', dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            if output.write(text) != len(text):
                raise OSError('short publication journal write')
            output.flush()
            os.fsync(output.fileno())
        # mkstemp is private; published notes and the catalog keep their mode.
        os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _index_output_snapshot(self, graph_dir: Path) -> Dict[str, tuple]:
        """Bytes of the index outputs a publication may overwrite.

        The snapshot lives in memory only, for this call's own rollback: an
        ordinary publication never writes a disk backup or a restore copy of the
        tree.  Each entry is ``(content, mode, size, mtime_ns)``; a file whose
        bytes could not be read keeps ``content=None`` together with its size and
        mtime, so a later restore can still tell whether it changed.

        Objects an earlier rollback retained are excluded here *and* in the
        post-scan of :meth:`_rollback_index_outputs`: they are that rollback's own
        deliberate product, not a live index output and not a foreign change, so
        counting them either way would report a conflict that did not happen.
        """
        snapshot: Dict[str, tuple] = {}
        if not graph_dir.is_dir():
            return snapshot
        for path in sorted(graph_dir.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            if retained_objects.is_retained_path(self.config, path):
                continue
            relative = str(path.relative_to(graph_dir))
            try:
                stat = path.stat()
                snapshot[relative] = (path.read_bytes(), stat.st_mode & 0o777, stat.st_size, stat.st_mtime_ns)
            except OSError:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                snapshot[relative] = (None, stat.st_mode & 0o777, stat.st_size, stat.st_mtime_ns)
        return snapshot

    def _install_index_output(self, graph_dir: Path, source: Path) -> None:
        """Install one built index output and record it for rollback.

        The record holds the hash of the bytes this call installed together with
        the bytes it replaced, so a rollback can *prove* a file still holds this
        call's write before undoing it.  A file somebody else rewrote after us
        never matches that hash and is left alone.
        """
        target = graph_dir / source.name
        if target.is_symlink():
            raise RuntimeError("index targets must not be symlinks")
        original = None
        if target.is_file():
            stat = target.stat()
            original = (target.read_bytes(), stat.st_mode & 0o777)
        staged = graph_dir / f".{source.name}.{os.getpid()}.tmp"
        shutil.copy2(source, staged)
        os.chmod(staged, 0o600)
        installed = hashlib.sha256(staged.read_bytes()).hexdigest()
        os.replace(staged, target)
        self._index_installs[str(target.relative_to(graph_dir))] = {
            "installed": installed,
            "original": original,
        }

    def _rollback_index_outputs(self, graph_dir: Path, snapshot: Dict[str, tuple]) -> List[str]:
        """Undo the index outputs this call installed, and only those.

        Attribution comes from the replacement record and is decided on the
        object this call atomically claims, never from "the bytes differ from
        the snapshot" and never from a read that the undo then trusts.  Anything
        else that moved during the rebuild was written by somebody else, is kept
        as it is, and is reported, because the index can then no longer be
        proven consistent.
        """
        problems: List[str] = []
        for relative, record in sorted(self._index_installs.items()):
            target = graph_dir / relative
            self._undo_installed_file(
                target,
                record["installed"],
                record["original"],
                problems,
                operation="memory.index_output",
            )
        if not graph_dir.is_dir():
            return problems
        for path in sorted(graph_dir.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            # Retained objects are this rollback's own product; the self-check
            # enumerates them separately, so they are never a foreign change here.
            if retained_objects.is_retained_path(self.config, path):
                continue
            relative = str(path.relative_to(graph_dir))
            if relative in self._index_installs:
                continue
            try:
                before = snapshot.get(relative)
                if before is None:
                    problems.append(f"{path}: appeared while the index was rebuilt; leaving it in place")
                elif before[0] is None:
                    stat = path.stat()
                    if (stat.st_size, stat.st_mtime_ns) != (before[2], before[3]):
                        problems.append(f"{path}: changed while the index was rebuilt; leaving it in place")
                elif path.read_bytes() != before[0]:
                    problems.append(f"{path}: changed while the index was rebuilt; leaving it in place")
            except OSError as exc:
                problems.append(f"{path}: {exc}")
        return problems

    def _rollback_publication_writes(
        self,
        graph_dir: Path,
        snapshot: Dict[str, tuple],
        note: Path,
        note_text: str,
        catalog: Path,
        original_catalog: str,
        catalog_text: str,
        temporaries: tuple,
        original_catalog_mode: Optional[int] = None,
    ) -> List[str]:
        """Undo only the writes this call can prove are its own.

        The note and the catalog are checked against the exact bytes this call
        wrote and undone through the same atomic claim as the index outputs, so
        a file another writer changed after the check is never deleted or
        overwritten.  A file that no longer matches is somebody else's content
        and is left in place, and the conflict is reported.
        """
        problems: List[str] = []
        for leftover in temporaries:
            try:
                leftover.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                problems.append(f"{leftover}: {exc}")
        note_sha256 = hashlib.sha256(note_text.encode("utf-8")).hexdigest()
        self._undo_installed_file(note, note_sha256, None, problems, operation="memory.note")
        catalog_sha256 = hashlib.sha256(catalog_text.encode("utf-8")).hexdigest()
        catalog_mode = 0o600 if original_catalog_mode is None else original_catalog_mode
        self._undo_installed_file(
            catalog,
            catalog_sha256,
            (original_catalog.encode("utf-8"), catalog_mode),
            problems,
            operation="memory.catalog",
        )
        problems.extend(self._rollback_index_outputs(graph_dir, snapshot))
        return problems

    @staticmethod
    def _without_catalog_link(text: str, link_line: str) -> str:
        return "\n".join(
            line for line in text.splitlines() if line.strip() != link_line
        ).rstrip() + "\n"

    def publish_candidate(self, task_id: str, expected_sha256: Optional[str] = None) -> Dict[str, Any]:
        with self._publication_lock():
            return self._publish_candidate_unlocked(task_id, expected_sha256)

    def _publish_candidate_unlocked(self, task_id: str, expected_sha256: Optional[str] = None) -> Dict[str, Any]:
        if not self.config.memory.get("publish_enabled"):
            raise RuntimeError("memory.publish_enabled=false")
        # File-only calls are not an approval bypass. Use the same gate as the
        # public transaction, on a read-only connection (the public caller may
        # already hold the SQLite write transaction).
        from .store import EventStore
        with closing(sqlite3.connect(self.config.database_path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            approved = EventStore(self.config.database_path)._publication_gate_candidate(conn, task_id, expected_sha256)
            approved_sha256 = approved['evidence_sha256']
        # Objects retained by this call's rollback are recorded against the task
        # whose write is being undone, so the self-check can name it later.
        self._retention_context = {"task_id": task_id}
        pending_source = self.staged_path(task_id)
        published_marker = self.outbox / f"{task_id}.published"
        source = pending_source if pending_source.is_file() else published_marker
        if not source.is_file():
            raise RuntimeError("memory candidate file is missing")
        source_bytes = source.read_bytes()
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        if source_sha256 != approved_sha256:
            raise RuntimeError("memory candidate changed after final review")
        if expected_sha256 and source_sha256 != expected_sha256:
            raise RuntimeError("memory candidate changed after user review")
        staged_text = source_bytes.decode("utf-8")
        sensitive = self._sensitive_reason(staged_text)
        if sensitive:
            raise RuntimeError(f"sensitive content gate failed: {sensitive}")
        # B19 (write boundary), second layer: a staged note is re-checked right
        # before any Vault write.  This is the backstop for content that
        # bypassed the creation gate -- a staged file dropped in by hand or by
        # an older code path -- and it runs before the note, the catalog line
        # or the index is touched, so a refusal leaves the Vault byte-identical.
        boundary = b19_write_boundary_reason(staged_text)
        if boundary:
            raise RuntimeError(boundary)

        vault = Path(self.config.memory["obsidian_vault"]).expanduser().resolve()
        destination_dir = vault / self.config.memory["obsidian_subdir"]
        catalog = vault / self.config.memory.get("catalog_note", "70_复利规则/经验索引.md")
        if not vault.is_dir() or not catalog.is_file():
            raise RuntimeError("current memory vault or experience catalog is unavailable")
        destination_dir.mkdir(parents=True, exist_ok=True)
        try:
            destination_dir.resolve().relative_to(vault)
        except ValueError as exc:
            raise RuntimeError("memory destination escapes the configured vault") from exc
        if catalog.is_symlink() or destination_dir.is_symlink():
            raise RuntimeError("memory publication paths must not be symlinks")
        published_text = self._render_published(task_id, staged_text)
        journal_path = self.outbox / f"{task_id}.publication.json"
        journal = json.loads(journal_path.read_text()) if journal_path.is_file() else None
        if journal:
            if journal.get('source_sha256') != source_sha256 or journal.get('vault') != str(vault):
                raise MemoryRecoveryError('publication recovery identity changed')
            # Rendering has a date field: retries on a later day use the date
            # from the original intent, not freshly rendered bytes.
            published_text = re.sub(r'last_verified: "[^"]+"',
                                    'last_verified: "' + journal['date'] + '"', published_text, count=1)
        target = self.find_note(vault, task_id) or destination_dir / self.note_filename(published_text, task_id)
        try:
            relative_target = target.relative_to(vault).with_suffix("")
        except ValueError as exc:
            raise RuntimeError("memory destination escapes the configured vault") from exc
        link = f"[[{relative_target}]]"
        original_catalog = catalog.read_text(encoding="utf-8")
        try:
            original_catalog_mode = catalog.stat().st_mode & 0o777
        except OSError:
            original_catalog_mode = None
        if target.exists():
            existing = target.read_text(encoding="utf-8")
            if journal and (journal.get('target') != str(target)
                            or hashlib.sha256(existing.encode()).hexdigest() != journal.get('note_sha256')):
                raise MemoryRecoveryError('publication recovery conflicts with changed note')
            if link not in original_catalog and journal:
                if (journal.get('target') != str(target) or existing != published_text
                        or hashlib.sha256(original_catalog.encode()).hexdigest() != journal.get('catalog_before')):
                    raise MemoryRecoveryError('publication recovery conflicts with changed note or catalog')
                self._atomic_write_text(catalog, original_catalog.rstrip() + f"\n- {link}\n")
                original_catalog = catalog.read_text(encoding='utf-8')
                journal['phase'] = 'catalog_installed'
                self._atomic_write_text(journal_path, json.dumps(journal, ensure_ascii=False))
            if f'source_task_id: "{task_id}"' not in existing or link not in original_catalog:
                raise RuntimeError("conflicting published memory already exists")
            # Re-verification rebuilds the index too, so it carries the same
            # rollback boundary as a first publication.
            graph_dir = Path(self.config.memory["knowlp_graph_dir"]).expanduser().resolve()
            snapshot = self._index_output_snapshot(graph_dir)
            self._index_installs = {}
            try:
                index_evidence = self._rebuild_and_verify_index(vault)
            except Exception as exc:
                problems = self._rollback_index_outputs(graph_dir, snapshot)
                if problems and not isinstance(exc, MemoryRecoveryError):
                    raise MemoryRecoveryError(
                        "index re-verification failed and could not be fully rolled back; "
                        "manual action is required for: " + "; ".join(problems)
                    ) from exc
                raise
            if pending_source.is_file():
                os.replace(pending_source, published_marker)
            if journal:
                journal['phase'] = 'complete'
                self._atomic_write_text(journal_path, json.dumps(journal, ensure_ascii=False))
            return {
                "published": 1,
                "task_id": task_id,
                "path": str(target),
                "sha256": hashlib.sha256(existing.encode("utf-8")).hexdigest(),
                "index": index_evidence,
                "idempotent": True,
            }
        if link in original_catalog:
            raise RuntimeError("experience catalog already contains this task")
        evidence_sha256 = hashlib.sha256(published_text.encode("utf-8")).hexdigest()

        graph_dir = Path(self.config.memory["knowlp_graph_dir"]).expanduser().resolve()
        snapshot = self._index_output_snapshot(graph_dir)
        self._index_installs = {}
        catalog_text = original_catalog.rstrip() + f"\n- {link}\n"
        journal = {'task_id': task_id, 'source_sha256': source_sha256, 'vault': str(vault),
                   'target': str(target), 'note_sha256': evidence_sha256,
                   'catalog_before': hashlib.sha256(original_catalog.encode()).hexdigest(),
                   'date': re.search(r'last_verified: "([^"]+)"', published_text).group(1),
                   'phase': 'prepared'}
        self._atomic_write_text(journal_path, json.dumps(journal, ensure_ascii=False))
        target_tmp = destination_dir / f".{task_id}.{os.getpid()}.tmp"
        catalog_tmp = catalog.parent / f".{catalog.name}.{os.getpid()}.tmp"
        try:
            target_tmp.write_text(published_text, encoding="utf-8")
            catalog_tmp.write_text(catalog_text, encoding="utf-8")
            os.replace(target_tmp, target)
            journal['phase'] = 'note_installed'
            self._atomic_write_text(journal_path, json.dumps(journal, ensure_ascii=False))
            os.replace(catalog_tmp, catalog)
            journal['phase'] = 'catalog_installed'
            self._atomic_write_text(journal_path, json.dumps(journal, ensure_ascii=False))
            index_evidence = self._rebuild_and_verify_index(vault)
        except Exception as exc:
            # Undo only what this call can prove it wrote: the note and the
            # catalog line against their exact bytes, the index outputs against
            # the replacement record.  Content another writer produced in the
            # meantime is kept as it is, and a rollback that cannot finish, or
            # that has to leave foreign content behind, is reported as needing
            # manual action instead of being called recovered.
            problems = self._rollback_publication_writes(
                graph_dir, snapshot, target, published_text,
                catalog, original_catalog, catalog_text, (target_tmp, catalog_tmp),
                original_catalog_mode,
            )
            if problems:
                raise MemoryRecoveryError(
                    "publication failed and could not be fully rolled back; manual "
                    "action is required for: " + "; ".join(problems)
                ) from exc
            raise
        if pending_source.is_file():
            os.replace(pending_source, published_marker)
        journal['phase'] = 'complete'
        self._atomic_write_text(journal_path, json.dumps(journal, ensure_ascii=False))
        return {
            "published": 1,
            "task_id": task_id,
            "path": str(target),
            "sha256": evidence_sha256,
            "index": index_evidence,
        }

    def rename_note(self, task_id: str, dry_run: bool = False) -> Dict[str, Any]:
        """把一篇按整串编号命名的经验笔记改成可读名字，同步索引笔记里的链接，再重建并校验索引。

        只改文件名、笔记开头的标题行和索引笔记里对应的那一行链接；失败时恢复原状。
        """
        with self._publication_lock():
            vault = Path(self.config.memory["obsidian_vault"]).expanduser().resolve()
            catalog = vault / self.config.memory.get("catalog_note", "70_复利规则/经验索引.md")
            source = self.find_note(vault, task_id)
            if source is None or source.is_symlink():
                raise RuntimeError("published memory note is missing")
            original = source.read_text(encoding="utf-8")
            head = re.search(r"\A---[ \t]*\n.*?\n---[ \t]*\n", original, re.DOTALL)
            if not head:
                raise RuntimeError("published memory note has no frontmatter")
            body = original[head.end():].strip()
            stale = re.match(r"#\s+已审核经验\s+" + re.escape(task_id) + r"\s*\n+", body)
            if stale:
                body = body[stale.end():].strip()
            renamed_text = original[:head.end()] + "\n" + self._titled_body(task_id, body) + "\n"
            target = source.with_name(self.note_filename(renamed_text, task_id))
            result = {"task_id": task_id, "from": str(source), "to": str(target),
                      "changed": target != source or renamed_text != original}
            if not result["changed"]:
                return result
            # 预览也要先查冲突，否则试运行说能改、真改时才报错
            if target != source and target.exists():
                raise RuntimeError("a note with the readable name already exists")
            old_link = f"- [[{source.relative_to(vault).with_suffix('')}]]"
            new_link = f"- [[{target.relative_to(vault).with_suffix('')}]]"
            original_catalog = catalog.read_text(encoding="utf-8")
            lines = original_catalog.splitlines()
            if sum(1 for line in lines if line.strip() == old_link) != 1:
                raise RuntimeError("experience catalog does not link this note exactly once")
            new_catalog = "\n".join(new_link if line.strip() == old_link else line for line in lines)
            new_catalog += "\n" if original_catalog.endswith("\n") else ""
            if dry_run:
                return result
            try:
                self._atomic_write_text(target, renamed_text)
                if target != source:
                    source.unlink()
                self._atomic_write_text(catalog, new_catalog)
                result["index"] = self._rebuild_and_verify_index(vault)
            except Exception:
                # 先把原文写回去，再删新文件：中途再出错也至少留着一份
                self._atomic_write_text(source, original)
                if target != source and target.exists():
                    target.unlink()
                self._atomic_write_text(catalog, original_catalog)
                raise
            return result

    def retire_candidate(self, task_id: str, published_path: Optional[str] = None) -> Dict[str, Any]:
        with self._publication_lock():
            return self._retire_candidate_unlocked(task_id, published_path)

    def _retire_candidate_unlocked(self, task_id: str, published_path: Optional[str] = None) -> Dict[str, Any]:
        vault = Path(self.config.memory["obsidian_vault"]).expanduser().resolve()
        target_input = (
            Path(published_path).expanduser()
            if published_path
            else self.find_note(vault, task_id) or vault / self.config.memory["obsidian_subdir"] / f"{task_id}.md"
        )
        if target_input.is_symlink():
            raise RuntimeError("published memory must not be a symlink")
        target = target_input.resolve()
        try:
            relative_target = target.relative_to(vault).with_suffix("")
        except ValueError as exc:
            raise RuntimeError("published memory escapes the configured vault") from exc
        catalog = vault / self.config.memory.get("catalog_note", "70_复利规则/经验索引.md")
        if catalog.is_symlink() or not catalog.is_file():
            raise RuntimeError("experience catalog is unavailable")
        link_line = f"- [[{relative_target}]]"
        original_catalog = catalog.read_text(encoding="utf-8")
        archive_dir = self.config.runtime_dir / "memory-retired"
        self._secure_runtime_dir(archive_dir)
        archive = archive_dir / f"{task_id}.md"
        legacy_archives = sorted(archive_dir.glob(f"{task_id}-*.md"))
        if not archive.is_file() and legacy_archives:
            archive = legacy_archives[-1]

        if not target.is_file():
            if not archive.is_file():
                raise RuntimeError("published memory is missing and no recoverable archive exists")
            try:
                if link_line in {line.strip() for line in original_catalog.splitlines()}:
                    self._atomic_write_text(
                        catalog, self._without_catalog_link(original_catalog, link_line)
                    )
                index_evidence = self._rebuild_and_verify_index(vault)
            except Exception:
                self._atomic_write_text(catalog, original_catalog)
                raise
            return {
                "retired": 1,
                "task_id": task_id,
                "archive_path": str(archive),
                "index": index_evidence,
                "idempotent": True,
            }

        original_memory = target.read_bytes()
        try:
            original_text = original_memory.decode("utf-8")
        except UnicodeError as exc:
            raise RuntimeError("published memory is not valid UTF-8") from exc
        quarantined_text, status_count = re.subn(
            r'^status:\s*"(?:active|revalidation_required)"\s*$',
            'status: "revalidation_required"',
            original_text,
            count=1,
            flags=re.MULTILINE,
        )
        if status_count != 1:
            raise RuntimeError("published memory status field is invalid")
        quarantined_memory = quarantined_text.encode("utf-8")
        archive_created = False
        try:
            # Make stale graph hits harmless before touching the catalog or
            # removing the file.  A process crash from this point onward is
            # fail-closed and the stable archive name makes retry idempotent.
            if quarantined_memory != original_memory:
                self._atomic_write_text(target, quarantined_text)
            if archive.is_file():
                if archive.read_bytes() != quarantined_memory:
                    raise RuntimeError("recoverable retirement archive conflicts with published memory")
            else:
                archive_tmp = archive_dir / f".{archive.name}.{os.getpid()}.tmp"
                archive_tmp.write_bytes(quarantined_memory)
                os.chmod(archive_tmp, 0o600)
                os.replace(archive_tmp, archive)
                archive_created = True
            self._atomic_write_text(
                catalog, self._without_catalog_link(original_catalog, link_line)
            )
            target.unlink()
            index_evidence = self._rebuild_and_verify_index(vault)
        except Exception:
            if not target.exists():
                target.write_bytes(original_memory)
            self._atomic_write_text(catalog, original_catalog)
            if archive_created and archive.exists():
                archive.unlink()
            raise
        return {
            "retired": 1,
            "task_id": task_id,
            "archive_path": str(archive),
            "index": index_evidence,
        }

    def set_validation_status(self, task_id: str, published_path: str, status: str) -> Dict[str, Any]:
        if status not in {"active", "revalidation_required"}:
            raise ValueError("invalid memory validation status")
        with self._publication_lock():
            vault = Path(self.config.memory["obsidian_vault"]).expanduser().resolve()
            target_input = Path(published_path).expanduser()
            if target_input.is_symlink():
                raise RuntimeError("published memory must not be a symlink")
            target = target_input.resolve()
            try:
                target.relative_to(vault)
            except ValueError as exc:
                raise RuntimeError("published memory escapes the configured vault") from exc
            if not target.is_file() or target.is_symlink():
                raise RuntimeError("published memory is unavailable")
            catalog = vault / self.config.memory.get("catalog_note", "70_复利规则/经验索引.md")
            if catalog.is_symlink() or not catalog.is_file():
                raise RuntimeError("experience catalog is unavailable")
            relative_target = target.relative_to(vault).with_suffix("")
            link = f"[[{relative_target}]]"
            link_line = f"- {link}"
            original = target.read_text(encoding="utf-8")
            original_catalog = catalog.read_text(encoding="utf-8")
            if f'source_task_id: "{task_id}"' not in original:
                raise RuntimeError("published memory does not belong to the requested task")
            updated, count = re.subn(
                r'^status:\s*"(?:active|revalidation_required)"\s*$',
                f'status: "{status}"',
                original,
                count=1,
                flags=re.MULTILINE,
            )
            if count != 1:
                raise RuntimeError("published memory status field is invalid")
            catalog_link_count = sum(
                1 for line in original_catalog.splitlines() if line.strip() == link_line
            )
            if catalog_link_count > 1:
                raise RuntimeError("experience catalog contains duplicate memory links")
            if status == "revalidation_required":
                updated_catalog = self._without_catalog_link(original_catalog, link_line)
            else:
                updated_catalog = original_catalog
                if catalog_link_count == 0:
                    updated_catalog = original_catalog.rstrip() + f"\n- {link}\n"
            try:
                # Quarantine changes the note first, so a stale graph hit is
                # rejected immediately. Reactivation restores the parent link
                # first, while the note remains ineligible until its status is
                # changed and the new graph is committed.
                if status == "revalidation_required":
                    if updated != original:
                        self._atomic_write_text(target, updated)
                    if updated_catalog != original_catalog:
                        self._atomic_write_text(catalog, updated_catalog)
                else:
                    if updated_catalog != original_catalog:
                        self._atomic_write_text(catalog, updated_catalog)
                    if updated != original:
                        self._atomic_write_text(target, updated)
                index_evidence = self._rebuild_and_verify_index(vault)
            except Exception:
                self._atomic_write_text(target, original)
                self._atomic_write_text(catalog, original_catalog)
                raise
            return {
                "task_id": task_id,
                "path": str(target),
                "status": status,
                "catalog_linked": status == "active",
                "index": index_evidence,
            }

    def publish(self) -> Dict[str, Any]:
        return {
            "published": 0,
            "reason": "bulk publication is disabled; use a final approved task review",
        }
