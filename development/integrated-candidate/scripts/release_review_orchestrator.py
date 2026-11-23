#!/usr/bin/env python3
"""W2-J3 & W2-J4: Release review fault isolation, heartbeat & incremental convergence orchestrator.

Responsibilities:
1. W2-J3: Classify review-node failures strictly into hard_config (stop immediately),
   transient_network (retry up to 2 times after 15s backoff in fresh isolated attempt dirs,
   preserving prior dirs/receipts), or unknown (fail-closed, stop immediately).
   Provide a detached heartbeat writer so long review runs always emit observable liveness.
2. W2-J4: Build Round-1 full packets, Round-2+ incremental diff + defect-checklist packets,
   final P0-only convergence packets, and a hard 3-round non-convergence circuit breaker
   that pauses and returns control to the user.
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

MAX_AUTO_REVIEW_ROUNDS = 3
DEFAULT_TRANSIENT_MAX_RETRIES = 2
DEFAULT_TRANSIENT_BACKOFF_SECONDS = 15.0
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10.0

_HARD_CONFIG_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("engine_retired", re.compile(r"\bengine_retired\b|workbuddy.*retired|已退役", re.I)),
    ("invalid_model", re.compile(r"\bmodel_not_found\b|\binvalid_model\b|model\b[^\n]{0,40}\bnot found\b|unsupported model|unknown model|模型不存在|无效模型", re.I)),
    ("auth_or_quota", re.compile(r"\b401\b|\b403\b|unauthorized|forbidden|invalid_api_key|authentication failed|creditStatus|insufficient[_\s-]?quota|quota exceeded|账户额度|认证失败|权限不足", re.I)),
    ("missing_binary", re.compile(r"command not found|no such file or directory|FileNotFoundError|\bENOENT\b|未找到可执行", re.I)),
    ("invalid_arguments", re.compile(r"unrecognized arguments|unknown option|invalid argument|usage:\s|schema validation failed|参数错误", re.I)),
    ("sensitive_blocked", re.compile(r"sensitive_content|命中敏感信息|sensitivity", re.I)),
)

_TRANSIENT_NETWORK_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("unexpected_eof", re.compile(r"unexpected\s+eof|\beof\b.*(?:read|tls|ssl|handshake|stream|socket)|stream disconnected|incomplete read", re.I)),
    ("tls_handshake", re.compile(r"\btls\b.*(?:timeout|reset|handshake|error)|\bssl\b.*(?:eof|handshake|error|wrong version)", re.I)),
    ("connection_reset", re.compile(r"connection reset|econnreset|broken pipe|\bepipe\b|connection aborted|remote end closed", re.I)),
    ("network_timeout", re.compile(r"timed?\s*out|\betimedout\b|read timeout|connect timeout|deadline exceeded|gateway timeout|\b504\b", re.I)),
    ("upstream_unavailable", re.compile(r"\b502\b|\b503\b|bad gateway|service unavailable|temporary failure in name resolution|network is unreachable|eai_again", re.I)),
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_review_failure(
    error_text: str,
    exit_code: Optional[int] = None,
) -> dict[str, Any]:
    """Classify a review execution failure into hard_config / transient_network / unknown.

    Hard configuration/auth/model errors are checked first so they never trigger blind retries.
    Unknown failures fail closed (retryable=False).
    """
    text = str(error_text or "").strip()
    for tag, pattern in _HARD_CONFIG_RULES:
        if pattern.search(text):
            return {
                "kind": "hard_config",
                "tag": tag,
                "retryable": False,
                "max_retries": 0,
                "backoff_seconds": 0.0,
                "exit_code": exit_code,
                "action": "stop_immediately",
                "reason": f"检测到硬配置/权限/模型错误（{tag}），立即中止，不自动重试",
            }
    for tag, pattern in _TRANSIENT_NETWORK_RULES:
        if pattern.search(text):
            return {
                "kind": "transient_network",
                "tag": tag,
                "retryable": True,
                "max_retries": DEFAULT_TRANSIENT_MAX_RETRIES,
                "backoff_seconds": DEFAULT_TRANSIENT_BACKOFF_SECONDS,
                "exit_code": exit_code,
                "action": "isolated_retry_with_backoff",
                "reason": f"检测到瞬时网络/连接异常（{tag}），保留原目录并等待退避后在隔离目录重试",
            }
    return {
        "kind": "unknown",
        "tag": "unclassified",
        "retryable": False,
        "max_retries": 0,
        "backoff_seconds": 0.0,
        "exit_code": exit_code,
        "action": "stop_for_inspection",
        "reason": "未识别的失败类型，默认停止以避免盲目重试掩盖根因",
    }


class DetachedHeartbeat:
    """Writes periodic heartbeat snapshots in a daemon thread without blocking callers."""

    def __init__(
        self,
        heartbeat_path: Path,
        *,
        run_id: str,
        attempt: int,
        interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self.heartbeat_path = Path(heartbeat_path)
        self.run_id = run_id
        self.attempt = attempt
        self.interval_seconds = max(0.05, float(interval_seconds))
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_monotonic = 0.0
        self.beats = 0

    def _write(self, status: str) -> None:
        self.beats += 1
        payload = {
            "run_id": self.run_id,
            "attempt": self.attempt,
            "status": status,
            "beats": self.beats,
            "elapsed_seconds": round(max(0.0, time.monotonic() - self._started_monotonic), 3),
            "updated_at": _utc_now_iso(),
        }
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.heartbeat_path.with_suffix(self.heartbeat_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.heartbeat_path)

    def _loop(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            try:
                self._write("running")
            except OSError:
                break

    def start(self) -> "DetachedHeartbeat":
        self._started_monotonic = time.monotonic()
        self._write("running")
        self._thread = threading.Thread(
            target=self._loop,
            name=f"review-hb-{self.run_id}-{self.attempt}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self, final_status: str = "finished") -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 2))
        self._write(final_status)


def execute_with_retry_isolation(
    attempt_fn: Callable[[Path, int], dict[str, Any]],
    *,
    work_root: Path,
    run_id: str,
    max_retries: int = DEFAULT_TRANSIENT_MAX_RETRIES,
    backoff_seconds: float = DEFAULT_TRANSIENT_BACKOFF_SECONDS,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run review attempts with isolated per-attempt directories, preserved failed dirs, and heartbeats.

    ``attempt_fn(attempt_dir, attempt_index)`` returns a dict containing at minimum:
      - ``ok``: bool
      - when ``ok`` is False: ``error`` (str) and optional ``exit_code`` (int)
      - optional ``receipt`` / extra fields.
    """
    safe_run_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(run_id or "review").strip()) or "review"
    root = Path(work_root)
    root.mkdir(parents=True, exist_ok=True)
    attempts: list[dict[str, Any]] = []
    max_total_attempts = 1 + max(0, int(max_retries))

    for attempt_idx in range(max_total_attempts):
        attempt_dir = root / f"{safe_run_id}-attempt-{attempt_idx}"
        attempt_dir.mkdir(parents=True, exist_ok=False)
        hb_path = attempt_dir / "heartbeat.json"
        hb = DetachedHeartbeat(
            hb_path,
            run_id=safe_run_id,
            attempt=attempt_idx,
            interval_seconds=heartbeat_interval,
        ).start()
        try:
            outcome = dict(attempt_fn(attempt_dir, attempt_idx) or {})
        except Exception as exc:
            outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "exit_code": None}
        finally:
            hb.stop("finished" if outcome.get("ok") else "failed")

        record: dict[str, Any] = {
            "attempt": attempt_idx,
            "attempt_dir": str(attempt_dir),
            "heartbeat_path": str(hb_path),
            "heartbeat_beats": hb.beats,
            "ok": bool(outcome.get("ok")),
            "outcome": outcome,
        }
        if record["ok"]:
            attempts.append(record)
            return {
                "ok": True,
                "run_id": safe_run_id,
                "attempts_used": len(attempts),
                "attempts": attempts,
                "final_attempt_dir": str(attempt_dir),
                "final_outcome": outcome,
            }

        classification = classify_review_failure(
            str(outcome.get("error") or ""),
            exit_code=outcome.get("exit_code"),
        )
        record["classification"] = classification
        attempts.append(record)

        retry_allowed = (
            classification["retryable"]
            and attempt_idx < min(int(max_retries), int(classification["max_retries"]))
        )
        if not retry_allowed:
            return {
                "ok": False,
                "run_id": safe_run_id,
                "attempts_used": len(attempts),
                "attempts": attempts,
                "stopped_reason": classification["kind"],
                "classification": classification,
                "preserved_attempt_dirs": [a["attempt_dir"] for a in attempts],
            }
        sleep_fn(float(backoff_seconds))

    last_cls = attempts[-1].get("classification", {}) if attempts else {}
    return {
        "ok": False,
        "run_id": safe_run_id,
        "attempts_used": len(attempts),
        "attempts": attempts,
        "stopped_reason": "retry_exhausted",
        "classification": last_cls,
        "preserved_attempt_dirs": [a["attempt_dir"] for a in attempts],
    }


