#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小八提示词导演台的本地工具控制器（只监听 127.0.0.1:8893）。"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
RUNTIME_ROOT = BASE.parent.parent / ".runtime"
CORE_PYTHON = RUNTIME_ROOT / "venvs" / "core" / "bin" / "python"
DEPTH_PYTHON = RUNTIME_ROOT / "venvs" / "depth" / "bin" / "python"
SUPERRES_PYTHON = RUNTIME_ROOT / "venvs" / "superres" / "bin" / "python"
PORT = 8893
STARTUP_TIMEOUT = 8.0
OSS_CACHE_TTL = 15 * 60
LOG_ROOT = RUNTIME_ROOT / "logs"
OSS_CACHE_PATH = RUNTIME_ROOT / "cache" / "oss_tools_cache.json"
MAX_REQUEST_BYTES = 64 * 1024

OSS_REPOSITORIES = [
    {
        "repository": "Blizaine/Maestro",
        "fit": "本地全流程(图片/视频/音乐)",
        "fallback_desc": "100% 本地 AI 视频/图像/音乐工作室, Director mode 自动规划镜头与音乐",
    },
    {
        "repository": "jnMetaCode/ai-shortfilm-prompts",
        "fit": "故事 → 分镜提示词",
        "fallback_desc": "Claude Code Skill, 把任意想法转成 Sora/Seedance 可用的电影级分镜提示词",
    },
    {
        "repository": "Hao0321/ai-media-generator",
        "fit": "自动生成高级导演提示词",
        "fallback_desc": "零门槛电影感: 资深导演提示词自动生成, Claude Code Skill",
    },
    {
        "repository": "jijiutong/ai-visual-director",
        "fit": "分镜/角色/场景设计",
        "fallback_desc": "AI 视觉导演 Skill: 分镜、角色、场景、电影镜头一站式",
    },
]

SERVICES = {
    "reverse": {
        "label": "图片 / 视频提示词反推",
        "port": 8890,
        "command": [str(CORE_PYTHON), str(BASE / "prompt_reverse_server.py")],
        "log": LOG_ROOT / "prompt-reverse.log",
    },
    "depth": {
        "label": "深度图转换",
        "port": 8891,
        "command": [str(DEPTH_PYTHON), str(BASE / "depth_server.py")],
        "log": LOG_ROOT / "depth-server.log",
        "launch_agent": "com.xiaoba.depth-server",
        "launch_plist": Path.home() / "Library/LaunchAgents/com.xiaoba.depth-server.plist",
    },
    "superres": {
        "label": "Real-ESRGAN 超分",
        "port": 8892,
        "command": [str(SUPERRES_PYTHON), str(BASE / "super_resolution_server.py")],
        "log": LOG_ROOT / "superres-server.log",
    },
}

_processes: dict[str, subprocess.Popen[bytes]] = {}
_log_handles: dict[str, object] = {}
_last_result: dict[str, dict] = {}
_service_lock = threading.RLock()


def is_running(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.35)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _tail_log(path: Path, limit: int = 1200) -> str:
    try:
        text = path.read_bytes()[-limit:].decode("utf-8", errors="replace")
    except OSError:
        return ""
    text = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        text,
    )
    lines = list(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()))
    return "；".join(lines[-3:])[:600]


def _read_oss_cache() -> dict | None:
    try:
        payload = json.loads(OSS_CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("tools"), list):
            return payload
    except (OSError, ValueError, TypeError):
        pass
    return None


def _write_oss_cache(payload: dict) -> None:
    temporary = OSS_CACHE_PATH.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(OSS_CACHE_PATH)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass


def _fallback_oss_tools() -> list[dict]:
    return [
        {
            "name": item["repository"].split("/", 1)[1],
            "repository": item["repository"],
            "stars": None,
            "description": item["fallback_desc"],
            "url": f"https://github.com/{item['repository']}",
            "fit": item["fit"],
            "updated_at": None,
        }
        for item in OSS_REPOSITORIES
    ]


def _fetch_oss_repository(item: dict) -> dict:
    repository = item["repository"]
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "xiaoba-prompt-director"},
    )
    with urllib.request.urlopen(request, timeout=4) as response:
        data = json.loads(response.read().decode("utf-8"))
    return {
        "name": data.get("name") or repository.split("/", 1)[1],
        "repository": repository,
        "stars": data.get("stargazers_count"),
        "description": data.get("description") or item["fallback_desc"],
        "url": data.get("html_url") or f"https://github.com/{repository}",
        "fit": item["fit"],
        "updated_at": data.get("updated_at") or data.get("pushed_at"),
    }


