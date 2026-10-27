#!/usr/bin/env python3
"""同步本地 SFX 资产索引和精选候选清单。

不移动、删除或重命名音频；现有人工字段会被保留。
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from sfx_match import LIBRARY, DB, load_preferences

INDEX = LIBRARY / "音效素材库索引.csv"
CANDIDATES = LIBRARY / "音效精选候选.csv"
SUPPORTED_EXTENSIONS = {".wav", ".mp3", ".aiff", ".aif", ".flac", ".m4a", ".ogg", ".aac"}
MANAGED_FOLDERS = {"精选音效", "ElevenLabs精选音效", "项目专用音效"}
FIELDS = [
    "文件名", "相对路径", "来源", "分类", "用途场景", "情绪/质感", "强度", "时长", "格式",
    "是否精选", "使用项目", "推荐级别", "评分", "使用次数", "否定次数", "最近使用",
    "生成日期", "生成提示词", "授权备注", "SHA256", "备注",
]


def read_csv(path, key):
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row.get(key, ""): row for row in csv.DictReader(handle) if row.get(key)}


def read_sidecar(audio):
    sidecar = audio.with_name(audio.name + ".sfx.json")
    if not sidecar.is_file():
        return {}
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"note": "sidecar_invalid"}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_for(relative, sidecar):
    if sidecar.get("source"):
        return str(sidecar["source"])
    top = relative.parts[0] if relative.parts else ""
    if top == "ElevenLabs精选音效" or "ElevenLabs" in relative.parts:
        return "ElevenLabs网页"
    if top == "MAPLE音效库":
        return "MAPLE音效库"
    if top == "精选音效":
        return "本地精选"
    if top == "项目专用音效":
        return "项目专用"
    return top or "本地素材"


def tier_for(curated, usage_count, rating, rejects):
    if curated:
        return "精选"
    if usage_count >= 3 or rating >= 4:
        return "精选候选"
    if usage_count >= 1 or rating >= 3:
        return "常用"
    if rejects or (rating and rating <= 2):
        return "冷备"
    return "未评"


def sync(dry_run=False, hash_all=False):
    existing = read_csv(INDEX, "相对路径")
    fingerprints = read_csv(DB, "path")
    preferences = load_preferences()
    files = sorted(
        path for path in LIBRARY.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    rows = []
    hashed = 0
    for audio in files:
        relative = audio.relative_to(LIBRARY)
        relative_text = str(relative)
        old = existing.get(relative_text, {})
        fingerprint = fingerprints.get(str(audio), {})
        sidecar = read_sidecar(audio)
        preference = preferences.get(str(audio), {})
        top = relative.parts[0] if relative.parts else ""
        curated = top in {"精选音效", "ElevenLabs精选音效"} or str(old.get("是否精选", "")).lower() in {"是", "yes", "true", "1"}
        usage_count = int(preference.get("usage_count", 0) or 0)
        rating = float(preference.get("rating", 0) or 0)
        rejects = int(preference.get("reject_count", 0) or 0)
        projects = sorted(preference.get("projects", set()) or set())
        project = sidecar.get("project")
        if project and project not in projects:
            projects.append(str(project))
        keywords = [item for item in (fingerprint.get("keywords") or "").split("|") if item]
        digest = old.get("SHA256", "")
        if not digest and (hash_all or top in MANAGED_FOLDERS):
            digest = sha256(audio)
            hashed += 1
        derived = {
            "文件名": audio.name,
            "相对路径": relative_text,
            "来源": source_for(relative, sidecar),
            "分类": sidecar.get("category") or fingerprint.get("profile_1") or "待指纹",
            "用途场景": sidecar.get("action") or "|".join(keywords[:5]),
            "情绪/质感": sidecar.get("texture", ""),
            "强度": sidecar.get("intensity", ""),
            "时长": fingerprint.get("duration", ""),
            "格式": audio.suffix.lower().lstrip("."),
            "是否精选": "是" if curated else "否",
            "使用项目": "|".join(sorted(set(projects))),
            "推荐级别": tier_for(curated, usage_count, rating, rejects),
            "评分": f"{rating:.2f}" if rating else "",
            "使用次数": str(usage_count),
            "否定次数": str(rejects),
            "最近使用": preference.get("last_used", ""),
            "生成日期": sidecar.get("generated_at", ""),
            "生成提示词": sidecar.get("prompt", ""),
            "授权备注": sidecar.get("license_note", ""),
            "SHA256": digest,
            "备注": sidecar.get("note", ""),
        }
        # 人工填写的语义和授权字段优先，自动统计字段以现场日志为准。
        for field in ("来源", "分类", "用途场景", "情绪/质感", "强度", "生成日期", "生成提示词", "授权备注", "备注"):
            if old.get(field) and not sidecar.get({
                "来源": "source", "分类": "category", "用途场景": "action",
                "情绪/质感": "texture", "强度": "intensity", "生成日期": "generated_at",
                "生成提示词": "prompt", "授权备注": "license_note", "备注": "note",
            }[field]):
                derived[field] = old[field]
        rows.append(derived)

    candidates = [row for row in rows if row["推荐级别"] == "精选候选"]
    if not dry_run:
        for path, payload in ((INDEX, rows), (CANDIDATES, candidates)):
            temporary = path.with_suffix(path.suffix + ".tmp")
            with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(payload)
            temporary.replace(path)
    return {
        "assets": len(rows),
        "selected": sum(row["推荐级别"] == "精选" for row in rows),
        "common": sum(row["推荐级别"] == "常用" for row in rows),
        "candidates": len(candidates),
        "cold": sum(row["推荐级别"] == "冷备" for row in rows),
        "hashed": hashed,
        "dry_run": dry_run,
    }


def main():
    parser = argparse.ArgumentParser(description="同步 SFX 资产索引，不移动或删除音频")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--hash-all", action="store_true", help="为全库计算 SHA256；默认只计算受管理目录")
    args = parser.parse_args()
    print(json.dumps(sync(dry_run=args.dry_run, hash_all=args.hash_all), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
