"""裁决模块：监看中心自己拥有并看护裁决后端，页面只和监看中心打交道。

裁决后端就是小八AgentOS 自带、经过验收的裁决中心代码（integrations/review-center/server.py），
经 monitor_core/review_backend.py 启动。这里不重写它的任何逻辑：候选版本比对、证据哈希校验、
调用已校验的 AgentOS CLI 写审核、发布与索引校验、待审队列同步到第二大脑，全部仍由它完成，
它自己的来源与 csrf 校验也原样保留。

另有一组“待核对的保留文件”接口（v1.5.5 起）：经验发布失败时，小八把被换下来的原文件留在原处等用户核对。
这里只做两件事：把已安装入口 `xiaoba memory-retained-list` 的结果转给页面；用户在页面上点了某一个文件之后，
对这一个文件调用 `xiaoba memory-retained-resolve`。是否处理、处理哪一个，始终由用户在页面上逐个决定，
监看中心不会自己发起，也不会批量处理。

监看中心负责它的生命周期：
  - 监看中心启动时把它拉起来并持续看护（退出自动重启，反复失败则熔断报障）；
  - 它只监听本机一个每次临时分配的内部端口，没有自己的网页；旧的 8797 地址不再存在；
  - 监看中心被强行结束后重新起来时，会认出并重新接管上一次留下的后端；
  - 监看中心关闭时，后端一并关闭。
兼容：如果旧的独立裁决服务（legacy_port，默认 8797）又被别人启动了，就直接使用它，不重复拉起，
避免两个后端同时改写第二大脑的待审队列。
"""
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
from pathlib import Path

from .. import config as cfg
from ..supervise import Backoff, Breaker, ManagedProcess
from ..web import Route
from . import Module

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


# 保留文件：命令行给出的英文原因，换成给人看的说法；认不出的原样显示
RETAINED_REASONS = (
    ("created the file", "这次发布新建的文件，发布失败后已从原位置撤下"),
    ("staged the file", "这次命令暂存的证据文件，失败后已从原位置撤下"),
    ("newer writer took the live path", "原位置已被别的写入占用，旧内容没有放回"),
    ("not this call's own write", "这个文件不是本次发布写入的，没有放回"),
    ("could not be put back", "旧内容放回原位置失败"),
    ("previous bytes were put back", "旧内容已放回原位置，这是被替换下来的那一份"),
)
RETAINED_OPERATIONS = {"memory.note": "经验笔记", "memory.catalog": "经验目录", "memory.index": "检索索引",
                       "evidence": "暂存证据", "constraint-hold-release": "暂存证据"}
RETAINED_ACTIONS = {"trash": "--trash", "confirm_deleted": "--confirm-deleted"}


