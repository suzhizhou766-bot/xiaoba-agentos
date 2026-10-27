#!/usr/bin/env python3
"""Register a downloaded ElevenLabs SFX into the project-specific local library."""
import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from sfx_match import LIBRARY
from sfx_usage_log import log_adopt, log_feedback

INDEX = LIBRARY / "音效素材库索引.csv"
SCRIPT_DIR = Path(__file__).resolve().parent
SUPPORTED_EXTENSIONS = {".wav", ".mp3", ".aiff", ".aif", ".flac", ".m4a", ".ogg", ".aac"}


def slug(value):
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", str(value).strip())
    return value.strip("_") or "unspecified"


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def duration(path):
    ffprobe = Path("/Users/suzhizhou/Documents/私人助理/.runtime/bin/ffprobe")
    result = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def existing_hashes():
    if not INDEX.is_file():
        return {}
    with INDEX.open(encoding="utf-8-sig", newline="") as handle:
        return {row.get("SHA256"): row for row in csv.DictReader(handle) if row.get("SHA256")}


def main():
    parser = argparse.ArgumentParser(description="登记 ElevenLabs 网页下载的项目专用音效")
    parser.add_argument("file", type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--category", required=True)
    parser.add_argument("--action", required=True)
    parser.add_argument("--material", default="clean")
    parser.add_argument("--intensity", default="medium")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--license-note", required=True, help="记录生成时的套餐/用途限制，不记录账号身份")
    parser.add_argument("--variant", type=int, default=1)
    parser.add_argument("--rating", type=float)
    parser.add_argument("--adopted", action="store_true")
    parser.add_argument("--no-refresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="只显示目标和元数据，不复制或写入")
    args = parser.parse_args()

    source = args.file.expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise SystemExit("输入必须是可读的音频文件")
    if args.rating is not None and not 1 <= args.rating <= 5:
        raise SystemExit("rating 必须介于 1 和 5")

    source_hash = digest(source)
    duplicate = existing_hashes().get(source_hash)
    if duplicate:
        print(json.dumps({"status": "duplicate", "existing": duplicate.get("相对路径")}, ensure_ascii=False))
        return

    length = duration(source)
    stem = "_".join([
        slug(args.category), slug(args.action), slug(args.material), slug(args.intensity),
        f"{length:.1f}s", "elevenlabs", f"v{max(1, args.variant):02d}",
    ])
    destination_dir = LIBRARY / "项目专用音效" / slug(args.project) / "ElevenLabs"
    destination = destination_dir / f"{stem}{source.suffix.lower()}"
    counter = 2
    while destination.exists() and digest(destination) != source_hash:
        destination = destination_dir / f"{stem}_{counter}{source.suffix.lower()}"
        counter += 1
    if args.dry_run:
        print(json.dumps({
            "status": "dry_run",
            "source": str(source),
            "destination": str(destination),
            "duration": round(length, 3),
            "sha256": source_hash,
            "would_refresh": not args.no_refresh,
        }, ensure_ascii=False, indent=2))
        return
    destination_dir.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copy2(source, destination)

    sidecar = destination.with_name(destination.name + ".sfx.json")
    metadata = {
        "schema_version": 1,
        "source": "ElevenLabs网页",
        "project": args.project,
        "category": args.category,
        "action": args.action,
        "material": args.material,
        "texture": args.material,
        "intensity": args.intensity,
        "duration": round(length, 3),
        "prompt": args.prompt,
        "license_note": args.license_note,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sha256": source_hash,
        "adopted": args.adopted,
    }
    sidecar.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.adopted:
        log_adopt(args.category, args.prompt, [(100, {"name": destination.name, "path": str(destination)})], project=args.project)
    if args.rating is not None:
        log_feedback(None, str(destination), args.rating, project=args.project, note="ElevenLabs generated SFX")

    if not args.no_refresh:
        subprocess.run([sys.executable, str(SCRIPT_DIR / "scan_classify_v3.py")], check=True)
        subprocess.run([sys.executable, str(SCRIPT_DIR / "sfx_catalog.py")], check=True)

    print(json.dumps({
        "status": "registered",
        "audio": str(destination),
        "metadata": str(sidecar),
        "sha256": source_hash,
        "refreshed": not args.no_refresh,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
