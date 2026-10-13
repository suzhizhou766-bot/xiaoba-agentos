"""3D 导演台模块：提供已并入监看中心的“小八 3D 导演台”（三维摄影预演工作台）。

它是纯前端应用，源码在本目录的「小八3D导演台/」，构建产物在其中的 dist/。本模块只把 dist 里的
文件原样提供在 /stage/ 下：不起额外进程，不占别的端口。文件在每次请求时现找，所以在该目录执行
npm run build 之后刷新页面就是新版本，不需要重启监看中心。

另外提供“本机工程库”（/stage/api/…）：导演台的草稿不再只存在浏览器里，而是存成本目录下的文件，
换浏览器、清浏览器数据都不会丢。模型和图片这类大素材按内容哈希单独存一份，工程文件里只留引用，
同一个素材被多个工程、多次保存使用时磁盘上只有一份。
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from .. import config as cfg
from ..web import Response, Route
from . import Module

DRAFT_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
ASSET_HASH = re.compile(r"^[0-9a-f]{64}$")
ASSET_REF = re.compile(r"xbasset:sha256:([0-9a-f]{64})")
# 素材类型白名单：扩展名 ↔ 浏览器取回时的类型
ASSET_TYPES = {"model/gltf-binary": "glb", "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
               "application/octet-stream": "bin"}
MAX_DRAFT = 32 * 1024 * 1024
MAX_ASSET = 96 * 1024 * 1024
MAX_DRAFTS = 500
MAX_COMPOSE = 512 * 1024          # 一次“写最终提示词”请求的上限
COMPOSE_TIMEOUT = 240             # 等 Key 池里的模型回答的时间（秒）

TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".json": "application/json; charset=utf-8", ".glb": "model/gltf-binary", ".gltf": "model/gltf+json",
         ".bin": "application/octet-stream", ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
         ".ico": "image/x-icon", ".woff2": "font/woff2", ".wasm": "application/wasm", ".txt": "text/plain; charset=utf-8",
         ".md": "text/plain; charset=utf-8"}


class StageModule(Module):
    id, title, kind = "stage", "3D 导演台", "view"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.dir = cfg.resolve(ctx.config, ctx.config["stage"]["dir"])
        self.library = cfg.resolve(ctx.config, ctx.config["stage"]["library_dir"])
        self.lock = threading.Lock()
        keypool = ctx.config.get("keypool") or {}
        self.keypool_file = cfg.resolve(ctx.config, keypool.get("file", "keypool.json"))
        self.keypool_url = "http://127.0.0.1:%d" % int(keypool.get("port", 8780))
        self.compose_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open  # 只连本机，不走系统代理
        # 与监看页面同源（浏览器里的草稿稳定），但只能加载和连接自己 /stage/ 下的东西：
        # 连不了裁决、Key 池等接口，也不能再嵌套别的页面。
        own = "http://127.0.0.1:%d/stage/" % ctx.port
        self.csp = ("default-src 'none'; script-src %s 'wasm-unsafe-eval' blob:; style-src %s 'unsafe-inline'; "
                    "img-src %s data: blob:; media-src %s data: blob:; font-src %s data:; worker-src %s blob:; "
                    "connect-src %s data: blob:; frame-ancestors 'self'; frame-src 'none'; base-uri 'none'; form-action 'none'"
                    % ((own,) * 7))

    def routes(self):
        return [Route("GET", "/stage", self.page, "page"),
                Route("GET", "/stage/", self.file, "page", prefix=True),
                # 本机工程库：读取只挡跨站；写入要求来自本机监看页面（Origin 校验）
                Route("GET", "/stage/api/library", self.library_list, "read"),
                Route("GET", "/stage/api/draft", self.draft_get, "read"),
                Route("POST", "/stage/api/draft", self.draft_put, "post", max_body=MAX_DRAFT),
                Route("POST", "/stage/api/draft/delete", self.draft_delete, "post"),
                Route("POST", "/stage/api/assets/missing", self.assets_missing, "post"),
                Route("GET", "/stage/api/asset", self.asset_get, "read"),
                Route("POST", "/stage/api/asset", self.asset_put, "post", max_body=MAX_ASSET),
                # 用 Key 池里的模型写最终提示词：合并 Key 只在后台用，页面拿不到
                Route("GET", "/stage/api/compose", self.compose_info, "read"),
                Route("POST", "/stage/api/compose", self.compose, "post", max_body=MAX_COMPOSE)]

    # ---- 用 Key 池模型写最终提示词 ----
    def _keypool(self):
        try:
            data = json.loads(self.keypool_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, []
        key = data.get("masterKey")
        key = key[0] if isinstance(key, list) and key else key
        nodes = [n for n in data.get("nodes") or [] if isinstance(n, dict) and n.get("enabled", True) is not False
                 and n.get("baseUrl") and (n.get("key") or n.get("payKey"))]
        return (key if isinstance(key, str) and key else None), nodes

    def compose_info(self, req):
        """页面用来显示“会由哪个模型来写”。只给模型名，不给任何 Key。"""
        key, nodes = self._keypool()
        if not key or not nodes:
            return 200, {"available": False, "models": [], "followsClient": False,
                         "reason": "Key 池里还没有可用的节点"}
        models = sorted({n["model"] for n in nodes if n.get("model")})
        return 200, {"available": True, "models": models, "followsClient": any(not n.get("model") for n in nodes)}

    def compose(self, req):
        body = req.json()
        messages = body.get("messages")
        if not (isinstance(messages, list) and 1 <= len(messages) <= 8 and all(
                isinstance(m, dict) and m.get("role") in ("system", "user", "assistant")
                and isinstance(m.get("content"), str) and m["content"].strip() for m in messages)):
            raise ValueError("请求内容不对")
        model = str(body.get("model") or "").strip()
        if not model or len(model) > 160 or any(ord(ch) < 32 for ch in model):
            raise ValueError("缺少模型名称")
        key, nodes = self._keypool()
        if not key or not nodes:
            return 200, {"ok": False, "error": "Key 池里还没有可用的节点"}
        payload = json.dumps({"model": model, "stream": False, "temperature": 0.4,
                              "messages": [{"role": m["role"], "content": m["content"]} for m in messages]},
                             ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.keypool_url + "/v1/chat/completions", data=payload, method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        started = time.time()
        try:
            with self.compose_open(request, timeout=COMPOSE_TIMEOUT) as response:
                answer = json.loads(response.read(4 * 1024 * 1024).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", "replace")
            self.ctx.log(self.id, "compose failed", "error", status=exc.code)
            return 200, {"ok": False, "error": "Key 池返回 HTTP %d：%s" % (exc.code, detail[:300])}
        except Exception as exc:
            self.ctx.log(self.id, "compose failed", "error", error=str(exc)[:200])
            return 200, {"ok": False, "error": "Key 池没有响应（可能超时）：%s" % (getattr(exc, "reason", None) or exc)}
        ms = int((time.time() - started) * 1000)
        try:
            text = answer["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            text = ""
        if not isinstance(text, str) or not text.strip():
            return 200, {"ok": False, "error": "模型没有返回内容"}
        self.ctx.log(self.id, "compose done", ms=ms, model=str(answer.get("model") or model)[:80], chars=len(text))
        return 200, {"ok": True, "text": text.strip(), "model": str(answer.get("model") or model), "ms": ms,
                     "usage": answer.get("usage") if isinstance(answer.get("usage"), dict) else None}

    # ---- 本机工程库 ----
    def _draft_path(self, draft_id):
        if not isinstance(draft_id, str) or not DRAFT_ID.match(draft_id):
            raise ValueError("草稿编号无效")
        return self.library / "drafts" / (draft_id + ".json")

    def _asset_file(self, digest):
        if not isinstance(digest, str) or not ASSET_HASH.match(digest):
            raise ValueError("素材编号无效")
        folder = self.library / "assets"
        return next((p for p in folder.glob(digest + ".*") if p.suffix[1:] in ASSET_TYPES.values()), None) if folder.is_dir() else None

    @staticmethod
    def _write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
        tmp.write_bytes(data)
        os.replace(str(tmp), str(path))

    def _records(self):
        folder = self.library / "drafts"
        for path in (folder.glob("*.json") if folder.is_dir() else []):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(record, dict) and isinstance(record.get("project"), dict):
                    yield path, record
            except (OSError, ValueError):
                continue

    def library_list(self, req):
        drafts = [{"id": r.get("id"), "name": r.get("name") or "未命名项目", "updatedAt": r.get("updatedAt") or 0,
                   "shotCount": len(r["project"].get("shots") or [])} for _, r in self._records()]
        drafts.sort(key=lambda d: -d["updatedAt"])
        return 200, {"ok": True, "drafts": drafts}

    def draft_get(self, req):
        path = self._draft_path(req.param("id"))
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 200, {"ok": True, "record": None}
        except (OSError, ValueError):
            return 200, {"ok": False, "error": "这份草稿的文件已损坏，无法读取"}
        return 200, {"ok": True, "record": record}

    def draft_put(self, req):
        body = req.json()
        path = self._draft_path(body.get("id"))
        project, name = body.get("project"), body.get("name")
        if not isinstance(project, dict) or not isinstance(project.get("shots"), list):
            raise ValueError("工程内容无效")
        if not isinstance(name, str) or len(name) > 200:
            raise ValueError("工程名称无效")
        owner = body.get("sessionOwner")
        text = json.dumps(project, ensure_ascii=False)
        missing = sorted({h for h in ASSET_REF.findall(text) if self._asset_file(h) is None})
        if missing:  # 引用了还没上传的素材：拒绝保存，免得存下一份打不开的工程
            return 409, {"ok": False, "error": "有素材还没有保存到本机，请重试", "missing": missing}
        with self.lock:
            folder = self.library / "drafts"
            if not path.exists() and folder.is_dir() and sum(1 for _ in folder.glob("*.json")) >= MAX_DRAFTS:
                return 400, {"ok": False, "error": "草稿数量已到上限（%d），请先删除一些" % MAX_DRAFTS}
            updated = int(time.time() * 1000)
            record = {"id": body["id"], "name": name or "未命名项目", "updatedAt": updated, "project": project,
                      "sessionOwner": owner if isinstance(owner, str) and len(owner) <= 80 else None}
            self._write(path, json.dumps(record, ensure_ascii=False).encode("utf-8"))
        return 200, {"ok": True, "updatedAt": updated}

    def draft_delete(self, req):
        path = self._draft_path(req.json().get("id"))
        with self.lock:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            removed = self._collect_assets()
        self.ctx.log(self.id, "draft deleted", draft=path.stem, assets_removed=removed)
        return 200, {"ok": True, "assetsRemoved": removed}

    def _collect_assets(self):
        """删掉没有任何草稿再引用的素材。只在删除草稿时做；调用方持有锁。"""
        folder = self.library / "assets"
        if not folder.is_dir():
            return 0
        used = set()
        for path, _ in self._records():
            used.update(ASSET_REF.findall(path.read_text(encoding="utf-8")))
        removed = 0
        for path in folder.iterdir():
            # 刚上传、还没来得及被草稿引用的素材留十分钟宽限
            if path.is_file() and ASSET_HASH.match(path.stem) and path.stem not in used and time.time() - path.stat().st_mtime > 600:
                path.unlink()
                removed += 1
        return removed

    def assets_missing(self, req):
        hashes = req.json().get("hashes")
        if not isinstance(hashes, list) or len(hashes) > 5000:
            raise ValueError("素材清单无效")
        return 200, {"ok": True, "missing": [h for h in hashes if self._asset_file(h) is None]}

    def asset_put(self, req):
        digest, kind = req.param("h"), req.param("type")
        if kind not in ASSET_TYPES:
            raise ValueError("不支持的素材类型")
        if not ASSET_HASH.match(digest or ""):
            raise ValueError("素材编号无效")
        data = req.body()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("素材内容与编号不符，上传可能不完整")
        with self.lock:
            if self._asset_file(digest) is None:
                self._write(self.library / "assets" / ("%s.%s" % (digest, ASSET_TYPES[kind])), data)
        return 200, {"ok": True}

    def asset_get(self, req):
        path = self._asset_file(req.param("h"))
        if path is None:
            return Response(404, "找不到这个素材".encode("utf-8"), "text/plain; charset=utf-8")
        kind = next(k for k, ext in ASSET_TYPES.items() if ext == path.suffix[1:])
        return Response(200, path.read_bytes(), kind, csp=self.csp)

    def _resolve(self, url_path):
        """把 /stage/ 下的地址对应到 dist 里的文件；只认 dist 内部的普通文件，隐藏文件和越出目录的一律不认。"""
        relative = url_path[len("/stage/"):] or "index.html"
        parts = relative.split("/")
        if any(not part or part.startswith(".") or "\\" in part or "\x00" in part for part in parts):
            return None
        root = self.dir.resolve()
        path = root.joinpath(*parts).resolve()
        if root != path and root not in path.parents:
            return None
        return path if path.is_file() else None

    def health(self):
        if not (self.dir / "index.html").is_file():
            return {"status": "failed", "detail": "找不到 3D 导演台的构建产物：%s（在「小八3D导演台」目录执行 npm run build）" % self.dir}
        return {"status": "ok", "detail": "页面就绪"}

    def _send(self, path):
        try:
            body = path.read_bytes()
        except OSError:
            return Response(404, "找不到 3D 导演台文件，请重新构建后重启监看中心".encode("utf-8"), "text/plain; charset=utf-8")
        return Response(200, body, TYPES.get(path.suffix.lower(), "application/octet-stream"), csp=self.csp)

    def page(self, req):
        path = self._resolve("/stage/")
        if path is None:
            return Response(404, ("找不到 3D 导演台的构建产物：%s" % self.dir).encode("utf-8"), "text/plain; charset=utf-8")
        return self._send(path)

    def file(self, req):
        path = self._resolve(req.path)
        if path is None:
            return Response(404, "找不到这个文件".encode("utf-8"), "text/plain; charset=utf-8")
        return self._send(path)