def get_oss_tools() -> dict:
    now = time.time()
    cached = _read_oss_cache()
    if cached and now - float(cached.get("fetched_epoch", 0)) < OSS_CACHE_TTL:
        return cached | {"source": "cache"}

    tools = []
    failures = []
    for item in OSS_REPOSITORIES:
        try:
            tools.append(_fetch_oss_repository(item))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            failures.append(f"{item['repository']}: {exc}")

    if len(tools) == len(OSS_REPOSITORIES):
        tools.sort(key=lambda item: item.get("stars") or -1, reverse=True)
        payload = {
            "ok": True,
            "source": "github",
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "fetched_epoch": now,
            "tools": tools,
        }
        _write_oss_cache(payload)
        return payload

    if cached:
        return cached | {"source": "stale-cache", "warning": "GitHub 暂时不可用，显示上次成功更新的数据"}
    return {
        "ok": True,
        "source": "fallback",
        "fetched_at": None,
        "fetched_epoch": 0,
        "tools": _fallback_oss_tools(),
        "warning": "GitHub 暂时不可用，显示内置数据",
        "errors": failures,
    }


def _remember(service: str, **values: object) -> None:
    _last_result.setdefault(service, {}).update(values)


def state(service: str) -> dict:
    spec = SERVICES[service]
    running = is_running(spec["port"])
    process = _processes.get(service)
    result = {
        "id": service,
        "label": spec["label"],
        "port": spec["port"],
        "running": running,
    }
    if process is not None and process.poll() is None:
        result["pid"] = process.pid
    result.update({key: value for key, value in _last_result.get(service, {}).items() if value})
    return result


def _close_log(service: str) -> None:
    handle = _log_handles.pop(service, None)
    if handle is not None:
        try:
            handle.close()
        except OSError:
            pass


def _launch_domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, check=False)


def _launch_agent_loaded(label: str) -> bool:
    result = _launchctl("print", f"{_launch_domain()}/{label}")
    return result.returncode == 0


def _start_managed_service(spec: dict) -> tuple[bool, str]:
    label = spec.get("launch_agent")
    plist = spec.get("launch_plist")
    if not label or not plist or not plist.is_file():
        return False, ""
    domain = _launch_domain()
    if not _launch_agent_loaded(label):
        result = _launchctl("bootstrap", domain, str(plist))
        if result.returncode != 0 and not _launch_agent_loaded(label):
            detail = (result.stderr or result.stdout).strip() or "launchctl bootstrap 失败"
            return True, detail
    else:
        result = _launchctl("kickstart", "-k", f"{domain}/{label}")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or "launchctl kickstart 失败"
            return True, detail
    return True, ""


def _stop_managed_service(spec: dict) -> tuple[bool, str]:
    label = spec.get("launch_agent")
    if not label:
        return False, ""
    if not _launch_agent_loaded(label):
        return True, ""
    result = _launchctl("bootout", f"{_launch_domain()}/{label}")
    if result.returncode != 0 and _launch_agent_loaded(label):
        detail = (result.stderr or result.stdout).strip() or "launchctl bootout 失败"
        return True, detail
    return True, ""


