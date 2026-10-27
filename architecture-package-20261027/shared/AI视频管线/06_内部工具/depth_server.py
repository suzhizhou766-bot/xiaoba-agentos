#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
提示词导演台 · 视频/图片深度图转换服务 (depth_server.py)
==========================================================
用途: Depth Anything V2 (ONNX) 单目深度估计。
      图片 → 深度图(灰度 + 伪彩); 视频 → 抽帧逐帧深度 → 合成深度视频。

依赖: 统一运行时 .runtime/venvs/depth (onnxruntime + numpy + pillow) + .runtime/bin/ffmpeg。
启动: .runtime/venvs/depth/bin/python depth_server.py   # 默认端口 8891
健康: GET  http://127.0.0.1:8891/healthz
图片: POST /depth_image  {"image_b64": "<dataURI或裸base64>"}
视频: POST /depth_video  {"video_b64": "<裸base64>", "filename": "x.mp4", "max_frames": 120}
      → {"ok":true,"depth_video_b64":"<mp4 base64>","frames":N,"fps":F}
"""
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from PIL import Image

PORT = 8891
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_ROOT = os.path.abspath(os.path.join(BASE_DIR, "..", "..", ".runtime"))
MODEL_PATH = os.path.join(BASE_DIR, "models", "depth_anything_v2_small", "model.onnx")
INPUT_SIZE = 518
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32).reshape(1, 3, 1, 1)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32).reshape(1, 3, 1, 1)
FFMPEG = os.path.join(RUNTIME_ROOT, "bin", "ffmpeg")
if not os.path.isfile(FFMPEG):
    FFMPEG = None
MAX_VIDEO_B64 = 20 * 1024 * 1024   # 视频 base64 上限 ~15MB 实际文件
MAX_IMAGE_B64 = 16 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_VIDEO_B64 + 512 * 1024


def jet_lut():
    """手写 jet 伪彩 LUT(0-255 → BGR)。"""
    lut = []
    for i in range(256):
        x = i / 255.0
        r = min(1.0, max(0.0, 1.5 - abs(4.0 * x - 3.0)))
        g = min(1.0, max(0.0, 1.5 - abs(4.0 * x - 2.0)))
        b = min(1.0, max(0.0, 1.5 - abs(4.0 * x - 1.0)))
        lut.append((int(r * 255), int(g * 255), int(b * 255)))
    return lut


JET = jet_lut()


class DepthEngine:
    def __init__(self):
        import onnxruntime as ort
        self.sess = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
        self.input_name = self.sess.get_inputs()[0].name
        print(f"[depth] 模型已加载: {os.path.basename(MODEL_PATH)}", flush=True)

    def predict(self, rgb_np):
        """rgb_np: HxWx3 uint8 → 返回原尺寸相对深度 HxW float32 (0~1)"""
        h, w = rgb_np.shape[:2]
        img = Image.fromarray(rgb_np).resize((INPUT_SIZE, INPUT_SIZE), Image.BILINEAR)
        x = np.asarray(img, dtype=np.float32) / 255.0
        x = np.transpose(x, (2, 0, 1))[None]          # 1x3x518x518
        x = (x - MEAN) / STD
        out = self.sess.run(None, {self.input_name: x})[0]   # 当前导出: 1x518x518
        # 整条二维深度图在 batch 维之后；按 1x1x518x518 取 out[0,0] 只会拿到第 0 行的
        # 一维剖面，而 PIL 会把一维数组当 1x518 图静默拉伸成「深度图」。形状不符就报错。
        if out.ndim == 3 and out.shape[0] == 1:
            depth = out[0]
        elif out.ndim == 4 and out.shape[0] == 1 and out.shape[1] == 1:
            depth = out[0, 0]
        else:
            raise RuntimeError(f"深度模型输出结构异常: shape={out.shape}")
        if depth.ndim != 2:
            raise RuntimeError(f"深度图不是二维数组: shape={depth.shape}")
        grid = INPUT_SIZE // 14 * 14
        if depth.shape != (grid, grid):
            raise RuntimeError(f"深度网格 {depth.shape} 与预期 {(grid, grid)} 不符")
        depth = np.array(Image.fromarray(depth).resize((w, h), Image.BILINEAR))
        dmin, dmax = float(depth.min()), float(depth.max())
        if dmax - dmin < 1e-6:
            return np.zeros((h, w), dtype=np.float32)
        return (depth - dmin) / (dmax - dmin)

    def to_outputs(self, depth01):
        """深度图 → 灰度 PNG + 伪彩 PNG 的字节流"""
        gray = (depth01 * 255).astype(np.uint8)
        gray_buf = io.BytesIO()
        Image.fromarray(gray).save(gray_buf, format="PNG")
        h, w = gray.shape
        color = np.zeros((h, w, 3), dtype=np.uint8)
        idx = gray.astype(np.int32)
        color[..., 0] = np.array([c[0] for c in JET], dtype=np.uint8)[idx]
        color[..., 1] = np.array([c[1] for c in JET], dtype=np.uint8)[idx]
        color[..., 2] = np.array([c[2] for c in JET], dtype=np.uint8)[idx]
        color_buf = io.BytesIO()
        Image.fromarray(color).save(color_buf, format="PNG")
        return gray_buf.getvalue(), color_buf.getvalue()


def parallax_frames(img_np, depth01, frames=9, strength=0.05):
    """2.5D 视差:按深度做水平视差位移,生成左右摇摆帧序列(纯 numpy 双线性)。"""
    h, w = img_np.shape[:2]
    xx, yy = np.meshgrid(np.arange(w), np.arange(h))
    out = []
    for i in range(frames):
        t = (i / (frames - 1)) * 2.0 - 1.0          # -1..1
        shift = t * strength * w
        d = depth01 * shift                           # 近处(depth 大)偏移大
        sx = np.clip(xx + d, 0, w - 1)
        x0 = np.floor(sx).astype(np.int32)
        x1 = np.minimum(x0 + 1, w - 1)
        fx = (sx - x0)[..., None]
        frame = (img_np[yy, x0] * (1.0 - fx) + img_np[yy, x1] * fx).astype(np.uint8)
        out.append(frame)
    return out


def b64_to_bytes(b64):
    if not isinstance(b64, str) or not b64:
        raise ValueError("缺少 base64 数据")
    if b64.startswith("data:"):
        b64 = b64.split(",", 1)[1]
    if len(b64) > MAX_VIDEO_B64:
        raise ValueError("base64 数据超过 20MB 限制")
    try:
        return base64.b64decode(b64, validate=True)
    except ValueError as exc:
        raise ValueError("base64 数据格式无效") from exc


def extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else None


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        origin = self.headers.get("Origin", "")
        if origin == "null" or origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/healthz"):
            ok = os.path.exists(MODEL_PATH)
            self._respond(200, {"service": "depth-server", "ok": ok, "model": os.path.basename(MODEL_PATH) if ok else "MISSING"})
        else:
            self._respond(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._respond(400, {"ok": False, "error": "Content-Length 无效"})
        if length <= 0 or length > MAX_REQUEST_BYTES:
            return self._respond(413, {"ok": False, "error": "请求体超过 20.5MB 限制"})
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if self.path.startswith("/depth_image"):
                out = self._depth_image(body)
            elif self.path.startswith("/depth_video"):
                out = self._depth_video(body)
            elif self.path.startswith("/parallax"):
                out = self._parallax(body)
            else:
                return self._respond(404, {"ok": False, "error": "not found"})
            self._respond(200, out)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc(file=sys.stderr)
            self._respond(500, {"ok": False, "error": str(e)})

    def _depth_image(self, body):
        image_b64 = body.get("image_b64", "")
        if not isinstance(image_b64, str) or len(image_b64) > MAX_IMAGE_B64:
            raise ValueError("图片 base64 超过 16MB 限制")
        img_bytes = b64_to_bytes(image_b64)
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        t0 = time.time()
        depth01 = engine.predict(np.asarray(img))
        gray_png, color_png = engine.to_outputs(depth01)
        return {
            "ok": True,
            "depth_b64": base64.b64encode(gray_png).decode(),
            "colored_b64": base64.b64encode(color_png).decode(),
            "width": img.width, "height": img.height,
            "ms": int((time.time() - t0) * 1000),
        }

    def _depth_video(self, body):
        v64 = body.get("video_b64", "")
        if len(v64) > MAX_VIDEO_B64:
            raise ValueError("视频过大: base64 上限 20MB(约 15MB 原文件), 请先压缩或截短")
        max_frames = min(max(int(body.get("max_frames", 120)), 1), 240)
        vid_bytes = b64_to_bytes(v64)
        work = tempfile.mkdtemp(prefix="depth_vid_")
        try:
            in_path = os.path.join(work, "in.mp4")
            open(in_path, "wb").write(vid_bytes)
            # 探测时长/帧率
            probe = subprocess.run(
                [FFMPEG, "-i", in_path, "-f", "null", "-"],
                capture_output=True, text=True, timeout=30)
            m = re.search(r"(\d+(?:\.\d+)?) fps", probe.stderr)
            src_fps = float(m.group(1)) if m else 12.0
            fps = min(src_fps, 12.0)
            # 抽帧
            fr_dir = os.path.join(work, "frames")
            os.makedirs(fr_dir)
            subprocess.run(
                [FFMPEG, "-y", "-i", in_path, "-vf", f"fps={fps}", "-q:v", "2",
                 os.path.join(fr_dir, "f_%04d.jpg")],
                capture_output=True, timeout=300, check=True)
            frames = sorted(os.listdir(fr_dir))
            if len(frames) > max_frames:
                frames = frames[:max_frames]
            t0 = time.time()
            # 逐帧深度 → 灰度帧 + 伪彩帧(Python 端 JET,与图片深度配色一致)
            dp_dir = os.path.join(work, "depths")
            dg_dir = os.path.join(work, "grays")
            os.makedirs(dp_dir)
            os.makedirs(dg_dir)
            r_lut = np.array([c[0] for c in JET], dtype=np.uint8)
            g_lut = np.array([c[1] for c in JET], dtype=np.uint8)
            b_lut = np.array([c[2] for c in JET], dtype=np.uint8)
            for i, fn in enumerate(frames):
                frame = Image.open(os.path.join(fr_dir, fn)).convert("RGB")
                d = engine.predict(np.asarray(frame))
                gray = (d * 255).astype(np.uint8)
                Image.fromarray(gray).save(os.path.join(dg_dir, f"g_{i:04d}.png"))
                idx = gray.astype(np.int32)
                color = np.dstack([r_lut[idx], g_lut[idx], b_lut[idx]])
                Image.fromarray(color).save(os.path.join(dp_dir, f"d_{i:04d}.png"))
            per_frame = (time.time() - t0) / max(len(frames), 1)
            # 合成两个深度视频:伪彩(便于观看) + 灰度(便于 ControlNet/深度控制)
            color_path = os.path.join(work, "depth.mp4")
            gray_path = os.path.join(work, "depth_gray.mp4")
            subprocess.run(
                [FFMPEG, "-y", "-framerate", str(fps), "-i", os.path.join(dp_dir, "d_%04d.png"),
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", color_path],
                capture_output=True, timeout=300, check=True)
            subprocess.run(
                [FFMPEG, "-y", "-framerate", str(fps), "-i", os.path.join(dg_dir, "g_%04d.png"),
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", gray_path],
                capture_output=True, timeout=300, check=True)
            out_b64 = base64.b64encode(open(color_path, "rb").read()).decode()
            gray_b64 = base64.b64encode(open(gray_path, "rb").read()).decode()
            return {
                "ok": True,
                "depth_video_b64": out_b64,
                "depth_gray_b64": gray_b64,
                "frames": len(frames), "fps": fps,
                "seconds": round(len(frames) / fps, 1),
                "per_frame_ms": int(per_frame * 1000),
                "total_s": round(time.time() - t0, 1),
            }
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _parallax(self, body):
        """2.5D 视差动图:原图 + 深度 → 左右摇摆 GIF。"""
        img_bytes = b64_to_bytes(body.get("image_b64", ""))
        frames_n = min(max(int(body.get("frames", 9)), 2), 60)
        strength = min(max(float(body.get("strength", 0.05)), 0.0), 0.25)
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        # 控制输出体积:最长边 ≤ 800
        MAX = 800
        if max(img.size) > MAX:
            img.thumbnail((MAX, MAX), Image.LANCZOS)
        t0 = time.time()
        depth01 = engine.predict(np.asarray(img))
        frames = parallax_frames(np.asarray(img), depth01, frames_n, strength)
        gif_buf = io.BytesIO()
        imgs = [Image.fromarray(f) for f in frames]
        imgs[0].save(gif_buf, format="GIF", save_all=True, append_images=imgs[1:],
                     duration=110, loop=0, optimize=False)
        return {
            "ok": True,
            "gif_b64": base64.b64encode(gif_buf.getvalue()).decode(),
            "frames": frames_n, "strength": strength,
            "ms": int((time.time() - t0) * 1000),
            "size_kb": round(len(gif_buf.getvalue()) / 1024, 1),
        }

    def _respond(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        sys.stderr.write("[depth] " + (fmt % args) + "\n")


engine = None


def main():
    global engine
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    if not FFMPEG:
        print("FATAL: 统一运行时缺少 .runtime/bin/ffmpeg", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(MODEL_PATH):
        print("FATAL: 模型不存在, 请先下载:")
        print(f"  curl -L -o models/depth_anything_v2_small/model.onnx \\")
        print("    https://hf-mirror.com/onnx-community/depth-anything-v2-small/resolve/main/onnx/model.onnx")
        sys.exit(1)
    engine = DepthEngine()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"深度图服务已启动: http://127.0.0.1:{port}  (图片+视频深度转换)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
