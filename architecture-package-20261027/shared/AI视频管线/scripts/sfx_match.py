#!/usr/bin/env python3
"""音效多维匹配引擎 — 指纹相似度 + 精选/使用/评分偏好。"""
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "音效指纹库_v3.csv"
ASSISTANT_ROOT = Path("/Users/suzhizhou/Documents/私人助理")
LIBRARY = ASSISTANT_ROOT / "音效素材库"
INDEX = LIBRARY / "音效素材库索引.csv"
USAGE_LOG = ASSISTANT_ROOT / "AI视频管线" / "usage_logs" / "sfx_usage.jsonl"

_PREFERENCE_SIGNATURE = None
_PREFERENCE_CACHE = {}

def load():
    seen = set()
    rows = []
    with open(DB) as f:
        for r in csv.DictReader(f):
            try:
                duration = float(r.get("duration", 0) or 0)
            except (TypeError, ValueError):
                continue
            # 超短 UI 点击音是有效素材；click 模板本身允许 0.01 秒。
            if duration <= 0:
                continue
            key = r.get("path") or r.get("name")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            rows.append(r)
    return rows

def tags_of(r):
    return (r.get("keywords") or r.get("tags") or "").split("|")


def _absolute_path(value):
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = LIBRARY / path
    try:
        path.relative_to(LIBRARY)
    except ValueError:
        return None
    return str(path)


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _preference_signature():
    return tuple(
        (str(path), path.stat().st_mtime_ns if path.exists() else 0)
        for path in (INDEX, USAGE_LOG)
    )


def load_preferences():
    """读取索引和使用日志，并按文件修改时间缓存。

    索引用于人工精选和历史字段；日志用于实际采用、评分、否定和项目亲和度。
    两者重叠时取更可信的最大计数，不重复累加。
    """
    global _PREFERENCE_SIGNATURE, _PREFERENCE_CACHE
    signature = _preference_signature()
    if signature == _PREFERENCE_SIGNATURE:
        return _PREFERENCE_CACHE

    indexed = {}
    if INDEX.is_file():
        with INDEX.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                path = _absolute_path(row.get("相对路径") or row.get("路径"))
                if path:
                    indexed[path] = row

    stats = defaultdict(lambda: {
        "usage_count": 0,
        "ratings": [],
        "reject_count": 0,
        "projects": set(),
        "last_used": "",
    })
    if USAGE_LOG.is_file():
        request_projects = {}
        with USAGE_LOG.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if record.get("type") == "match" and record.get("request_id"):
                    request_projects[record["request_id"]] = record.get("project")
                    continue
                if record.get("type") == "feedback":
                    path = _absolute_path(record.get("selected_path"))
                    if not path:
                        continue
                    item = stats[path]
                    item["usage_count"] += 1
                    rating = _number(record.get("rating"), None)
                    if rating is not None and 1 <= rating <= 5:
                        item["ratings"].append(rating)
                        if rating <= 2:
                            item["reject_count"] += 1
                    project = record.get("project") or request_projects.get(record.get("request_id"))
                    if project:
                        item["projects"].add(str(project))
                    item["last_used"] = max(item["last_used"], str(record.get("ts") or ""))
                    continue
                if record.get("type") == "adopt":
                    for picked in record.get("picked") or []:
                        path = _absolute_path(picked.get("path"))
                        if not path:
                            continue
                        item = stats[path]
                        item["usage_count"] += 1
                        project = record.get("project")
                        if project:
                            item["projects"].add(str(project))
                        item["last_used"] = max(item["last_used"], str(record.get("ts") or ""))

    merged = {}
    for path in set(indexed) | set(stats):
        row = indexed.get(path, {})
        item = stats.get(path, {})
        log_ratings = item.get("ratings") or []
        index_rating = _number(row.get("评分"), 0)
        rating = sum(log_ratings) / len(log_ratings) if log_ratings else index_rating
        projects = set(item.get("projects") or set())
        projects.update(p.strip() for p in (row.get("使用项目") or "").split("|") if p.strip())
        merged[path] = {
            "curated": str(row.get("是否精选") or "").strip().lower() in {"是", "yes", "true", "1"},
            "usage_count": max(int(_number(row.get("使用次数"), 0)), int(item.get("usage_count", 0))),
            "rating": round(rating, 2) if rating else 0.0,
            "reject_count": max(int(_number(row.get("否定次数"), 0)), int(item.get("reject_count", 0))),
            "projects": projects,
            "last_used": item.get("last_used") or row.get("最近使用") or "",
        }

    _PREFERENCE_SIGNATURE = signature
    _PREFERENCE_CACHE = merged
    return merged