def start_service(service: str) -> dict:
    spec = SERVICES[service]
    if is_running(spec["port"]):
        message = "服务已运行"
        _remember(service, message=message, error_code="", detail="")
        return state(service) | {"message": message}

    command = spec["command"]
    missing = next((item for item in command if not Path(item).exists()), None)
    if missing:
        message = f"启动失败：找不到启动文件 {missing}"
        _remember(service, message=message, error_code="missing_file", detail=message)
        return state(service) | {"message": message, "error_code": "missing_file"}

    managed, launch_error = _start_managed_service(spec)
    if managed:
        if launch_error:
            message = f"启动失败：{launch_error}"
            _remember(service, message=message, error_code="launchctl_failed", detail=launch_error)
            return state(service) | {"message": message, "error_code": "launchctl_failed", "detail": launch_error}
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if is_running(spec["port"]):
                message = "服务已启动"
                _remember(service, message=message, error_code="", detail="")
                return state(service) | {"message": message}
            time.sleep(0.15)
        detail = _tail_log(spec["log"]) or "守护服务未在规定时间内监听端口"
        message = f"启动超时：{detail}"
        _remember(service, message=message, error_code="startup_timeout", detail=detail)
        return state(service) | {"message": message, "error_code": "startup_timeout", "detail": detail}

    _close_log(service)
    log_handle = None
    try:
        log_handle = open(spec["log"], "ab", buffering=0)
        process = subprocess.Popen(
            command,
            cwd=BASE,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        if log_handle is not None:
            log_handle.close()
        message = f"启动失败：{exc}"
        _remember(service, message=message, error_code="spawn_failed", detail=str(exc))
        return state(service) | {"message": message, "error_code": "spawn_failed"}

    _processes[service] = process
    _log_handles[service] = log_handle
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if is_running(spec["port"]):
            message = "服务已启动"
            _remember(service, message=message, error_code="", detail="")
            return state(service) | {"message": message}
        exit_code = process.poll()
        if exit_code is not None:
            detail = _tail_log(spec["log"]) or f"进程已退出（exit code {exit_code}）"
            error_code = "missing_config" if ".env" in detail or "配置" in detail else "process_exit"
            message = f"启动失败：{detail}"
            _remember(service, message=message, error_code=error_code, detail=detail)
            return state(service) | {"message": message, "error_code": error_code, "detail": detail}
        time.sleep(0.15)

    detail = _tail_log(spec["log"]) or "服务未在规定时间内监听端口"
    message = f"启动超时：{detail}"
    _remember(service, message=message, error_code="startup_timeout", detail=detail)
    return state(service) | {"message": message, "error_code": "startup_timeout", "detail": detail}


def _listen_pids(port: int) -> list[int]:
    result = subprocess.run(
        ["/usr/sbin/lsof", "-tiTCP:%d" % port, "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [int(line) for line in result.stdout.splitlines() if line.strip().isdigit()]


def stop_service(service: str) -> dict:
    spec = SERVICES[service]
    if not is_running(spec["port"]):
        message = "服务未运行"
        _remember(service, message=message, error_code="", detail="")
        return state(service) | {"message": message}

    managed, launch_error = _stop_managed_service(spec)
    if managed and launch_error:
        message = f"停止失败：{launch_error}"
        _remember(service, message=message, error_code="launchctl_failed", detail=launch_error)
        return state(service) | {"message": message, "error_code": "launchctl_failed", "detail": launch_error}

    process = _processes.get(service)
    if not managed and process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        for pid in _listen_pids(spec["port"]):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and is_running(spec["port"]):
        time.sleep(0.15)
    running = is_running(spec["port"])
    message = "服务已停止" if not running else "停止超时，请检查服务进程"
    _remember(service, message=message, error_code="" if not running else "stop_timeout", detail="")
    if not running:
        _close_log(service)
    return state(service) | {"message": message}


class Handler(BaseHTTPRequestHandler):
    def _cors(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin == "null" or origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _respond(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        if self.path.startswith("/healthz"):
            self._respond(200, {"ok": True, "services": {key: state(key) for key in SERVICES}})
            return
        if self.path.startswith("/oss-tools"):
            self._respond(200, get_oss_tools())
            return
        self._respond(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if not self.path.startswith("/service"):
            self._respond(404, {"ok": False, "error": "not found"})
            return
        try:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._respond(400, {"ok": False, "error": "Content-Length 无效"})
            if length <= 0 or length > MAX_REQUEST_BYTES:
                return self._respond(413, {"ok": False, "error": "请求体超过 64KB 限制"})
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            service = payload.get("service")
            action = payload.get("action", "toggle")
            if service not in SERVICES or action not in {"start", "stop", "toggle"}:
                raise ValueError("未知服务或操作")
            if action == "toggle":
                action = "stop" if is_running(SERVICES[service]["port"]) else "start"
            with _service_lock:
                result = start_service(service) if action == "start" else stop_service(service)
            failed = action == "start" and not result.get("running")
            self._respond(503 if failed else 200, {"ok": not failed, "service": result})
        except (ValueError, json.JSONDecodeError) as exc:
            self._respond(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # pragma: no cover
            self._respond(500, {"ok": False, "error": f"控制器异常：{exc}"})

    def log_message(self, fmt: str, *args: object) -> None:
        print("[local-tools-control] " + (fmt % args))


if __name__ == "__main__":
    print(f"本地工具开关已启动: http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
