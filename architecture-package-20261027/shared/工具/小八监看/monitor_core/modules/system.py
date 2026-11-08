"""系统模块：主页面、存活探针、各模块健康状况。"""
import html
import subprocess
import time

from .. import VERSION, config as cfg
from ..web import Response, Route
from . import Module


class SystemModule(Module):
    id, title, kind = "system", "系统", "service"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.page = cfg.resolve(ctx.config, ctx.config["page_file"])
        self.revision = self._revision()
        self.warnings = []   # 最近一次读到的模块警告，正文提示条用它（不是只在标签悬浮提示里）

    def _revision(self):
        try:
            out = subprocess.run(["/usr/bin/git", "-C", self.ctx.config["root"], "rev-parse", "--short", "HEAD"],
                                 capture_output=True, text=True, timeout=3)
            return out.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None

    def routes(self):
        return [
            Route("GET", "/", self.index, "page"),
            Route("GET", "/index.html", self.index, "page"),
            Route("GET", "/healthz", self.healthz, "page"),
            Route("GET", "/api/health", self.api_health, "read"),
            Route("GET", "/api/modules", self.api_modules, "read"),
        ]

    def index(self, req):
        ctx = self.ctx
        if req.headers.get("Host") == "localhost:%d" % ctx.port:
            # 统一用 127.0.0.1 这一个地址：浏览器按地址分开保存数据，导演台的方案库才不会分成两份
            return Response(302, b"", headers={"Location": "http://127.0.0.1:%d/" % ctx.port})
        try:
            page = self.page.read_text(encoding="utf-8")
        except OSError:
            return Response(404, ("找不到页面文件：%s" % self.page).encode("utf-8"), "text/plain; charset=utf-8")
        doc = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
               '<meta name="viewport" content="width=device-width,initial-scale=1">'
               '<meta name="xiaoba-page-token" content="%s"></head><body>' % ctx.page_token
               + self._banner() + page + "</body></html>")
        return Response(200, doc.encode("utf-8"), "text/html; charset=utf-8")

    def _banner(self):
        """模块自己报的警告（例如端口监听检测不可用）写进正文里的一条提示：光有标签上的悬浮提示不够。
        还没读到过警告时也留好这个位置，页面脚本会接着更新它。"""
        if not self.warnings:
            return '<p class="rc-notice crit" id="pageWarn" role="alert" hidden></p>'
        text = "；".join("%s：%s" % (html.escape(w["title"]), html.escape(w["text"])) for w in self.warnings)
        return '<p class="rc-notice crit" id="pageWarn" role="alert">⚠️ %s</p>' % text

    def healthz(self, req):
        # 看护进程用它判断监看中心是否还在响应；不含任何业务数据
        return 200, {"ok": True}

    def _modules(self):
        items = []
        for module in self.ctx.modules.values():
            try:
                health = module.health() or {}
            except Exception as exc:
                health = {"status": "failed", "detail": str(exc)}
            items.append({"id": module.id, "title": module.title, "kind": module.kind, **health})
        return items

    def api_modules(self, req):
        return 200, {"ok": True, "modules": self._modules()}

    def api_health(self, req):
        modules = self._modules()
        self.warnings = [{"id": m["id"], "title": m.get("title") or m["id"], "text": m["warning"]}
                         for m in modules if m.get("warning")]
        worst = "ok"
        for item in modules:
            status = item.get("status")
            if status == "failed":
                worst = "failed"
            elif status in ("warn", "starting") and worst == "ok":
                worst = status
        return 200, {"ok": worst != "failed", "status": worst, "version": VERSION, "revision": self.revision,
                     "uptimeSec": int(time.time() - self.ctx.started_at), "modules": modules}
