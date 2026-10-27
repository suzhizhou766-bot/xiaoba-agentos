#!/usr/bin/env python3
"""SFX 使用日志：记录匹配请求、采用与评分，供匹配引擎迭代优化。"""
import json
import pathlib
import threading
import uuid
from datetime import datetime, timezone

LOG_DIR = pathlib.Path(__file__).resolve().parent.parent / "usage_logs"
LOG_FILE = LOG_DIR / "sfx_usage.jsonl"
_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _append(record):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def log_match(profile, dur, top, results, project=None):
    """记录一次匹配请求，返回 request_id 供后续反馈关联。"""
    request_id = str(uuid.uuid4())[:8]
    _append({
        "type": "match",
        "ts": _now(),
        "request_id": request_id,
        "profile": profile,
        "dur": dur,
        "top": top,
        "project": project,
        "candidates": [
            {"score": r.get("score"), "name": r.get("name"), "path": r.get("path")}
            for r in (results or [])
        ],
    })
    return request_id


def log_feedback(request_id, selected_path, rating, project=None, note=None):
    """记录人工或脚本反馈：采用哪个文件、评分多少。"""
    _append({
        "type": "feedback",
        "ts": _now(),
        "request_id": request_id,
        "selected_path": selected_path,
        "rating": rating,
        "project": project,
        "note": note,
    })


def log_adopt(profile, desc, picks, project=None):
    """记录 sfx_gen 自动采用的结果，便于统计真实使用频率。"""
    _append({
        "type": "adopt",
        "ts": _now(),
        "profile": profile,
        "desc": desc,
        "project": project,
        "picked": [
            {"score": s, "name": r.get("name"), "path": r.get("path")}
            for s, r in picks
        ],
    })
