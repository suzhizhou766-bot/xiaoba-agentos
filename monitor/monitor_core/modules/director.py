"""导演台模块：提供已并入监看中心的“小八提示词导演台”页面，以及旧入口的一次性数据迁移。"""
import json
import secrets
import threading

from .. import config as cfg
from ..web import Response, Route, error
from . import Module

MIGRATION_MAX_BODY = 8 * 1024 * 1024


class DirectorModule(Module):
    id, title, kind = "director", "导演台", "view"

    def __init__(self, ctx):
        super().__init__(ctx)
        settings = ctx.config["director"]
        self.file = cfg.resolve(ctx.config, settings["file"])
        self.token_file = cfg.resolve(ctx.config, settings["migration_token_file"])
        ports = " ".join("http://127.0.0.1:%d" % int(port) for port in settings["service_ports"])
        # 导演台与监看页面同源（本地存储稳定），但它只能连自己的接口与本地工具服务：
        # 连不了裁决接口，也不能再嵌套别的页面；提交裁决的令牌只在监看页面脚本的闭包里。
        self.csp = ("frame-ancestors 'self'; frame-src 'none'; connect-src http://127.0.0.1:%d/director/ %s data: blob:"
                    % (ctx.port, ports))
        self.pending = []
        self.lock = threading.Lock()

    def routes(self):
        return [
            Route("GET", "/director", self.page, "page"),
            Route("GET", "/director/", self.page, "page"),
            Route("GET", "/director/migration", self.migration_pending, "read"),
            Route("POST", "/director/migration", self.migration_receive, "open", max_body=MIGRATION_MAX_BODY),
            Route("POST", "/director/migration/ack", self.migration_ack, "post"),
        ]

    def health(self):
        if not self.file.is_file():
            return {"status": "failed", "detail": "找不到导演台页面：%s" % self.file}
        return {"status": "ok", "detail": "页面就绪；本地工具服务由导演台里的开关控制"}

    def page(self, req):
        try:
            body = self.file.read_bytes()
        except OSError:
            return Response(404, ("找不到导演台文件：%s" % self.file).encode("utf-8"), "text/plain; charset=utf-8")
        return Response(200, body, "text/html; charset=utf-8", csp=self.csp)

    # ---- 旧入口（file://）一次性迁移：令牌文件非空时才接受；数据只在内存里过手，不写磁盘 ----
    def _expected_token(self):
        try:
            return self.token_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def migration_receive(self, req):
        cors = {"Access-Control-Allow-Origin": "*"}
        try:
            body = req.json()
            token = str(body.get("token") or "")
            autosave, schemes = body.get("autosave"), body.get("schemes")
            if (autosave is not None and not isinstance(autosave, str)) or (schemes is not None and not isinstance(schemes, str)):
                raise ValueError
            parsed = json.loads(schemes) if schemes else []
            count = len(parsed) if isinstance(parsed, list) else 0
        except (ValueError, UnicodeDecodeError, AttributeError):
            return error(400, "迁移数据无法识别", cors)
        expected = self._expected_token()
        if not expected or not secrets.compare_digest(token, expected):
            return error(403, "迁移入口已关闭或令牌不符", cors)
        entry = {"id": secrets.token_hex(8), "autosave": autosave, "schemes": schemes, "count": count}
        with self.lock:
            self.pending.append(entry)
        self.ctx.log(self.id, "migration received", id=entry["id"], schemes=count, autosave=bool(autosave))
        return Response(200, json.dumps({"ok": True, "schemes": count, "autosave": bool(autosave)}).encode("utf-8"), headers=cors)

    def migration_pending(self, req):
        with self.lock:
            return 200, {"ok": True, "pending": [dict(item) for item in self.pending]}

    def migration_ack(self, req):
        body = req.json()
        ids = set(str(item) for item in (body.get("ids") or []))
        with self.lock:
            self.pending = [item for item in self.pending if item["id"] not in ids]
        self.ctx.log(self.id, "migration merged", ids=sorted(ids), added=body.get("added"), renamed=body.get("renamed"),
                     skipped=body.get("skipped"), sessions=body.get("sessions"), total=body.get("total"))
        return 200, {"ok": True}