def compute_manifest_diff(
    prev_manifest: Optional[dict[str, str]],
    curr_manifest: dict[str, str],
) -> dict[str, Any]:
    """Compute sorted added/modified/removed file lists between two {path: sha256} manifests."""
    prev = dict(prev_manifest or {})
    curr = dict(curr_manifest or {})
    added = sorted(k for k in curr if k not in prev)
    removed = sorted(k for k in prev if k not in curr)
    modified = sorted(k for k in curr if k in prev and prev[k] != curr[k])
    unchanged = sorted(k for k in curr if k in prev and prev[k] == curr[k])
    return {
        "added": added,
        "modified": modified,
        "removed": removed,
        "changed_files": sorted(set(added) | set(modified) | set(removed)),
        "unchanged_count": len(unchanged),
    }


def build_incremental_review_packet(
    round_number: int,
    *,
    prev_manifest: Optional[dict[str, str]],
    curr_manifest: dict[str, str],
    prev_defects: Optional[list[dict[str, Any]]] = None,
    final_p0_only: bool = False,
    max_rounds: int = MAX_AUTO_REVIEW_ROUNDS,
) -> dict[str, Any]:
    """Build a review scope packet with incremental diffing, P0-only convergence, and circuit breaker."""
    if round_number < 1:
        raise ValueError("round_number must be >= 1")
    defects = [dict(d) for d in (prev_defects or [])]
    diff = compute_manifest_diff(prev_manifest, curr_manifest)

    if round_number > max_rounds:
        return {
            "status": "circuit_breaker_user_decision",
            "allow_dispatch": False,
            "mode": "halted",
            "round_number": round_number,
            "max_rounds": max_rounds,
            "reason": (
                f"同一版本已连续 {max_rounds} 轮未收敛，触发熔断机制；"
                "暂停自动派审，请用户裁定继续收敛、收窄范围或退回重做。"
            ),
            "unresolved_defects": defects,
            "diff": diff,
            "files_to_review": [],
        }

    if round_number == 1:
        all_files = sorted(curr_manifest.keys())
        return {
            "status": "ready",
            "allow_dispatch": True,
            "mode": "full_review",
            "round_number": 1,
            "max_rounds": max_rounds,
            "allowed_severities": ["P0", "P1", "P2"],
            "scope_instruction": "第 1 轮首审：按本版全量改动范围核验代码、契约与测试。",
            "files_to_review": all_files,
            "diff": diff,
            "defect_checklist": [],
        }

    if prev_manifest is None:
        raise ValueError("round_number >= 2 requires prev_manifest to build an incremental diff packet")

    checklist = [
        {
            "id": str(item.get("id") or f"D-{idx + 1}"),
            "severity": str(item.get("severity") or "P1").upper(),
            "summary": str(item.get("summary") or "").strip(),
            "file": str(item.get("file") or "").strip(),
            "recheck_status": "pending",
        }
        for idx, item in enumerate(defects)
    ]

    if final_p0_only:
        mode = "final_p0_only"
        allowed = ["P0"]
        instruction = (
            "最终收敛轮（P0-only）：只核对上轮遗留阻断项与本轮增量改动是否引入新的 P0 阻断缺陷，"
            "严禁对未改动文件或非阻断风格建议扩大发散。"
        )
    else:
        mode = "incremental_recheck"
        allowed = ["P0", "P1"]
        instruction = (
            "第 2+ 轮增量复审：仅审查相对上一轮的增量变更文件与上轮缺陷清单，"
            "不再全仓重审未改动文件。"
        )

    return {
        "status": "ready",
        "allow_dispatch": True,
        "mode": mode,
        "round_number": round_number,
        "max_rounds": max_rounds,
        "allowed_severities": allowed,
        "scope_instruction": instruction,
        "files_to_review": diff["changed_files"],
        "diff": diff,
        "defect_checklist": checklist,
    }


