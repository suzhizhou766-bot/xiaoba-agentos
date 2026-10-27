#!/usr/bin/env python3
"""Inspect PCM WAV delivery properties, peak level, and full-scale clipping."""

import json
import math
import struct
import sys
from pathlib import Path

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
# KSDATAFORMAT_SUBTYPE_* GUIDs share this tail after the 2-byte format code
# and two zero bytes (00000000-0010-0080-00AA-00389B71 in little-endian form).
_SUBFORMAT_TAIL = b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"


class WavError(Exception):
    pass


class _WavReader:
    """Minimal RIFF/WAVE reader: fmt chunk (incl. WAVE_FORMAT_EXTENSIBLE) and data."""

    def __init__(self, handle):
        self._handle = handle
        self._handle.seek(0, 2)
        file_size = self._handle.tell()
        self._handle.seek(0)
        header = self._handle.read(12)
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise WavError("file does not start with RIFF/WAVE header")
        fmt = None
        self._data_start = None
        self._data_size = 0
        while True:
            chunk = self._handle.read(8)
            if len(chunk) < 8:
                break
            name, size = chunk[:4], struct.unpack("<I", chunk[4:])[0]
            start = self._handle.tell()
            if name == b"fmt ":
                fmt = self._handle.read(size)
            elif name == b"data":
                self._data_start = start
                # Streamed writers may leave 0 or 0xFFFFFFFF here; trust the file.
                remaining = file_size - start
                self._data_size = remaining if size == 0 or size > remaining else size
                if fmt is not None:
                    break
            self._handle.seek(start + size + (size & 1))
        if fmt is None:
            raise WavError("fmt chunk not found")
        if self._data_start is None:
            raise WavError("data chunk not found")
        if len(fmt) < 16:
            raise WavError("fmt chunk too short")
        tag, channels, rate, _, block_align, bits = struct.unpack("<HHIIHH", fmt[:16])
        if tag == WAVE_FORMAT_EXTENSIBLE:
            if len(fmt) < 40:
                raise WavError("WAVE_FORMAT_EXTENSIBLE fmt chunk too short")
            subformat = fmt[24:40]
            if subformat[2:] != _SUBFORMAT_TAIL:
                raise WavError("unknown WAVE_FORMAT_EXTENSIBLE subformat")
            tag = struct.unpack("<H", subformat[:2])[0]
        if channels == 0 or block_align == 0 or block_align % channels:
            raise WavError(f"invalid block_align={block_align} channels={channels}")
        self.format_tag = tag
        self.channels = channels
        self.rate = rate
        self.bits = bits
        self.width = block_align // channels
        self.block_align = block_align
        self.frames = self._data_size // block_align
        self._left = self.frames * block_align
        self._handle.seek(self._data_start)

    def readframes(self, count):
        size = min(count * self.block_align, self._left)
        raw = self._handle.read(size)
        self._left -= len(raw)
        return raw


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
        with open(file, "rb") as handle:
            audio = _WavReader(handle)
            frames = audio.frames
            rate = audio.rate
            channels = audio.channels
            width = audio.width
            compression = (
                "NONE"
                if audio.format_tag == WAVE_FORMAT_PCM
                else f"format_tag={audio.format_tag}"
            )
            duration = frames / rate if rate else 0
            issues = []

            if compression != "NONE":
                issues.append(f"compression={compression}")
            if rate != 48000:
                issues.append(f"sample_rate={rate}")
            if channels != 2:
                issues.append(f"channels={channels}")
            if audio.bits < 16:
                issues.append(f"bit_depth={audio.bits}")
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
                "bit_depth": audio.bits,
                "compression": compression,
                "peak_dbfs": peak_dbfs,
                "full_scale_samples": clipped,
                "sample_count": sample_count,
                "issues": issues,
            }
    except (WavError, struct.error, EOFError, OSError) as error:
        return {"path": str(file), "status": "FAIL", "reason": f"decode: {error}"}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: audio_qc.py file.wav [...]")
    for item in sys.argv[1:]:
        print(json.dumps(inspect(item), ensure_ascii=False))
