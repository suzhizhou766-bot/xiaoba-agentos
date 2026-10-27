#!/usr/bin/env python3
"""Machine-checkable video/audio content preflight for the delivery pipeline."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def number(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def probe(path: Path, ffprobe: str) -> dict:
    result = run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    return json.loads(result.stdout)


def detect_video(path: Path, ffmpeg: str, filter_name: str) -> list[dict]:
    if filter_name == "blackdetect":
        # The compositions use a deliberately near-black (#05090b) background.
        # A 10% threshold reports that designed background as a false black frame.
        vf = "blackdetect=d=0.75:pix_th=0.03"
        pattern = re.compile(r"black_start:(?P<start>[0-9.]+).*?black_end:(?P<end>[0-9.]+).*?black_duration:(?P<duration>[0-9.]+)")
    else:
        vf = "freezedetect=n=-60dB:d=1"
        pattern = re.compile(r"freeze_(?:start|end|duration):(?P<value>[0-9.]+)")
    result = run([ffmpeg, "-hide_banner", "-i", str(path), "-vf", vf, "-an", "-f", "null", "-"], timeout=180)
    text = result.stderr
    findings = []
    if filter_name == "blackdetect":
        for match in pattern.finditer(text):
            findings.append({"start": float(match.group("start")), "end": float(match.group("end")), "duration": float(match.group("duration"))})
    else:
        for match in pattern.finditer(text):
            findings.append({"value": float(match.group("value"))})
    return findings


def audio_level(path: Path, ffmpeg: str) -> dict:
    result = run([ffmpeg, "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"], timeout=120)
    values = {}
    for key in ("mean_volume", "max_volume"):
        match = re.search(rf"{key}:\s*(-?[0-9.]+) dB", result.stderr)
        if match:
            values[key] = float(match.group(1))
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video")
    parser.add_argument("--output")
    parser.add_argument("--expect-audio", choices=("0", "1"), default="0")
    args = parser.parse_args()
    path = Path(args.video).resolve()
    runtime_bin = Path(__file__).resolve().parents[2] / ".runtime" / "bin"
    ffprobe_candidate = runtime_bin / "ffprobe"
    ffmpeg_candidate = runtime_bin / "ffmpeg"
    ffprobe = str(ffprobe_candidate) if ffprobe_candidate.is_file() else None
    ffmpeg = str(ffmpeg_candidate) if ffmpeg_candidate.is_file() else None
    if not path.is_file() or path.stat().st_size == 0:
        print(f"QC FAIL: missing or empty video: {path}", file=sys.stderr)
        return 1
    if not ffprobe or not ffmpeg:
        print("QC FAIL: ffmpeg/ffprobe not found in unified PATH", file=sys.stderr)
        return 1

    failures: list[str] = []
    warnings: list[str] = []
    try:
        data = probe(path, ffprobe)
    except Exception as exc:  # noqa: BLE001
        print(f"QC FAIL: {exc}", file=sys.stderr)
        return 1
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = number((data.get("format") or {}).get("duration"))
    if not video:
        failures.append("缺少视频流")
    if duration is None or duration <= 0:
        failures.append("视频时长无效")
    elif duration < 0.5:
        failures.append("视频时长小于 0.5 秒")
    if video:
        if not video.get("width") or not video.get("height"):
            failures.append("视频尺寸无效")
        try:
            black = detect_video(path, ffmpeg, "blackdetect")
            if black:
                warnings.append(f"检测到 {len(black)} 段连续黑帧，最长 {max(item['duration'] for item in black):.2f}s")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"黑帧检测未完成: {exc}")
        try:
            freeze = detect_video(path, ffmpeg, "freezedetect")
            if freeze:
                warnings.append(f"检测到静帧区间（{len(freeze)} 个事件），请结合创意确认")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"静帧检测未完成: {exc}")

    expected_audio = args.expect_audio == "1"
    if expected_audio and not audio:
        failures.append("要求有音轨但成片没有音频流")
    if audio:
        audio_duration = number(audio.get("duration")) or duration
        if duration and audio_duration and abs(duration - audio_duration) > 0.15:
            failures.append(f"音视频时长不匹配: video={duration:.3f}s audio={audio_duration:.3f}s")
        if expected_audio and str(audio.get("sample_rate")) != "48000":
            failures.append(f"音轨采样率不是 48kHz: {audio.get('sample_rate')}")
        if expected_audio and number(audio.get("channels")) != 2:
            failures.append(f"音轨不是双声道: {audio.get('channels')}")
        try:
            levels = audio_level(path, ffmpeg)
            if levels.get("max_volume") is not None and levels["max_volume"] > -0.1:
                failures.append(f"音轨峰值可能削波: {levels['max_volume']:.1f} dB")
            if levels.get("mean_volume") is not None and levels["mean_volume"] < -60:
                warnings.append(f"音轨平均响度很低: {levels['mean_volume']:.1f} dB")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"音频电平检测未完成: {exc}")

    report = {
        "schema_version": 1,
        "status": "FAIL" if failures else "PASS",
        "video": {"path": str(path), "duration": duration, "codec": video.get("codec_name") if video else None, "width": video.get("width") if video else None, "height": video.get("height") if video else None},
        "audio": {"present": bool(audio), "duration": number(audio.get("duration")) if audio else None, "codec": audio.get("codec_name") if audio else None, "sample_rate": audio.get("sample_rate") if audio else None, "channels": audio.get("channels") if audio else None},
        "expected_audio": expected_audio,
        "failures": failures,
        "warnings": warnings,
    }
    if args.output:
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