def render_packet_markdown(packet: dict[str, Any]) -> str:
    """Render a compact Markdown dispatch summary for reviewers or user decision."""
    lines = [
        f"# 发布审查包（第 {packet.get('round_number')} 轮 / 模式：{packet.get('mode')}）",
        "",
        f"- 状态：`{packet.get('status')}`（允许派审：`{packet.get('allow_dispatch')}`）",
    ]
    if not packet.get("allow_dispatch"):
        lines.append(f"- 熔断原因：{packet.get('reason')}")
        unresolved = packet.get("unresolved_defects") or []
        if unresolved:
            lines.append("")
            lines.append("## 未收敛缺陷清单")
            for item in unresolved:
                lines.append(f"- [{item.get('severity', 'P1')}] {item.get('id', '')} {item.get('summary', '')}".strip())
        return "\n".join(lines) + "\n"

    lines.append(f"- 审查口径：{packet.get('scope_instruction')}")
    lines.append(f"- 允许报告级别：{', '.join(packet.get('allowed_severities') or [])}")
    files = packet.get("files_to_review") or []
    lines.append(f"- 本轮审查文件数：{len(files)}")
    if files:
        lines.append("")
        lines.append("## 本轮审查文件")
        for f in files:
            lines.append(f"- `{f}`")
    checklist = packet.get("defect_checklist") or []
    if checklist:
        lines.append("")
        lines.append("## 上轮缺陷逐条核销清单")
        for item in checklist:
            loc = f"（`{item['file']}`）" if item.get("file") else ""
            lines.append(f"- [ ] `[{item['severity']}]` {item['id']}{loc}：{item['summary']}")
    return "\n".join(lines) + "\n"


