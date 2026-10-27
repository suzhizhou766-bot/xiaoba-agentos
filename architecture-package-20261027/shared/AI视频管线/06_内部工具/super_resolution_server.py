#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小八提示词导演台 · 本地图片超分服务。

启动：.runtime/venvs/superres/bin/python super_resolution_server.py
健康：GET  http://127.0.0.1:8892/healthz
调用：POST http://127.0.0.1:8892/upscale
      {"image_b64":"data:image/jpeg;base64,...", "target":"2k|4k|8k"}

使用官方 Real-ESRGAN x4 权重进行 AI 细节恢复，再按 2K / 4K / 8K 输出。
模型不可用时才降级为 Pillow Lanczos，保证工具不会中断。
"""

import base64
import io
import json
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageFilter, ImageOps

PORT = 8892
TARGET_LONG_EDGE = {"2k": 2048, "4k": 4096, "8k": 7680}
MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_REQUEST_BYTES = 68 * 1024 * 1024
MODEL_PATH = Path(__file__).parent / "models" / "realesr-general-x4v3.pth"
_engine = None
_engine_error = None
_engine_lock = threading.Lock()
_inference_lock = threading.Lock()


def decode_image(value):
    if not isinstance(value, str) or not value:
        raise ValueError("缺少图片数据")
    if value.startswith("data:"):
        value = value.split(",", 1)[1]
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise ValueError("图片 base64 格式无效") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("图片超过 50MB")
    image = Image.open(io.BytesIO(raw))
    return ImageOps.exif_transpose(image)


def get_engine():
    global _engine, _engine_error
    with _engine_lock:
        if _engine is not None or _engine_error is not None:
            return _engine
        try:
            if not MODEL_PATH.exists():
                raise RuntimeError("未找到 Real-ESRGAN 权重")
            from real_esrgan_engine import RealESRGAN
            _engine = RealESRGAN(MODEL_PATH)
        except Exception as exc:  # noqa: BLE001
            _engine_error = str(exc)
        return _engine


def upscale_image(image, target):
    long_edge = TARGET_LONG_EDGE[target]
    width, height = image.size
    scale = long_edge / max(width, height)
    if scale <= 1:
        return image.copy(), 1.0, "原图已达到目标尺寸，未放大"
    engine = get_engine()
    if engine is not None:
        # MPS 推理不宜并发；输出统一收敛到用户选择的目标长边。
        with _inference_lock:
            restored = engine.upscale(image)
        result = restored.resize((round(width * scale), round(height * scale)), Image.Resampling.LANCZOS)
        return result, scale, "Real-ESRGAN x4（官方 x4v3 权重）"
    resized = image.resize((round(width * scale), round(height * scale)), Image.Resampling.LANCZOS)
    # 保守锐化：只补边缘，不重绘素材，不伪造细节。
    if resized.mode in {"RGB", "RGBA"}:
        resized = resized.filter(ImageFilter.UnsharpMask(radius=1.2, percent=85, threshold=3))
    return resized, scale, "Pillow Lanczos 降级：" + (_engine_error or "Real-ESRGAN 未加载")


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        origin = self.headers.get("Origin", "")
        if origin == "null" or origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _respond(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/healthz"):
            engine = get_engine()
            self._respond(200, {"ok": True, "service": "local-super-resolution", "engine": "Real-ESRGAN" if engine else "Pillow fallback", "model_ready": bool(engine), "detail": _engine_error})
        else:
            self._respond(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/upscale"):
            self._respond(404, {"ok": False, "error": "not found"})
            return
        try:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._respond(400, {"ok": False, "error": "Content-Length 无效"})
            if length <= 0 or length > MAX_REQUEST_BYTES:
                return self._respond(413, {"ok": False, "error": "请求体超过 68MB 限制"})
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            target = body.get("target", "")
            if target not in TARGET_LONG_EDGE:
                raise ValueError("target 必须是 2k、4k 或 8k")
            image = decode_image(body.get("image_b64", ""))
            if not image.size[0] or not image.size[1]:
                raise ValueError("无法读取图片")
            result, scale, engine_name = upscale_image(image, target)
            has_alpha = "A" in result.getbands()
            if has_alpha:
                output = result.convert("RGBA")
                mime, fmt = "image/png", "PNG"
            else:
                output = result.convert("RGB")
                mime, fmt = "image/jpeg", "JPEG"
            buffer = io.BytesIO()
            output.save(buffer, format=fmt, quality=95, optimize=True)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            self._respond(200, {
                "ok": True, "image_b64": f"data:{mime};base64,{encoded}", "mime": mime,
                "width": output.width, "height": output.height, "scale": round(scale, 3),
                "engine": engine_name,
            })
        except Exception as exc:  # noqa: BLE001
            self._respond(400, {"ok": False, "error": str(exc)})

    def log_message(self, fmt, *args):
        print("[super-resolution] " + (fmt % args))


if __name__ == "__main__":
    print(f"本地超分服务已启动: http://127.0.0.1:{PORT} · 权重: {MODEL_PATH}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