def preference_for(row, project=None):
    path = _absolute_path(row.get("path"))
    info = dict(load_preferences().get(path, {}))
    info.setdefault("usage_count", 0)
    info.setdefault("rating", 0.0)
    info.setdefault("reject_count", 0)
    info.setdefault("projects", set())

    try:
        top_folder = Path(path).relative_to(LIBRARY).parts[0] if path else ""
    except (ValueError, IndexError):
        top_folder = ""
    curated = bool(info.get("curated")) or top_folder in {"精选音效", "ElevenLabs精选音效"}
    usage_count = int(info.get("usage_count", 0))
    rating = float(info.get("rating", 0) or 0)
    rejects = int(info.get("reject_count", 0))

    if curated:
        tier = "selected"
    elif usage_count >= 3 or rating >= 4:
        tier = "selected_candidate"
    elif usage_count >= 1 or rating >= 3:
        tier = "common"
    elif rejects or (rating and rating <= 2):
        tier = "cold"
    else:
        tier = "unrated"

    bonus = {
        "selected": 18,
        "selected_candidate": 14,
        "common": 6,
        "cold": -8,
        "unrated": 0,
    }[tier]
    bonus += min(8, usage_count * 2)
    if rating >= 4.5:
        bonus += 8
    elif rating >= 4:
        bonus += 6
    elif rating >= 3:
        bonus += 2
    elif rating and rating <= 2:
        bonus -= 18
    bonus -= min(25, rejects * 8)
    if project and str(project) in info.get("projects", set()):
        bonus += 8
    bonus = max(-35, min(35, bonus))

    return {
        "bonus": bonus,
        "tier": tier,
        "usage_count": usage_count,
        "rating": rating,
        "reject_count": rejects,
        "same_project": bool(project and str(project) in info.get("projects", set())),
    }

# === 音色特征模板 ===
PROFILES = {
    "whoosh": {
        "tags": ["whoosh", "transition", "wind"],
        "desc": "快速空气流动声，平滑、干净",
        "crest_range": (1.5, 8),     # 中低瞬态
        "zcr_range": (0.01, 0.3),     # 中等噪声
        "flat_range": (0.001, 0.05),  # 偏 noise-like
        "dur_min": 0.2, "dur_max": 5,
        "category_weight": {"转场": 3, "魔法": 2, "运动": 1.5, "生活": 0.5},
        "bad": ["人声", "笑声", "动物", "综艺", "游戏", "手机", "电话", "盲音", "拨打", "挂断", "铃声", "打响指", "气泡", "弹出", "啵"],
    },
    "impact": {
        "tags": ["impact", "mechanical"],
        "desc": "瞬间撞击/敲击，短促有力",
        "crest_range": (6, 30),
        "zcr_range": (0.0, 0.2),
        "flat_range": (0.0, 0.01),
        "dur_min": 0.05, "dur_max": 2,
        "category_weight": {"机械": 3, "打斗": 2, "生活": 1, "蒸汽": 1.5},
        "bad": ["人声", "笑声", "动物", "BGM", "乐器", "音乐"],
    },
    "ambient": {
        "tags": ["ambient", "nature", "water", "wind"],
        "desc": "环境氛围底噪，长持续",
        "crest_range": (1, 6),
        "zcr_range": (0.0, 0.15),
        "flat_range": (0.0, 0.03),
        "dur_min": 2, "dur_max": 999,
        "category_weight": {"环境音": 3, "生活": 2, "紧张": 1},
        "bad": ["人声", "笑声", "综艺", "游戏", "魔法", "电话", "手机", "人群"],
    },
    "transition": {
        "tags": ["transition", "whoosh"],
        "desc": "转场过渡声",
        "crest_range": (1.5, 10),
        "zcr_range": (0.005, 0.25),
        "flat_range": (0.0, 0.05),
        "dur_min": 0.1, "dur_max": 3,
        "category_weight": {"转场": 3, "魔法": 2, "运动": 1},
        "bad": ["人声", "笑声", "动物", "综艺", "游戏", "电话", "盲音", "拨打", "挂断", "铃声", "打响指"],
    },
    "click": {
        "tags": ["click"],
        "desc": "短促按钮/提示音",
        "crest_range": (4, 25),
        "zcr_range": (0.0, 0.2),
        "flat_range": (0.0, 0.01),
        "dur_min": 0.01, "dur_max": 0.5,
        "category_weight": {"手机": 3, "机械": 2},
        "bad": ["人声", "笑声"],
    },
    "metal": {
        "tags": ["mechanical", "metal"],
        "desc": "金属碰撞/齿轮声",
        "crest_range": (3, 20),
        "zcr_range": (0.01, 0.3),
        "flat_range": (0.0, 0.02),
        "dur_min": 0.1, "dur_max": 5,
        "category_weight": {"机械": 3, "蒸汽": 3, "打斗": 1},
        "bad": ["人声", "笑声", "动物", "BGM"],
    },
    "stone": {
        "tags": ["mechanical", "metal", "impact", "stone", "石材", "岩石"],
        "desc": "石材/地质纹理，短促材质触感",
        "crest_range": (3, 25),
        "zcr_range": (0.0, 0.2),
        "flat_range": (0.0, 0.015),
        "dur_min": 0.05, "dur_max": 3,
        "category_weight": {"机械": 3, "蒸汽": 3, "金属": 3, "打斗": 1},
        "bad": ["人声", "笑声", "动物", "BGM", "枪", "gun", "blood", "血腥", "穿刺", "gore", "scream"],
    },
}

