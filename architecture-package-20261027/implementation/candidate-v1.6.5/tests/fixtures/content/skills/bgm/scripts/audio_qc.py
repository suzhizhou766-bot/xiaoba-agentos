#!/usr/bin/env python3
"""Inspect PCM WAV delivery properties, peak level, and full-scale clipping."""

import json
import math
import sys
import wave
from pathlib import Path


def _samples(raw, width):
    if width == 1:
        for value in raw:
            yield value - 128
        return
    for offset in range(0, len(raw) - width + 1, width):
        chunk = raw[offset : offset + width]
        yield int.from_bytes(chunk, byteorder="little", signed=True)


def inspect(path):
    file = Path(path)
    if not file.is_file() or file.stat().st_size == 0:
        return {"path": str(file), "status": "FAIL", "reason": "missing or empty"}

    try:
        with wave.open(str(file), "rb") as audio:
            frames = audio.getnframes()
            rate = audio.getframerate()
            channels = audio.getnchannels()
            width = audio.getsampwidth()
            compression = audio.getcomptype()
            duration = frames / rate if rate else 0
            issues = []

            if compression != "NONE":
                issues.append(f"compression={compression}")
            if rate != 48000:
                issues.append(f"sample_rate={rate}")
            if channels != 2:
                issues.append(f"channels={channels}")
            if width < 2:
                issues.append(f"bit_depth={width * 8}")
            if width not in (1, 2, 3, 4):
                issues.append(f"unsupported_sample_width={width}")
            if duration <= 0:
                issues.append("duration<=0")

            peak = 0
            clipped = 0
            sample_count = 0
            if compression == "NONE" and width in (1, 2, 3, 4):
                full_scale = (1 << (width * 8 - 1)) - 1
                while True:
                    raw = audio.readframes(8192)
                    if not raw:
                        break
                    for sample in _samples(raw, width):
                        magnitude = abs(sample)
                        peak = max(peak, magnitude)
                        clipped += magnitude >= full_scale
                        sample_count += 1
                if clipped:
                    issues.append(f"full_scale_samples={clipped}")
                peak_dbfs = (
                    round(20 * math.log10(peak / full_scale), 3)
                    if peak and full_scale
                    else None
                )
            else:
                peak_dbfs = None

            return {
                "path": str(file),
                "status": "PASS" if not issues else "WARN",
                "duration": round(duration, 3),
                "sample_rate": rate,
                "channels": channels,
                "bit_depth": width * 8,
                "compression": compression,
                "peak_dbfs": peak_dbfs,
                "full_scale_samples": clipped,
                "sample_count": sample_count,
                "issues": issues,
            }
    except (wave.Error, EOFError, OSError) as error:
        return {"path": str(file), "status": "FAIL", "reason": f"decode: {error}"}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: audio_qc.py file.wav [...]")
    for item in sys.argv[1:]:
        print(json.dumps(inspect(item), ensure_ascii=False))
