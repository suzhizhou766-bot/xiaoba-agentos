#!/usr/bin/env python3
"""Deterministic delivery specification check for Remotion outputs."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

def probe(path: Path, ffprobe: str) -> dict:
    result = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], capture_output=True, text=True, check=False)
    if result.returncode: raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    return json.loads(result.stdout)

def fps(stream: dict) -> float | None:
    value = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    try:
        a, b = value.split("/")
        return float(a) / float(b) if float(b) else None
    except (AttributeError, ValueError, ZeroDivisionError): return None

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expect-audio", choices=("0", "1"), default="0")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--min-duration", type=float, default=0.5)
    args = parser.parse_args()
    ffprobe = Path(__file__).resolve().parents[2] / ".runtime/bin/ffprobe"
    failures, warnings = [], []
    try: data = probe(args.video, str(ffprobe))
    except Exception as exc:
        payload = {"schema_version": 2, "status": "FAIL", "failures": [str(exc)], "warnings": []}
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}
    duration = float(fmt.get("duration")) if fmt.get("duration") else None
    actual_fps = fps(video) if video else None
    if not video: failures.append("缺少视频流")
    else:
        if video.get("width") != args.width or video.get("height") != args.height: failures.append(f"分辨率不符: {video.get('width')}x{video.get('height')} != {args.width}x{args.height}")
        if actual_fps is None or abs(actual_fps - args.fps) > 0.1: failures.append(f"帧率不符: {actual_fps} != {args.fps}")
        if video.get("codec_name") not in {"h264", "hevc"}: failures.append(f"视频编码不支持: {video.get('codec_name')}")
        if video.get("pix_fmt") not in {"yuv420p", "yuvj420p"}: warnings.append(f"像素格式需确认: {video.get('pix_fmt')}")
    if duration is None or duration < args.min_duration: failures.append(f"时长无效: {duration}")
    if args.expect_audio == "1" and not audio: failures.append("要求音频但缺少音频流")
    if audio:
        if audio.get("sample_rate") != "48000": failures.append(f"音频采样率不符: {audio.get('sample_rate')}")
        if int(audio.get("channels") or 0) != 2: failures.append(f"音频声道不符: {audio.get('channels')}")
        if audio.get("codec_name") not in {"aac", "pcm_s16le", "pcm_s24le"}: failures.append(f"音频编码不支持: {audio.get('codec_name')}")
    payload = {
        "schema_version": 2, "status": "FAIL" if failures else "PASS", "failures": failures, "warnings": warnings,
        "spec": {"width": args.width, "height": args.height, "fps": args.fps, "video_codec": ["h264", "hevc"], "audio_sample_rate": 48000, "audio_channels": 2, "min_duration": args.min_duration},
        "actual": {"duration": duration, "size_bytes": fmt.get("size"), "video": {"codec": video.get("codec_name") if video else None, "width": video.get("width") if video else None, "height": video.get("height") if video else None, "fps": actual_fps, "pix_fmt": video.get("pix_fmt") if video else None, "bit_rate": video.get("bit_rate") if video else None}, "audio": {"present": bool(audio), "codec": audio.get("codec_name") if audio else None, "sample_rate": audio.get("sample_rate") if audio else None, "channels": audio.get("channels") if audio else None, "bit_rate": audio.get("bit_rate") if audio else None}},
        "source": str(args.video),
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 1 if failures else 0

if __name__ == "__main__": raise SystemExit(main())
