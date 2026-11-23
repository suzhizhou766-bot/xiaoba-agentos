"""任务卡模块：给“任务卡”页提供发卡台账的只读视图。

页面只读：列出任务卡目录里的卡、每张卡的状态（发了没有、发给谁、回执到了没有、核对了没有），
以及卡和回执的原文。登记发卡、回执、核对由宿主 Agent 用 tools/taskcard.py 写进台账，
页面每 10 秒刷新一次就能看到。这里不改任何文件、不调用小八入口、不碰数据库。
"""
import threading
import time

from .. import config as cfg, taskcards
from ..web import Route, error, json_response
from . import Module

CACHE_SECONDS = 3


class TaskCardsModule(Module):
    id, title, kind = "taskcards", "任务卡", "view"

    def __init__(self, ctx):
        super().__init__(ctx)
        settings = ctx.config.get("taskcards") or {}
        dirs = [str(cfg.resolve(ctx.config, item)) for item in (settings.get("dirs") or [])]
        ledger = settings.get("ledger") or ""
        receipt_root = settings.get("receipt_root") or ""
        if ledger:
            ledger = str(cfg.resolve(ctx.config, ledger))
        if receipt_root:
            receipt_root = str(cfg.resolve(ctx.config, receipt_root))
        self.board = taskcards.Board(dirs, ledger, receipt_root, settings.get("receipt_depth", 3))
        self._lock = threading.Lock()
        self._cached = (0.0, None)

    def routes(self):
        return [
            Route("GET", "/api/taskcards", self.listing, "read"),
            Route("GET", "/api/taskcards/file", self.file, "read"),
        ]

    def _snapshot(self):
        with self._lock:
            at, data = self._cached
            if data is None or time.time() - at > CACHE_SECONDS:
                data = self.board.snapshot()
                self._cached = (time.time(), data)
            return data

    def listing(self, req):
        try:
            return json_response(200, self._snapshot())
        except Exception as exc:  # 目录读不了时页面要能显示原因，不让整个监看中心报错
            self.ctx.log(self.id, "任务卡列表读取失败", "warn", error=str(exc)[:200])
            return error(500, "任务卡列表读取失败：%s" % str(exc)[:200])

    def file(self, req):
        path = req.param("path")
        if not path:
            return error(400, "缺少 path")
        try:
            return json_response(200, self.board.read_text(path))
        except PermissionError as exc:
            return error(403, str(exc))
        except OSError as exc:
            return error(404, "读不到这个文件：%s" % str(exc)[:200])

    def health(self):
        missing = [str(item) for item in self.board.card_dirs if not item.is_dir()]
        if not self.board.card_dirs:
            return {"status": "warn", "detail": "没有配置任务卡目录"}
        if missing:
            return {"status": "warn", "detail": "任务卡目录不存在：%s" % "、".join(missing)}
        return {"status": "ok"}
