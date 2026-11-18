"""Bounded host-side video derivatives for explicitly required mixed reviews.

This module never invokes a model or grants a reviewer tools. It prepares PNG
frames and selected media facts; copying an original video into a headless CLI
is not a substitute for this packet or proof that the model read the images.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import struct
import subprocess
import tempfile
import time

MAX_VIDEO_BYTES = 512 * 1024 * 1024
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_PROCESS_OUTPUT = 128 * 1024
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}


def freeze_video_review_request(conn, candidate, request):
    """Bind an explicit media request to the current combined-route artifact."""
    from .validation_basis import validate_basis
    if (not isinstance(request, dict) or set(request) != {"version", "source_video"}
            or type(request.get("version")) is not int or request["version"] != 1):
        raise ValueError("media review requires a version 1 source_video request")
    basis = validate_basis(conn, candidate)
    if basis.get("route") != "combined" or basis.get("status") == "revoked":
        raise ValueError("video review materials require a current combined validation basis")
    source = request["source_video"]
    expected = {key: basis["artifact"][key] for key in ("path", "sha256")}
    if not isinstance(source, dict) or source != expected:
        raise ValueError("media source does not match the current validation basis artifact")
    workspace = basis["real_task"]["workspace"]
    _, _, fd, _ = _video_file(source, workspace)
    os.close(fd)
    return {"version": 1, "route": "combined", "source_video": dict(source),
            "workspace": workspace, "basis_id": basis["basis_id"],
            "basis_sha256": basis["basis_sha256"], "model_read_verified": False}


def require_media_review_node(source, registry=None):
    """Require an actual material-staging entry plus declared vision support."""
    from .capabilities import CapabilityRegistry, agent_provider_model
    from .config import AgentConfig
    from .pool_dispatch import infer_agent_type
    if not isinstance(source, dict) or not isinstance(source.get("node"), dict):
        raise ValueError("missing_materials: review node identity is required; manual_review")
    node = source["node"]
    kind = infer_agent_type(node)
    if (node.get("interface") != "cli" or kind not in {"agy", "dsh", "qoder", "codebuddy", "codex", "claude"}
            or node.get("agy_mode") == "interactive"):
        raise ValueError("missing_materials: entry does not stage media; manual_review")
    agent = AgentConfig(str(source.get("reviewer_id") or "media-review"), "pool_cli", True, "review", 20,
                        None, {"pool_source": source, "driver": kind, "model": node.get("model")})
    provider, model = agent_provider_model(agent)
    if not (registry or CapabilityRegistry()).has_capability(provider, model, "vision"):
        raise ValueError("missing_materials: configured node vision capability is unknown or unavailable; manual_review")
    return {"entry_program": kind, "provider": provider, "model": model, "material_staging": True}


def _sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _check_open_video(path, fd, before, expected_sha256):
    if path.is_symlink() or path.resolve(strict=True) != path:
        raise ValueError('video path changed during preprocessing')
    current = path.stat(follow_symlinks=False)
    fields = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if fields(before) != fields(current) or fields(before) != fields(os.fstat(fd)):
        raise ValueError('video changed during preprocessing')
    value = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    for block in iter(lambda: os.read(fd, 1024 * 1024), b''):
        value.update(block)
    if value.hexdigest() != expected_sha256:
        raise ValueError('video hash changed')
    if fields(before) != fields(os.fstat(fd)) or fields(before) != fields(path.stat(follow_symlinks=False)):
        raise ValueError('video changed during preprocessing')
    os.lseek(fd, 0, os.SEEK_SET)


def _run_bounded(command, *, pass_fds=(), timeout_seconds=20):
    """Read small chunks and stop before either output stream can grow unbound."""
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, shell=False, pass_fds=pass_fds,
                               close_fds=True)
    deadline = time.monotonic() + timeout_seconds
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    try:
        for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            left = deadline - time.monotonic()
            if left <= 0:
                raise ValueError("media preprocessing timed out")
            for key, _ in selector.select(min(left, 0.2)):
                block = os.read(key.fd, 16384)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                buffers[key.data].extend(block)
                if sum(len(value) for value in buffers.values()) > MAX_PROCESS_OUTPUT:
                    raise ValueError("media preprocessing output exceeds limit")
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if process.returncode:
            raise ValueError("media preprocessing tool failed")
        return bytes(buffers["stdout"])
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        process.kill()
        process.wait(timeout=2)
        if isinstance(exc, ValueError):
            raise
        raise ValueError("media preprocessing failed or timed out") from exc
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            stream.close()


def _video_file(video_ref, workspace):
    if not isinstance(video_ref, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(video_ref.get("sha256", ""))):
        raise ValueError("video reference requires a SHA-256")
    root = Path(workspace)
    path = Path(str(video_ref.get("path") or ""))
    if (not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir()
            or not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path
            or not path.is_relative_to(root) or path.suffix.casefold() not in VIDEO_SUFFIXES):
        raise ValueError("video must be a regular authorized workspace file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_VIDEO_BYTES:
        os.close(fd)
        raise ValueError("video is empty, irregular or exceeds size limit")
    try:
        _check_open_video(path, fd, info, video_ref["sha256"])
    except BaseException:
        os.close(fd)
        raise
    return path, root, fd, info


def prepare_video_review_materials(video_ref, workspace, *, route, review_source=None, timeout_seconds=20):
    """Return only derivatives in material_files; pure artifact routes are no-ops.

    source=None permits preparation before node selection, but marks the result
    delivery_checked=False. Dispatch must still call require_media_review_node.
    Unsupported inputs produce a visible manual-review result, never a pass.
    """
    if route in {"real_task_acceptance", "sandbox"}:
        return {"status": "not_required", "route": route, "material_files": [], "model_read_verified": False}
    if route != "combined":
        return {"status": "missing_materials", "route": route, "manual_review": True,
                "reason": "unknown validation route", "material_files": [], "model_read_verified": False}
    fd = None
    package = None
    try:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 20:
            raise ValueError("media preprocessing deadline must be within 20 seconds")
        node = require_media_review_node(review_source) if review_source is not None else None
        tools = {name: shutil.which(name) for name in ("ffprobe", "ffmpeg")}
        if not all(tools.values()):
            raise ValueError("ffprobe and ffmpeg are required for controlled video preprocessing")
        path, root, fd, before = _video_file(video_ref, workspace)
        started = time.monotonic()
        input_path = "/dev/fd/" + str(fd)
        raw = _run_bounded([tools["ffprobe"], "-v", "error", "-protocol_whitelist", "file,pipe",
                            "-show_entries", "format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate",
                            "-of", "json", input_path], pass_fds=(fd,), timeout_seconds=timeout_seconds)
        data = json.loads(raw)
        streams = data.get("streams") if isinstance(data, dict) else None
        video = next((s for s in streams or [] if isinstance(s, dict) and s.get("codec_type") == "video"), None)
        duration = float(data.get("format", {}).get("duration", 0))
        if (not video or not math.isfinite(duration) or not 0 < duration <= 86400
                or type(video.get("width")) is not int or type(video.get("height")) is not int
                or not 0 < video["width"] <= 32768 or not 0 < video["height"] <= 32768):
            raise ValueError("video has no bounded dimensions/duration")
        parent = root / ".xiaoba-media-review"
        if parent.is_symlink():
            raise ValueError("media packet directory cannot be a symlink")
        parent.mkdir(mode=0o700, exist_ok=True)
        package = Path(tempfile.mkdtemp(prefix="packet-", dir=parent))
        frames = []
        for index, fraction in enumerate((0.0, 1 / 3, 2 / 3, 0.95), 1):
            timestamp = round(duration * fraction, 6)
            frame = package / ("frame-%02d.png" % index)
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise ValueError("media preprocessing timed out")
            os.lseek(fd, 0, os.SEEK_SET)
            _run_bounded([tools["ffmpeg"], "-nostdin", "-hide_banner", "-loglevel", "error",
                          "-protocol_whitelist", "file,pipe", "-ss", str(timestamp), "-i", input_path,
                          "-map", "0:v:0", "-frames:v", "1", "-threads", "1", "-vf",
                          "scale=1280:1280:force_original_aspect_ratio=decrease", "-fs", str(MAX_FRAME_BYTES),
                          "-update", "1", str(frame)], pass_fds=(fd,), timeout_seconds=remaining)
            if frame.is_symlink() or not frame.is_file() or not 24 <= frame.stat().st_size <= MAX_FRAME_BYTES:
                raise ValueError("video frame is missing or exceeds size limit")
            with frame.open("rb") as stream:
                header = stream.read(24)
            dimensions = struct.unpack(">II", header[16:24])
            if (header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR"
                    or any(not 0 < value <= 1280 for value in dimensions)):
                raise ValueError("video derivative is not a PNG frame")
            os.chmod(frame, 0o600)
            frames.append({"path": str(frame), "sha256": _sha(frame), "timestamp_seconds": timestamp})
        _check_open_video(path, fd, before, video_ref["sha256"])
        metadata = {"version": 1, "route": route, "source_video": dict(video_ref), "duration_seconds": duration,
                    "video": {k: video[k] for k in ("codec_name", "width", "height", "r_frame_rate") if k in video},
                    "frames": frames, "sampling": "four deterministic timestamps; not exhaustive video coverage",
                    "model_read_verified": False}
        metadata_path = package / "media-parameters.json"
        encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2).encode()
        if len(encoded) > MAX_PROCESS_OUTPUT:
            raise ValueError("media metadata exceeds size limit")
        metadata_path.write_bytes(encoded)
        os.chmod(metadata_path, 0o600)
        ref = {"path": str(metadata_path), "sha256": _sha(metadata_path)}
        return {"status": "ready", "route": route, "source_video": dict(video_ref), "package_dir": str(package),
                "frames": frames, "metadata": ref, "material_files": [
                    {"path": f["path"], "sha256": f["sha256"]} for f in frames] + [ref],
                "delivery_checked": node is not None, "review_node": node, "model_read_verified": False}
    except (OSError, ValueError, TypeError, KeyError, StopIteration) as exc:
        return {"status": "missing_materials", "route": route, "manual_review": True, "material_files": [],
                "reason": str(exc), "package_dir": str(package) if package else None, "model_read_verified": False}
    finally:
        if fd is not None:
            os.close(fd)
