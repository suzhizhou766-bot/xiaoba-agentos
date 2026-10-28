#!/usr/bin/env python3
"""Validate a storyboard CSV against the project-director schema."""
import csv, sys
from pathlib import Path

REQUIRED = ["镜号", "时间段", "时长", "叙事任务", "画面内容", "音乐节拍", "补拍备选", "注意事项"]

def main(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit("ERROR: no storyboard rows")
    missing = [key for key in REQUIRED if key not in rows[0]]
    if missing:
        raise SystemExit("ERROR: missing columns: " + ", ".join(missing))
    failures = []
    for number, row in enumerate(rows, 2):
        for key in REQUIRED:
            if not row.get(key, "").strip():
                failures.append(f"row {number}: empty {key}")
    print(f"rows={len(rows)}")
    if failures:
        print("\n".join(failures))
        raise SystemExit(1)
    print("storyboard=PASS")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_storyboard.py storyboard.csv")
    main(sys.argv[1])
