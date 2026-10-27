"""HTTP 内核：路由表 + 统一的安全门禁 + 统一的响应头与错误处理。

模块只声明“哪个地址、谁能访问、怎么处理”；门禁在这里集中执行，默认拒绝。
这样新增模块时不可能忘记加防护，防护规则也只有一处需要审查。

门禁级别（Route.guard）：
  page   只要求 Host 是本机地址（供浏览器直接打开的页面）
  read   page + 不是跨站请求（只读接口）
  post   read + Origin 是本监看页面（同源页面发起的、不改变审核状态的提交）
  write  post + 本进程的页面令牌 + JSON 请求体（会改变状态的操作，如提交裁决）
  open   只要求 Host，允许任意来源（仅限自带令牌校验的入口，如旧入口迁移）
"""
import http.server
import json
import secrets
import time
import urllib.parse

from . import logs

MAX_BODY = 1024 * 1024
GUARDS = ("page", "read", "post", "write", "open")


class Response:
    def __init__(self, code=200, body=b"", ctype="application/json; charset=utf-8", headers=None, frame="DENY", csp=None):
        self.code, self.body, self.ctype = code, body, ctype
        self.headers, self.frame, self.csp = dict(headers or {}), frame, csp


def json_response(code, payload, headers=None):
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    return Response(code, body, headers=headers)


def error(code, message, headers=None):
    return json_response(code, {"ok": False, "error": message}, headers)


class Route:
    def __init__(self, method, path, handler, guard="read", max_body=MAX_BODY, prefix=False):
        if guard not in GUARDS:
            raise ValueError("未知门禁级别：%s" % guard)
        self.method, self.path, self.handler, self.guard, self.max_body = method, path, handler, guard, max_body
        # prefix=True：path 以 / 结尾，匹配它下面的所有地址（没有精确匹配时才用）。给静态文件目录用。
        self.prefix = prefix


class Request:
    def __init__(self, raw, route):
        self.raw, self.route = raw, route
        self.method = raw.command
        self.path, _, query = raw.path.partition("?")
        self.query = urllib.parse.parse_qs(query)
        self.headers = raw.headers
        self._body = None

    def param(self, name, default=""):
        values = self.query.get(name)
        return values[0] if values else default

    def body(self):
        if self._body is None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if not 0 <= length <= self.route.max_body:
                raise ValueError("请求体过大")
            self._body = self.raw.rfile.read(length) if length else b""
        return self._body

    def json(self):
        data = json.loads(self.body().decode("utf-8") or "null")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data


class Context:
    """各模块共享的运行环境。"""

    def __init__(self, config, logger, port):
        self.config, self.logger, self.port = config, logger, port
        self.page_token = secrets.token_urlsafe(32)  # 每次启动重新生成，只写进本服务返回的页面
        self.hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}
        self.origins = {"http://" + host for host in self.hosts}
        self.started_at = time.time()
        self.modules = {}

    def log(self, mod, msg, level="info", **fields):
        logs.event(self.logger, mod, msg, level, **fields)


class App:
    def __init__(self, ctx):
        self.ctx = ctx
        self.routes = {}
        self.prefixes = []  # 前缀路由，按路径从长到短
        self.modules = []

    def register(self, module):
        if module.id in self.ctx.modules:
            raise ValueError("模块重复：%s" % module.id)
        for route in module.routes():
            if route.prefix:
                if not route.path.endswith("/"):
                    raise ValueError("前缀地址要以 / 结尾：%s" % route.path)
                self.prefixes.append(route)
                self.prefixes.sort(key=lambda r: -len(r.path))
                continue
            key = (route.method, route.path)
            if key in self.routes:
                raise ValueError("地址重复：%s %s" % key)
            self.routes[key] = route
        self.ctx.modules[module.id] = module
        self.modules.append(module)

    def start(self):
        for module in self.modules:
            try:
                module.start()
            except Exception as exc:  # 一个模块起不来不拖垮其他模块
                self.ctx.log(module.id, "start failed", "error", error=str(exc))

    def stop(self):
        for module in reversed(self.modules):
            try:
                module.stop()
            except Exception as exc:
                self.ctx.log(module.id, "stop failed", "error", error=str(exc))

    # ---- 门禁 ----
    def check(self, route, raw):
        ctx, headers = self.ctx, raw.headers
        if headers.get("Host") not in ctx.hosts:
            return "仅允许本机直接访问"
        if route.guard in ("page", "open"):
            return None
        if headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            return "仅允许本机直接访问"
        if route.guard == "read":
            return None
        if headers.get("Origin") not in ctx.origins:
            return "请从本机监看页面操作"
        if route.guard == "post":
            return None
        if not secrets.compare_digest(headers.get("X-Xiaoba-Token", ""), ctx.page_token):
            return "请从本机监看页面确认提交"
        if not headers.get("Content-Type", "").startswith("application/json"):
            return "无效请求"
        return None

    def dispatch(self, raw):
        path = raw.path.split("?")[0]
        route = self.routes.get((raw.command, path))
        if route is None:
            route = next((r for r in self.prefixes if r.method == raw.command and path.startswith(r.path)), None)
        if route is None:
            return error(404, "not found")
        denied = self.check(route, raw)
        if denied:
            return error(403, denied)
        try:
            result = route.handler(Request(raw, route))
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            return error(400, "无效请求：%s" % exc)
        except Exception as exc:  # 处理函数出错只影响这一次请求
            self.ctx.log("web", "handler error", "error", path=path, error=repr(exc))
            return error(500, "内部错误：%s" % exc)
        if isinstance(result, Response):
            return result
        code, payload = result
        return json_response(code, payload)

    def handler_class(self):
        app = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def _serve(self):
                response = app.dispatch(self)
                try:
                    self.send_response(response.code)
                    self.send_header("Content-Type", response.ctype)
                    self.send_header("Content-Length", str(len(response.body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    if response.csp:
                        self.send_header("Content-Security-Policy", response.csp)
                    else:
                        self.send_header("X-Frame-Options", response.frame)
                    for key, value in response.headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(response.body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = do_POST = _serve

        return Handler

    def server(self, host="127.0.0.1"):
        httpd = http.server.ThreadingHTTPServer((host, self.ctx.port), self.handler_class())
        httpd.daemon_threads = True
        return httpd
