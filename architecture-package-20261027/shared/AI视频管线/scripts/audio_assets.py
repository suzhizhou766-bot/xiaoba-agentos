#!/usr/bin/env python3
"""Create auditable BGM/SFX stems, a timeline CSV and a structure report."""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

def run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode: raise SystemExit(result.stderr.strip() or "ffmpeg failed")

def duration(path: Path) -> float | None:
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True, check=False)
    try: return float(result.stdout.strip())
    except ValueError: return None

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bgm", type=Path)
    parser.add_argument("--sfx", type=Path)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--delivery", type=Path, required=True)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True); args.delivery.mkdir(parents=True, exist_ok=True)
    rows = []
    stems = []
    for label, source in (("BGM", args.bgm), ("SFX", args.sfx)):
        if not source: continue
        output = args.work / f"{label.lower()}_48k.wav"
        run(["ffmpeg", "-y", "-i", str(source), "-af", "loudnorm=I=-16:LRA=11:TP=-1.5,apad", "-t", str(args.duration), "-ar", "48000", "-ac", "2", str(output)])
        target = args.delivery / f"{label}_单轨_48k.wav"
        run(["cp", str(output), str(target)])
        stems.append(output)
        rows.append({"track": label, "source": str(source), "start_seconds": 0.0, "end_seconds": args.duration, "source_duration": duration(source), "normalized": True})
    premix = args.work / "premix_48k.wav"
    if stems:
        inputs = []
        for stem in stems: inputs += ["-i", str(stem)]
        run(["ffmpeg", "-y", *inputs, "-filter_complex", f"amix=inputs={len(stems)}:duration=longest:normalize=0,loudnorm=I=-16:LRA=11:TP=-1.5", "-t", str(args.duration), "-ar", "48000", "-ac", "2", str(premix)])
        run(["cp", str(premix), str(args.delivery / "BGM_SFX_预混总轨_48k.wav")])
    with (args.delivery / "SFX时间轴.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["track", "source", "start_seconds", "end_seconds", "source_duration", "normalized"]); writer.writeheader(); writer.writerows(rows)
    report = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "video_duration": args.duration, "tracks": rows, "premix": str(args.delivery / "BGM_SFX_预混总轨_48k.wav") if stems else None, "status": "PASS" if stems else "NOT_REQUESTED"}
    (args.delivery / "BGM结构报告.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0

if __name__ == "__main__": raise SystemExit(main())
