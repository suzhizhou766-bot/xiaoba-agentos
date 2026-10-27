#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
提示词导演台 · 图片反推服务 (prompt_reverse_server.py)
=====================================================
用途: 供提示词导演台上传图片，通过 AGY 当前 Web 选择对应的多模态模型，
      反推出结构化导演参数并自动填回网页控件。

边界: 每次只在隔离临时目录放置本次上传图片；不发送源码或项目材料；
      不自动重试、不切换模型。
启动: python3 prompt_reverse_server.py   # 默认端口 8890
健康: GET  http://127.0.0.1:8890/healthz
反推: POST http://127.0.0.1:8890/reverse  {"image_b64": "<dataURI或裸base64>", "filename": "x.jpg"}
看图补全: POST http://127.0.0.1:8890/describe  {"images": [{"role": "character|scene|first|last|anchor", "image_b64": "…", "filename": "x.jpg"}]}
      最多 6 张，顺序即 <Picture N>；固定用 Gemini 3.8 Flash (High)，不跟随桌面端选中的模型。
      返回每张图的英文定义、角色外观、场景描述和中文提醒，供提示词导演台的「参考素材」写进提示词。
"""
import json
import re
import sys
import base64
import os
import mimetypes
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8890
AGENTOS_CONFIG = Path("/Users/suzhizhou/Documents/小八AgentOS/config/agentos.json")
AGY_TIMEOUT_SECONDS = 180
ROUTE_CACHE_SECONDS = 10
_route_cache = None
MAX_REQUEST_BYTES = 40 * 1024 * 1024    # 看图补全一次可带多张图
MAX_IMAGE_B64_BYTES = 16 * 1024 * 1024
_request_slots = threading.BoundedSemaphore(2)

SYSTEM_PROMPT = """You are a professional AI film prompt director reverse-engineering tool.
Analyze the image and return STRICT JSON ONLY (no markdown, no explanation) with EXACTLY these keys,
using only the enum values given:

{
 "scene": "arch_evo|arch_ext|interior|nature|urban",        // 场景类型
 "scene_desc_en": "one English sentence describing the main subject of the shot, usable as a video/image generation scene prompt",
 "time_of_day": "morning|day|golden|bluehour|night|overcast",
 "stage": "none|raw|hard|soft|final",                       // 建筑空间演化阶段, 非建筑= none
 "lens_type": "ultrawide|wide|normal|shorttele|tele|supertele|macro",
 "focal": "24|35|50|85|135",                                // 最接近的等效焦距
 "aperture": "f/1.4|f/1.8|f/2.8|f/4|f/5.6",
 "shot_size": "extreme_wide|wide_shot|full_shot|medium_shot|close_up|extreme_close|macro_close",
 "cam_height": "ground|low|hip|eye|high|bird",
 "cam_angle": "level|low_angle|high_angle|dutch",
 "cam_move": "static|push|pull|truck|pan|orbit|handheld_follow|crane",
 "stability": "locked|slight|docu_handheld",
 "body_preset": "cine|film|digital|anamorphic|docu",        // 画面质感
 "light_source": "sun|golden|sunset|night_urban|indoor|backlit|overcast_soft|moon",
 "light_ratio": "low|mid|high",
 "light_dir": "side|front|back|top",
 "has_person": true,
 "person": {"count":"1","age":"toddler|child|teen|young|middle|senior","height":"short|mid|tall|v_tall",
            "build":"slim|normal|athletic|sturdy|plump","face":"calm|smile|laugh|thinking|gaze|tense|tired|surprised|sad|resolute|gentle|dignified",
            "action":"stand|walk|run|lookback|lean|sit|stretch|hold|wave|squat|jump",
            "gaze":"camera|away|scene|down","cloth":"casual|business|sporty|home|uniform|ethnic|minimal"},
 "palette": ["#aabbcc","#ddeeff","#112233"],                 // 60%主色 / 30%辅色 / 10%点缀 十六进制
 "photography": {"p1":true,"p2":true,"p3":true,"p4":true,"p5":true},  // 摄影五件套是否成立(空气透视/光比/柔光/黑位/微表面)
 "suggested_model": "seedance|soulcinema|soul2",             // 建议生成模型
 "confidence": "high|medium|low"
}