def _load_manifest_files(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("files"), dict):
        return {str(k): str(v) for k, v in raw["files"].items()}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    raise ValueError(f"Unsupported manifest JSON structure: {path}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    c_p = sub.add_parser("classify", help="Classify a review failure message")
    c_p.add_argument("--error", required=True)
    c_p.add_argument("--exit-code", type=int, default=None)

    p_p = sub.add_parser("packet", help="Build an incremental review packet")
    p_p.add_argument("--round", type=int, required=True, dest="round_number")
    p_p.add_argument("--curr-manifest", type=Path, required=True)
    p_p.add_argument("--prev-manifest", type=Path, default=None)
    p_p.add_argument("--prev-defects", type=Path, default=None)
    p_p.add_argument("--final-p0-only", action="store_true")
    p_p.add_argument("--markdown", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "classify":
        print(json.dumps(classify_review_failure(args.error, args.exit_code), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "packet":
        prev = _load_manifest_files(args.prev_manifest) if args.prev_manifest else None
        curr = _load_manifest_files(args.curr_manifest)
        defects = json.loads(args.prev_defects.read_text(encoding="utf-8")) if args.prev_defects else []
        pkt = build_incremental_review_packet(
            args.round_number,
            prev_manifest=prev,
            curr_manifest=curr,
            prev_defects=defects,
            final_p0_only=args.final_p0_only,
        )
        if args.markdown:
            print(render_packet_markdown(pkt))
        else:
            print(json.dumps(pkt, ensure_ascii=False, indent=2))
        return 0 if pkt.get("allow_dispatch") else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
