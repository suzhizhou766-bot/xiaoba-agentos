#!/usr/bin/env python3
"""Create a deterministic CSV manifest without modifying project files."""
import csv, hashlib, sys
from datetime import datetime
from pathlib import Path

def category(path):
    name = path.name.lower()
    suffix = path.suffix.lower()
    if "原文" in path.name or "文案" in path.name: return "文案"
    if suffix in {".wav", ".aiff", ".mp3", ".m4a", ".mp4"}: return "音视频"
    if suffix in {".xlsx", ".csv"}: return "表格"
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}: return "图片"
    if suffix in {".py", ".mjs", ".swift", ".sh"}: return "脚本"
    if "preview" in name or "inspect" in name: return "预览/检查"
    return "其他"

def row(root, path):
    stat = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16] if stat.st_size <= 50_000_000 else "SKIPPED_LARGE"
    return [str(path.relative_to(root)), category(path), stat.st_size, datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"), path.suffix.lower(), digest]

if __name__ == "__main__":
    if len(sys.argv) not in (2, 3): raise SystemExit("usage: asset_manifest.py PROJECT_DIR [OUT.csv]")
    root = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]) if len(sys.argv) == 3 else root / "asset_manifest.csv"
    files = sorted(p for p in root.rglob("*") if p.is_file() and p != out and ".git" not in p.parts and "node_modules" not in p.parts)
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["相对路径", "类别", "大小字节", "修改时间", "扩展名", "SHA256前16位"])
        writer.writerows(row(root, path) for path in files)
    print(f"manifest={out}\nfiles={len(files)}")