Rules:
- has_person=false when no human is present; person=null then.
- The scene_desc_en must be in English and only describe what is visible.
- Palette colors: read from the actual dominant colors of the image.
- For architecture/interiors prefer scene=arch_evo/interior; street scenes=urban; landscape=nature."""

ENUMS = {
    "scene": {"arch_evo", "arch_ext", "interior", "nature", "urban"},
    "time_of_day": {"morning", "day", "golden", "bluehour", "night", "overcast"},
    "stage": {"none", "raw", "hard", "soft", "final"},
    "lens_type": {"ultrawide", "wide", "normal", "shorttele", "tele", "supertele", "macro"},
    "focal": {"24", "35", "50", "85", "135"},
    "aperture": {"f/1.4", "f/1.8", "f/2.8", "f/4", "f/5.6"},
    "shot_size": {"extreme_wide", "wide_shot", "full_shot", "medium_shot", "close_up", "extreme_close", "macro_close"},
    "cam_height": {"ground", "low", "hip", "eye", "high", "bird"},
    "cam_angle": {"level", "low_angle", "high_angle", "dutch"},
    "cam_move": {"static", "push", "pull", "truck", "pan", "orbit", "handheld_follow", "crane"},
    "stability": {"locked", "slight", "docu_handheld"},
    "body_preset": {"cine", "film", "digital", "anamorphic", "docu"},
    "light_source": {"sun", "golden", "sunset", "night_urban", "indoor", "backlit", "overcast_soft", "moon"},
    "light_ratio": {"low", "mid", "high"},
    "light_dir": {"side", "front", "back", "top"},
    "suggested_model": {"seedance", "soulcinema", "soul2"},
    "confidence": {"high", "medium", "low"},
}
PERSON_ENUMS = {
    "count": {"1", "2", "3", "4+"}, "age": {"toddler", "child", "teen", "young", "middle", "senior"},
    "height": {"short", "mid", "tall", "v_tall"}, "build": {"slim", "normal", "athletic", "sturdy", "plump"},
    "face": {"calm", "smile", "laugh", "thinking", "gaze", "tense", "tired", "surprised", "sad", "resolute", "gentle", "dignified"},
    "action": {"stand", "walk", "run", "lookback", "lean", "sit", "stretch", "hold", "wave", "squat", "jump"},
    "gaze": {"camera", "away", "scene", "down"}, "cloth": {"casual", "business", "sporty", "home", "uniform", "ethnic", "minimal"},
}


def normalize_result(result):
    """Normalize harmless model formatting variants before strict validation."""
    if not isinstance(result, dict):
        return result
    result = dict(result)
    for key in ("focal",):
        value = result.get(key)
        if isinstance(value, (int, float)):
            result[key] = str(int(value)) if float(value).is_integer() else str(value)
    aperture = result.get("aperture")
    if isinstance(aperture, str):
        match = re.fullmatch(r"f/([0-9]+(?:\.[0-9]+)?)", aperture.strip().lower())
        if match:
            numeric = float(match.group(1))
            options = {1.4: "f/1.4", 1.8: "f/1.8", 2.8: "f/2.8", 4.0: "f/4", 5.6: "f/5.6"}
            result["aperture"] = options[min(options, key=lambda item: abs(item - numeric))]
    person = result.get("person")
    if isinstance(person, dict):
        person = dict(person)
        count = person.get("count")
        if isinstance(count, (int, float)) and float(count).is_integer():
            person["count"] = str(int(count))
        result["person"] = person
    return result


def validate_result(result):
    if not isinstance(result, dict):
        raise ValueError("模型结果必须是 JSON 对象")
    required = set(ENUMS) | {"scene_desc_en", "has_person", "person", "palette", "photography"}
    missing = sorted(required - set(result))
    if missing:
        raise ValueError("模型结果缺少字段: " + ", ".join(missing))
    extra = sorted(set(result) - required)
    if extra:
        raise ValueError("模型结果包含未约定字段: " + ", ".join(extra))
    for key, values in ENUMS.items():
        if not isinstance(result[key], str) or result[key] not in values:
            raise ValueError(f"字段 {key} 的值不合法")
    if not isinstance(result["scene_desc_en"], str) or not result["scene_desc_en"].strip() or len(result["scene_desc_en"]) > 600:
        raise ValueError("scene_desc_en 必须是非空短字符串")
    if not isinstance(result["has_person"], bool):
        raise ValueError("has_person 必须是布尔值")
    person = result["person"]
    if result["has_person"]:
        if not isinstance(person, dict) or set(person) != set(PERSON_ENUMS):
            raise ValueError("has_person=true 时 person 字段结构不完整")
        for key, values in PERSON_ENUMS.items():
            valid = bool(re.fullmatch(r"[1-9][0-9]*", person[key])) if key == "count" and isinstance(person[key], str) else person[key] in values
            if not isinstance(person[key], str) or not valid:
                raise ValueError(f"person.{key} 的值不合法")
    elif person is not None:
        raise ValueError("has_person=false 时 person 必须为 null")
    palette = result["palette"]
    if not isinstance(palette, list) or len(palette) != 3 or any(not isinstance(c, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", c) for c in palette):
        raise ValueError("palette 必须是 3 个十六进制颜色")
    photography = result["photography"]
    if not isinstance(photography, dict) or set(photography) != {"p1", "p2", "p3", "p4", "p5"} or any(not isinstance(v, bool) for v in photography.values()):
        raise ValueError("photography 必须包含 p1-p5 布尔字段")
    return result


def load_agy_route():
    """跟随 Antigravity 桌面端当前选中的模型，与 AgentOS 的 local_agy 路由同一个读取器；不回退。"""
    global _route_cache
    if _route_cache and time.monotonic() - _route_cache[0] < ROUTE_CACHE_SECONDS:
        return _route_cache[1]
    try:
        config = json.loads(AGENTOS_CONFIG.read_text(encoding="utf-8"))
        route = config["cli_routes"]["local_agy"]
        command = config["integrations"]["agy_command"]
        debug_port = route.get("desktop_debug_port")
        if route.get("driver") != "agy" or route.get("selection") != "local_account" or not debug_port or not command:
            raise ValueError("local_agy 配置不完整")
        command_path = Path(command)
        if not command_path.is_file():
            raise ValueError(f"AGY CLI 不存在：{command_path}")
        completed = subprocess.run(
            [str(config["integrations"]["node_command"]), str(AGENTOS_CONFIG.parent.parent / "scripts" / "agy_desktop_route.cjs"), str(debug_port)],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if completed.returncode:
            reason = re.search(r"^reason=([a-z_]{1,40})$", completed.stderr or "", re.MULTILINE)
            raise ValueError("读不到 Antigravity 桌面端的模型选择（" + (reason.group(1) if reason else "unknown") + "），请确认 Antigravity 已打开且只有一个窗口")
        metadata = json.loads(completed.stdout)
        model = metadata.get("model") if isinstance(metadata, dict) else None
        if not isinstance(model, str) or not model.strip() or metadata.get("model_source") != "antigravity_desktop":
            raise ValueError("Antigravity 桌面端返回的模型信息无效")
        resolved = {"command": str(command_path), "provider": str(metadata.get("provider") or "Antigravity"), "model": model}
        _route_cache = (time.monotonic(), resolved)
        return resolved
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"无法读取 AGY 当前路由：{exc}") from exc


class UpstreamError(RuntimeError):
    """AGY/Antigravity 单次调用失败，交给 HTTP 层返回可识别的 502。"""

    status_code = 502


def _decode_image(image_b64):
    value = image_b64.split(",", 1)[1] if image_b64.startswith("data:") else image_b64
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise ValueError("图片 base64 格式无效") from exc


def _safe_extension(filename):
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return suffix
    guessed = mimetypes.guess_extension(mimetypes.guess_type(filename or "")[0] or "")
    return guessed if guessed in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"


def call_agy(image_b64, filename):
    route = load_agy_route()
    image_bytes = _decode_image(image_b64)
    prompt = (
        "@input_image" + _safe_extension(filename) + "\n"
        + SYSTEM_PROMPT
        + "\nAnalyze only the attached image and return the required strict JSON object. "
          "Do not inspect any other file, do not use web search, and do not include Markdown fences."
    )
    try:
        with tempfile.TemporaryDirectory(prefix="xiaoba-agy-vision-") as temp_dir:
            temp_path = Path(temp_dir)
            image_path = temp_path / ("input_image" + _safe_extension(filename))
            image_path.write_bytes(image_bytes)
            completed = subprocess.run(
                [
                    route["command"],
                    "--print=" + prompt,
                    "--output-format", "json",
                    "--model", route["model"],
                    "--mode", "plan",
                    "--sandbox",
                    "--dangerously-skip-permissions",
                    "--add-dir", temp_dir,
                    "--print-timeout", f"{AGY_TIMEOUT_SECONDS}s",
                ],
                cwd=temp_dir,
                text=True,
                capture_output=True,
                timeout=AGY_TIMEOUT_SECONDS + 15,
                check=False,
                env={
                    "HOME": str(Path.home()),
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "LANG": "en_US.UTF-8",
                    "LC_CTYPE": "UTF-8",
                },
            )
    except subprocess.TimeoutExpired as exc:
        raise UpstreamError("AGY 视觉调用超时") from exc
    except OSError as exc:
        raise UpstreamError(f"AGY CLI 启动失败：{exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "AGY CLI failed").strip()
        raise UpstreamError("AGY 视觉调用失败：" + detail[:500])
    try:
        envelope = json.loads((completed.stdout or "").strip())
    except json.JSONDecodeError as exc:
        raise UpstreamError("AGY 返回了不可解析的响应") from exc
    if not isinstance(envelope, dict) or str(envelope.get("status", "")).upper() != "SUCCESS":
        detail = str(envelope.get("error") or envelope.get("response") or "AGY 未返回成功状态")
        raise UpstreamError(detail[:500])
    response = str(envelope.get("response") or "").strip()
    if not response:
        raise UpstreamError("AGY 未返回视觉分析结果")
    result = validate_result(normalize_result(extract_json(response)))
    return result, route


# ---------- 看图补全：角色卡 / 场景参考图 → 可直接写进提示词的描述 ----------
# 这个接口固定用 Gemini 3.8 Flash（用户指定），不跟随 Antigravity 桌面端当前选中的模型。
DESCRIBE_MODEL = "Gemini 3.8 Flash (High)"
DESCRIBE_ROLES = {"character": "character reference sheet / character card", "scene": "scene / environment reference",
                  "first": "first frame of the shot", "last": "last frame of the shot",
                  "anchor": "composition anchor / general visual reference"}
MAX_DESCRIBE_IMAGES = 6
DESCRIBE_PROMPT = """You are a professional AI video prompt director preparing reference material notes.
The attached images are numbered in the order listed below; each has a declared role.
Look at every image and return STRICT JSON ONLY (no markdown, no explanation) with EXACTLY these keys:

{
 "pictures": [   // one item per attached image, same order
   {"n": 1, "definition_en": "ONE English sentence stating what this picture shows and what it locks (identity / costume / space / light / framing), suitable to follow '<Picture 1> is'"}
 ],
 "subjects": [   // one item per distinct character or hero object that appears in a 'character' image; [] if none
   {"from_picture": 1, "name_en": "short noun phrase, e.g. 'the young woman in a red qipao'",
    "appearance_en": "concise visible facts only: apparent age group, hair, clothing and colors, accessories, distinctive features, art style if not photoreal"}
 ],
 "scene_en": "1-2 English sentences on the environment from the 'scene' image(s): space type, architecture, materials, light direction and time of day; empty string if no scene image",
 "notes_zh": "用中文写 1-2 句提醒：例如图里有文字或 logo、角色卡是三视图、几张图画风不一致；没有就留空字符串"
}

Rules: describe only what is visible; never invent names, brands, story or motion; a multi-view turnaround sheet is ONE character;
do not inspect any other file; do not use web search; do not run commands; no Markdown fences."""


def agy_command():
    """只取 AGY CLI 的路径，不读桌面端选中的模型。"""
    try:
        config = json.loads(AGENTOS_CONFIG.read_text(encoding="utf-8"))
        path = Path(str(config["integrations"]["agy_command"]))
        if path.is_file():
            return str(path)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    fallback = Path("/Users/suzhizhou/Documents/AGY/agy")
    if fallback.is_file():
        return str(fallback)
    raise UpstreamError("找不到 AGY CLI")


def run_agy(command, model, prompt, temp_dir):
    """调用一次 AGY，返回模型的文字回复；失败抛 UpstreamError。"""
    try:
        completed = subprocess.run(
            [command, "--print=" + prompt, "--output-format", "json", "--model", model, "--mode", "plan", "--sandbox",
             "--dangerously-skip-permissions", "--add-dir", temp_dir, "--print-timeout", f"{AGY_TIMEOUT_SECONDS}s"],
            cwd=temp_dir, text=True, capture_output=True, timeout=AGY_TIMEOUT_SECONDS + 15, check=False,
            env={"HOME": str(Path.home()), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8", "LC_CTYPE": "UTF-8"},
        )
    except subprocess.TimeoutExpired as exc:
        raise UpstreamError("AGY 视觉调用超时") from exc
    except OSError as exc:
        raise UpstreamError(f"AGY CLI 启动失败：{exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "AGY CLI failed").strip()
        raise UpstreamError("AGY 视觉调用失败：" + detail[:500])
    try:
        envelope = json.loads((completed.stdout or "").strip())
    except json.JSONDecodeError as exc:
        raise UpstreamError("AGY 返回了不可解析的响应") from exc
    if not isinstance(envelope, dict) or str(envelope.get("status", "")).upper() != "SUCCESS":
        detail = str(envelope.get("error") or envelope.get("response") or "AGY 未返回成功状态")
        raise UpstreamError(detail[:500])
    response = str(envelope.get("response") or "").strip()
    if not response:
        raise UpstreamError("AGY 未返回视觉分析结果")
    return response


def _text(value, limit):
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def validate_description(raw, count):
    """把模型的回答收拾成固定形状；缺的项留空，不替它编。"""
    if not isinstance(raw, dict):
        raise ValueError("模型返回的不是 JSON 对象")
    pictures = {}
    for item in raw.get("pictures") if isinstance(raw.get("pictures"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("n"), int) and 1 <= item["n"] <= count:
            pictures[item["n"]] = _text(item.get("definition_en"), 400)
    subjects = []
    for item in raw.get("subjects") if isinstance(raw.get("subjects"), list) else []:
        if not isinstance(item, dict):
            continue
        source = item.get("from_picture")
        name, look = _text(item.get("name_en"), 120), _text(item.get("appearance_en"), 500)
        if name or look:
            subjects.append({"from_picture": source if isinstance(source, int) and 1 <= source <= count else None,
                             "name_en": name, "appearance_en": look})
    if not any(pictures.values()) and not subjects:
        raise ValueError("模型没有给出任何描述")
    return {"pictures": [{"n": n, "definition_en": pictures.get(n, "")} for n in range(1, count + 1)],
            "subjects": subjects[:6], "scene_en": _text(raw.get("scene_en"), 600), "notes_zh": _text(raw.get("notes_zh"), 300)}


def describe_references(images):
    """images: [{"role", "image_b64", "filename"}]，顺序就是 <Picture N> 的编号。"""
    if not isinstance(images, list) or not 1 <= len(images) <= MAX_DESCRIBE_IMAGES:
        raise ValueError(f"需要 1 到 {MAX_DESCRIBE_IMAGES} 张图片")
    command = agy_command()
    with tempfile.TemporaryDirectory(prefix="xiaoba-agy-describe-") as temp_dir:
        mentions, listing = [], []
        for index, image in enumerate(images, 1):
            if not isinstance(image, dict) or not isinstance(image.get("image_b64"), str) or not image["image_b64"]:
                raise ValueError(f"第 {index} 张图片缺少 image_b64")
            if len(image["image_b64"]) > MAX_IMAGE_B64_BYTES:
                raise ValueError("图片 base64 超过 16MB 限制")
            role = image.get("role") if image.get("role") in DESCRIBE_ROLES else "anchor"
            name = f"picture_{index}" + _safe_extension(str(image.get("filename") or ""))
            (Path(temp_dir) / name).write_bytes(_decode_image(image["image_b64"]))
            mentions.append("@" + name)
            listing.append(f"Picture {index} = {name} — role: {DESCRIBE_ROLES[role]}")
        prompt = " ".join(mentions) + "\n" + DESCRIBE_PROMPT + "\n\nAttached images:\n" + "\n".join(listing)
        response = run_agy(command, DESCRIBE_MODEL, prompt, temp_dir)
    return validate_description(extract_json(response), len(images))


def extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    # Some vision gateways append a second JSON object or a short explanation.
    # Decode the first complete object with the standard parser instead of a
    # greedy regex, which turns valid `}{` output into "Extra data" failures.
    decoder = json.JSONDecoder()
    for offset, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[offset:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("模型未返回 JSON: " + text[:200])


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
            try:
                route = load_agy_route()
                self._respond(200, {"service": "prompt-reverse", "ok": True, "backend": "agy", "provider": route["provider"], "model": route["model"]})
            except RuntimeError as exc:
                self._respond(503, {"service": "prompt-reverse", "ok": False, "error": str(exc)})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        describe = self.path.startswith("/describe")
        if not describe and not self.path.startswith("/reverse"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._respond(400, {"ok": False, "error": "Content-Length 无效"})
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._respond(413, {"ok": False, "error": "请求体超过 40MB 限制"})
            return
        if not _request_slots.acquire(blocking=False):
            self._respond(429, {"ok": False, "error": "反推服务繁忙，请稍后重试"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if describe:
                result = describe_references(body.get("images"))
                self._respond(200, {"ok": True, "result": result, "backend": "agy", "provider": "Antigravity", "model": DESCRIBE_MODEL})
                return
            image_b64 = body.get("image_b64", "")
            filename = body.get("filename", "image.jpg")
            if not image_b64:
                raise ValueError("缺少 image_b64")
            if len(image_b64) > MAX_IMAGE_B64_BYTES:
                raise ValueError("图片 base64 超过 16MB 限制")
            result, route = call_agy(image_b64, filename)
            self._respond(200, {"ok": True, "result": result, "backend": "agy", "provider": route["provider"], "model": route["model"]})
        except ValueError as e:
            self._respond(400, {"ok": False, "error": str(e)})
        except UpstreamError as e:
            self._respond(e.status_code, {"ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._respond(500, {"ok": False, "error": str(e)})
        finally:
            _request_slots.release()

    def _respond(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        sys.stderr.write("[prompt-reverse] " + (fmt % args) + "\n")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    try:
        route = load_agy_route()
        print(f"AGY route OK · provider={route['provider']} · model={route['model']}", flush=True)
    except RuntimeError as e:
        print("FATAL:", e, file=sys.stderr)
        sys.exit(1)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"提示词反推服务已启动: http://127.0.0.1:{port}  (healthz 可自检)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