def in_range(val, lo, hi):
    if val is None: return 0.5
    if lo <= val <= hi: return 1.0
    dist = min(abs(val - lo), abs(val - hi))
    span = (hi - lo) or 0.01
    return max(0, 1.0 - dist / (span * 2))

def score(r, profile):
    """Score a file against a sonic profile (0-100)"""
    s = 0.0
    
    # Keyword tag bonus (strongest signal)
    tags = set(tags_of(r))
    for t in profile.get("tags", []):
        if t in tags:
            s += 30  # Big bonus for keyword match
    
    # Crest factor match
    cf = float(r.get("crest", 0))
    s += in_range(cf, *profile.get("crest_range", (0, 99))) * 20
    
    # ZCR match
    z = float(r.get("zcr", 0))
    s += in_range(z, *profile.get("zcr_range", (0, 99))) * 15
    
    # Flatness match
    fl = float(r.get("flatness", 0))
    s += in_range(fl, *profile.get("flat_range", (0, 99))) * 10
    
    # Duration bonus (closer to target = better)
    target = profile.get("duration_target", 1.0)
    dur = float(r.get("duration", 0))
    if dur > 0:
        dur_score = max(0, 1.0 - abs(dur - target) / max(target, dur))
        s += dur_score * 10
    
    # Category weight
    cat = r.get("category", "")
    for kw, w in profile.get("category_weight", {}).items():
        if kw in cat:
            s += w * 5
    
    # Duration bounds penalty
    if dur < profile.get("dur_min", 0) or dur > profile.get("dur_max", 999):
        s *= 0.3
    
    return min(100, round(s, 1))


def has_semantic_signal(row, profile):
    """精选不能让语义无关的素材跨类型插队。"""
    tags = {tag.lower() for tag in tags_of(row) if tag}
    if any(tag.lower() in tags for tag in profile.get("tags", [])):
        return True
    category = (row.get("category") or "").lower()
    return any(
        keyword.lower() in category and float(weight) >= 1.0
        for keyword, weight in profile.get("category_weight", {}).items()
    )

def query(profile_name, target_dur=None, top_k=10, project=None):
    """Query best matches for a sonic profile and apply bounded preference weights."""
    db = load()
    # Copy per request; the server handles requests concurrently.
    profile = dict(PROFILES.get(profile_name, PROFILES["transition"]))
    if target_dur:
        profile["duration_target"] = max(0.01, min(float(target_dur), 999.0))
    
    bad = profile.get("bad", [])
    scored = []
    for r in db:
        haystack = f"{r.get('category', '')} {r.get('name', '')}"
        if any(b in haystack for b in bad):
            continue
        base_score = score(r, profile)
        preference = preference_for(r, project=project)
        applied_bonus = preference["bonus"]
        if applied_bonus > 0 and not has_semantic_signal(r, profile):
            applied_bonus = min(3, applied_bonus)
        final_score = max(0, min(100, round(base_score + applied_bonus, 1)))
        enriched = dict(r)
        enriched.update({
            "base_score": base_score,
            "preference_bonus": applied_bonus,
            "raw_preference_bonus": preference["bonus"],
            "preference_tier": preference["tier"],
            "usage_count": preference["usage_count"],
            "average_rating": preference["rating"],
            "reject_count": preference["reject_count"],
            "same_project": preference["same_project"],
        })
        scored.append((final_score, enriched))
    scored.sort(key=lambda x: (
        -x[0],
        -float(x[1].get("preference_bonus", 0)),
        -float(x[1].get("base_score", 0)),
        str(x[1].get("path") or x[1].get("name") or ""),
    ))
    
    return [(s, r) for s, r in scored[:top_k] if s > 10]

if __name__ == "__main__":
    import sys
    profile = sys.argv[1] if len(sys.argv) > 1 else "whoosh"
    dur = float(sys.argv[2]) if len(sys.argv) > 2 else None
    project = sys.argv[3] if len(sys.argv) > 3 else None
    
    print(f"🔍 查询: {profile}" + (f" ~{dur}s" if dur else ""))
    print(f"   模板: {PROFILES.get(profile,{}).get('desc','?')}")
    print()
    
    matches = query(profile, dur, 15, project=project)
    for s, r in matches:
        tags = " ".join(tags_of(r))
        cats = (r.get("category", "") or "").split("/")[-1][:12]
        dur_s = float(r.get("duration", 0))
        pref = f"{r.get('preference_tier')} {r.get('preference_bonus'):+}"
        print(f"  [{s:>4}] {cats:<12} {r['name'][:38]:<38} {dur_s:.1f}s  {pref:<22} {'#'+tags if tags else ''}")