def short(value, limit=160):
    text = str(value or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ReviewModule(Module):
    id, title, kind = "review", "裁决中心", "view"

    def __init__(self, ctx, manage=True):
        super().__init__(ctx)
        settings = ctx.config["review"]
        self.legacy_port = int(settings["port"])      # 旧独立服务的端口，只用于发现“别人又把它启动了”
        self.port = self.legacy_port                  # 当前实际通信的端口；自己拉起时改为临时分配的内部端口
        self.project = Path(ctx.config["agentos_project"])
        self.script = self.project / settings["script"]
        self.launcher = Path(__file__).resolve().parent.parent / "review_backend.py"
        self.python = settings["python"]
        self.start_timeout = int(settings["start_timeout_seconds"])
        self.config_path = cfg.agentos_config_path(ctx.config)
        # 已安装并自带发布校验的小八入口；保留文件的列出与处理只经过它
        self.cli = Path(settings.get("cli") or (Path.home() / ".agents/skills/xiaoba-agentos/scripts/xiaoba"))
        self._retained_lock = threading.Lock()
        self.public_url = "http://127.0.0.1:%d" % ctx.port
        self.manage = manage          # 测试时可关掉，只连接已有后端
        # idle / external（别人启动的旧独立服务，只使用不管理）/ managed（本次由监看中心拉起）
        # / adopted（监看中心上一次运行时拉起、重启后重新接管的）
        self.mode = "idle"
        self.process = None
        self.adopted_pid = None
        self.state_file = cfg.resolve(ctx.config, "run") / "review-backend.json"
        self._stop = threading.Event()
        self._thread = None

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    # ---- 生命周期 ----
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._keep, name="review-keeper", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self.process:
            self.process.stop()
        if self.mode == "adopted" and self.adopted_pid:
            try:
                os.kill(self.adopted_pid, signal.SIGTERM)
            except OSError:
                pass
        if self.mode in ("managed", "adopted"):
            try:
                self.state_file.unlink()
            except OSError:
                pass

    @staticmethod
    def _free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def _listener_pid(self, port):
        try:
            out = subprocess.run(["/usr/sbin/lsof", "-nP", "-tiTCP:%d" % port, "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=5).stdout.split()
            return int(out[0]) if out else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def _previous_backend(self):
        """上一次运行时由监看中心拉起、现在仍在监听的后端：返回 (pid, port)，没有则 None。"""
        try:
            record = json.loads(self.state_file.read_text(encoding="utf-8"))
            pid, port = int(record["pid"]), int(record["port"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return (pid, port) if pid == self._listener_pid(port) else None

    def _record_backend(self):
        # 等后端真正起来后记下它的进程号与端口，供监看中心重启后认领
        for _ in range(self.start_timeout):
            if self._stop.is_set() or not self.process:
                return
            status = self.process.status()
            if status["pid"] and status["state"] == "running":
                try:
                    self.state_file.parent.mkdir(parents=True, exist_ok=True)
                    self.state_file.write_text(json.dumps({"pid": status["pid"], "port": self.port}), encoding="utf-8")
                except OSError:
                    pass
                return
            time.sleep(1)

    def _alive(self, port=None):
        if port is None:
            code, data = self.call("/api/state", timeout=4)
        else:
            code, data = self.call("/api/state", timeout=4, port=port)
        return code == 200 and isinstance(data, dict) and bool(data.get("ok"))

    def _spawn(self):
        watchdog = self.ctx.config["watchdog"]
        env = dict(os.environ)
        env.update({"XIAOBA_CONFIG": str(self.config_path), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"})
        log_dir = cfg.resolve(self.ctx.config, self.ctx.config["log"]["dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        self.port = self._free_port()
        self.process = ManagedProcess(
            "review-backend",
            [self.python, str(self.launcher), "--server-script", str(self.script), "--port", str(self.port),
             "--config", str(self.config_path), "--public-url", self.public_url],
            self.ctx.logger, env=env, cwd=str(Path.home()),
            ready=self._alive, ready_timeout=self.start_timeout,
            backoff=Backoff(watchdog["backoff_seconds"]),
            breaker=Breaker(watchdog["max_fast_failures"], watchdog["fast_failure_seconds"]),
            output_path=str(log_dir / "review-backend.log"))
        self.process.start()
        self.mode = "managed"
        self.ctx.log(self.id, "backend managed by monitor", port=self.port)
        threading.Thread(target=self._record_backend, name="review-pid", daemon=True).start()

    def _keep(self):
        """决定后端从哪来：认领自己上次拉起的 → 使用别人启动的旧独立服务 → 自己拉起。"""
        while not self._stop.is_set():
            if self.mode == "adopted":
                if not self._alive():
                    self.mode, self.adopted_pid = "idle", None
            elif self.mode == "external":
                if not self._alive():
                    self.mode = "idle"
            if self.mode == "idle":
                previous = self._previous_backend()
                if previous and self._alive(previous[1]):
                    self.adopted_pid, self.port = previous
                    self.mode = "adopted"
                    self.ctx.log(self.id, "re-adopted backend started by a previous run", pid=previous[0], port=previous[1])
                elif self._alive(self.legacy_port):
                    self.port, self.mode = self.legacy_port, "external"
                    self.ctx.log(self.id, "legacy standalone review service is running; using it", "warning", port=self.port)
                elif self.manage:
                    if not self.script.is_file():
                        self.ctx.log(self.id, "backend script missing", "error", script=str(self.script))
                    else:
                        self._spawn()
            self._stop.wait(10)

    def reset(self):
        if self.process:
            self.process.reset()

    def health(self):
        if self.mode == "external":
            return {"status": "warn", "mode": "external",
                    "detail": "旧的独立裁决服务（端口 %d）又在运行，当前使用它；停用它后监看中心会自动接管" % self.port}
        if self.mode == "adopted":
            return {"status": "ok", "mode": "managed", "detail": "由监看中心运行（重启后重新接管）", "pid": self.adopted_pid}
        if self.mode == "managed" and self.process:
            status = self.process.status()
            state = status["state"]
            mapped = {"running": "ok", "starting": "starting", "backoff": "warn", "failed": "failed"}.get(state, "warn")
            detail = {"running": "由监看中心运行", "starting": "正在启动（发布校验与第二大脑同步，约需几十秒）",
                      "backoff": "已退出，等待自动重启", "failed": status["error"] or "已停止自动重启"}.get(state, state)
            return {"status": mapped, "mode": "managed", "detail": detail, "pid": status["pid"],
                    "restarts": status["restarts"]}
        if not self.script.is_file():
            return {"status": "failed", "mode": self.mode, "detail": "找不到裁决后端：%s" % self.script}
        return {"status": "starting", "mode": self.mode, "detail": "正在连接裁决后端"}

    # ---- 与后端通信 ----
    def call(self, path, body=None, csrf=None, timeout=10, port=None):
        base = self.base if port is None else "http://127.0.0.1:%d" % port
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers.update({"Content-Type": "application/json", "Origin": base, "X-Review-CSRF": csrf or ""})
        request = urllib.request.Request(base + path, data=data, headers=headers,
                                         method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                payload = {"ok": False, "error": "裁决后端返回 HTTP %d" % exc.code}
            return exc.code, payload
        except Exception as exc:
            return 502, {"ok": False, "error": self._unavailable(exc)}

    def _unavailable(self, exc=None):
        health = self.health()
        if health["status"] in ("starting", "warn", "failed"):
            return "裁决后端暂不可用：%s" % health["detail"]
        return "裁决后端没有响应：%s" % short(exc, 120)

    def _post(self, path, body, timeout):
        code, state = self.call("/api/state")
        csrf = state.get("csrf") if isinstance(state, dict) else None
        if code != 200 or not csrf:
            return 502, {"ok": False, "error": "无法连接裁决后端，未提交任何内容"}
        return self.call(path, body=body, csrf=csrf, timeout=timeout)

    def state(self):
        code, data = self.call("/api/state")
        if isinstance(data, dict):
            data.pop("csrf", None)  # 后端的 csrf 只在服务端使用，不下发给浏览器
        return code, data

    # ---- 路由 ----
    def routes(self):
        return [
            Route("GET", "/api/review/state", lambda req: self.state(), "read"),
            Route("GET", "/api/review/candidate", self.candidate, "read"),
            Route("POST", "/api/review/decisions", self.decisions, "write"),
            Route("POST", "/api/review/sync", self.sync, "write"),
            Route("GET", "/api/review/retained", self.retained, "read"),
            Route("POST", "/api/review/retained/resolve", self.retained_resolve, "write"),
        ]

    def candidate(self, req):
        task_id = req.param("id")
        if not UUID_RE.match(task_id):
            return 400, {"ok": False, "error": "候选编号无效"}
        return self.call("/api/candidate?id=" + task_id)

    def decisions(self, req):
        body = req.json()
        items = body.get("items") if isinstance(body.get("items"), list) else []
        # 每条裁决最长约 4 分钟（发布与索引校验），等待上限随条数放宽
        return self._post("/api/decisions", body, timeout=120 + 260 * max(1, min(len(items), 100)))

    def sync(self, req):
        req.json()
        return self._post("/api/sync", {}, timeout=180)

    # ---- 待核对的保留文件 ----
    def _cli(self, args, timeout=90):
        """调用已安装的小八入口，返回 (退出码, 解析后的 JSON 或 None, 出错说明)。"""
        if not self.cli.is_file():
            return None, None, "找不到已安装的小八入口：%s" % self.cli
        try:
            done = subprocess.run([str(self.cli)] + list(args), capture_output=True, text=True,
                                  timeout=timeout, cwd=str(Path.home()))
        except subprocess.TimeoutExpired:
            return None, None, "小八入口在 %d 秒内没有返回" % timeout
        except OSError as exc:
            return None, None, "无法运行小八入口：%s" % short(exc, 120)
        try:
            data = json.loads(done.stdout)
        except ValueError:
            return done.returncode, None, short(done.stderr or done.stdout or "命令没有返回内容", 300)
        return done.returncode, (data if isinstance(data, dict) else None), ""

    def _retained_raw(self):
        code, data, error = self._cli(["memory-retained-list"])
        if data is None or not isinstance(data.get("objects"), list):
            return None, error or "当前版本的小八没有保留文件清单（需要 v1.5.5 或更新）"
        return data, ""

    @staticmethod
    def _retained_item(obj):
        reason = str(obj.get("reason") or "")
        label = next((text for key, text in RETAINED_REASONS if key in reason), reason or "未记录原因")
        missing = obj.get("state") == "record_without_object"
        return {
            "path": obj.get("retained_path"), "originalPath": obj.get("original_path"),
            "exists": not missing,
            "operation": RETAINED_OPERATIONS.get(str(obj.get("operation")), str(obj.get("operation") or "未知")),
            "reason": label, "task": str(obj.get("task_id") or "")[:8], "recordedAt": obj.get("recorded_at"),
            "sizeBytes": obj.get("size_bytes"), "readable": bool(obj.get("readable", True)),
            "problems": [short(p, 200) for p in (obj.get("problems") or [])][:5],
        }

    @staticmethod
    def _pending(obj):
        return isinstance(obj, dict) and obj.get("retained_path") and obj.get("manual_verification", "pending") == "pending"

    def retained(self, req):
        data, error = self._retained_raw()
        if data is None:
            return 502, {"ok": False, "error": error}
        items = [self._retained_item(o) for o in data["objects"] if self._pending(o)]
        return 200, {
            "ok": True, "checkedAt": data.get("checked_at"), "items": items, "pending": len(items),
            "enumerationOk": bool(data.get("enumeration_ok", False)),
            "anomalies": [short(a.get("detail") or a.get("kind"), 200) if isinstance(a, dict) else short(a, 200)
                          for a in (data.get("anomalies") or [])][:10],
            "problems": [short(p, 200) for p in (data.get("problems") or [])][:10],
        }

    def retained_resolve(self, req):
        body = req.json()
        path, action = body.get("path"), body.get("action")
        if action not in RETAINED_ACTIONS or not isinstance(path, str) or not path:
            return 400, {"ok": False, "error": "请求无效：需要指明一个保留文件和处理方式"}
        with self._retained_lock:   # 一次只处理一个，前一个没结束不接下一个
            data, error = self._retained_raw()
            if data is None:
                return 502, {"ok": False, "error": error}
            # 只接受清单里此刻确实列着、且仍待核对的那一个；页面传来的路径不直接交给命令行
            target = next((o for o in data["objects"] if self._pending(o) and o.get("retained_path") == path), None)
            if target is None:
                return 409, {"ok": False, "error": "这个文件已不在待核对清单里，请刷新后再看"}
            args = ["memory-retained-resolve", RETAINED_ACTIONS[action]]
            if action == "trash" and target.get("dev") is not None and target.get("ino") is not None:
                # 带上清单里看到的文件身份：这期间文件若被换成了别的，命令行会拒绝
                args += ["--expect-dev", str(target["dev"]), "--expect-ino", str(target["ino"])]
            code, result, error = self._cli(args + [path], timeout=120)
            ok = code == 0 and isinstance(result, dict) and result.get("ok") is not False
            self.ctx.log(self.id, "retained object resolved by user" if ok else "retained object resolve refused",
                         "info" if ok else "warning", action=action, name=Path(path).name)
            if ok:
                return 200, {"ok": True, "action": action, "trashPath": result.get("trash_path")}
            detail = error
            if isinstance(result, dict):
                issues = result.get("issues") if isinstance(result.get("issues"), list) else []
                detail = "；".join(str(p) for p in issues[:3]) or result.get("error") or result.get("status") or detail
            return 409, {"ok": False, "error": "没有处理：%s" % short(detail or "命令行拒绝了这次操作", 300)}

    # ---- 给运行总览用的摘要：只有标题、阶段、时间，不带正文与哈希 ----
    def queue_summary(self, ts_text=str):
        out = {"online": False, "items": [], "pending": 0, "byStage": {}, "recentDecisions": []}
        health = self.health()
        out["mode"], out["backend"] = health.get("mode"), health.get("status")
        code, data = self.call("/api/state", timeout=2)
        if code != 200 or not isinstance(data, dict):
            out["error"] = short(data.get("error") if isinstance(data, dict) else "", 120)
            out["starting"] = health.get("status") == "starting"
            return out
        out["online"] = True
        stages = {}
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            stage = item.get("stage") or "?"
            stages[stage] = stages.get(stage, 0) + 1
            out["items"].append({"id": str(item.get("task_id") or "")[:8], "fullId": item.get("task_id"),
                                 "title": short(item.get("title") or item.get("prompt"), 60), "stage": item.get("stage"),
                                 "status": item.get("status"), "statusLabel": item.get("status_label"),
                                 "createdAt": ts_text(item.get("created_at")), "updatedAt": ts_text(item.get("updated_at"))})
        out["pending"], out["byStage"] = len(out["items"]), stages
        reviews = [r for r in (data.get("reviews") or []) if isinstance(r, dict)]
        reviews.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
        out["recentDecisions"] = [{"task": str(r.get("task_id") or "")[:8], "stage": r.get("stage"),
                                   "decision": r.get("decision"), "at": ts_text(r.get("created_at"))} for r in reviews[:5]]
        sync = data.get("sync") if isinstance(data.get("sync"), dict) else {}
        out.update({"total": data.get("total"), "holds": data.get("holds"), "release": data.get("release"),
                    "updated": data.get("updated"), "syncState": sync.get("state"), "syncAt": sync.get("at")})
        return out
