#!/usr/bin/env python3
"""Key 池代理：把多个中转站的 API Key 合并成一个 Key（由监看中心拉起并看护）。

只用标准库，兼容 3.9；生产用统一解释器 配置/ 下的 3.14.7。只监听 127.0.0.1。

  - 客户端用合并 Key（Authorization: Bearer 或 x-api-key）访问本代理，路径原样转发，
    OpenAI 格式（/v1/chat/completions 等）与 Anthropic 格式（/v1/messages）都适用；
  - 每个请求挑“当前并发 ÷ 权重”最小、且没有在冷却的节点；
  - 节点返回 429 / 5xx / 401 / 403 或连不上时自动换下一个节点，出错的节点进入冷却；
  - 流式（SSE）响应边收边转；
  - 配置文件（keypool.json）由监看中心写，本代理只读，文件一变就自动重新加载；
  - /_admin/state 给监看中心读实时数据，要带本次启动的管理令牌（环境变量 KEYPOOL_ADMIN_TOKEN）。
  - GET /user/balance（或 /v1/user/balance）由代理自己回答：DeepSeek 格式的全池余额合计，同样要合并 Key。
"""
import argparse
import collections
import http.server
import json
import os
import random
import secrets
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:                                        # 作为包的一部分被导入时
    from . import keypool_guard as guard
    from . import keypool_stats as stats
except ImportError:                         # 作为脚本直接运行时（监看中心就是这样拉起它的）
    import keypool_guard as guard
    import keypool_stats as stats

DEFAULT_SETTINGS = {
    "guardEnabled": True,           # 安全检测总开关
    "maskSecrets": True,            # 请求里发现密钥时自动打码（否则只告警）
    "cooldownSeconds": 30,          # 429 / 5xx 后冷却
    "authFailCooldownSeconds": 300,  # 401 / 403（Key 失效、余额不足）后冷却
    "networkFailCooldownSeconds": 60,
    "timeoutSeconds": 300,          # 等上游的超时（首字节与流式两段之间都算）
    # 流式请求：从发出到收到第一段带内容的数据最多等多久，没到就换下一个节点；0 = 用内置的 180 秒
    "firstByteTimeoutSeconds": 180,
    # 流式请求：开始转发后两段之间最多断流多久，超了就断开并给客户端一条错误；0 = 用内置的 120 秒
    "streamIdleTimeoutSeconds": 120,
    # 熔断：偶发一次失败不停节点。连续失败够数、或最近的错误率过高才停（冷却时长仍用上面三个）；
    # 冷却结束后先放一个请求试探，连续成功够数才算恢复，试探失败立刻再停
    "breakerFailures": 3,           # 连续失败几次后停；1 = 失败一次就停（旧行为）
    "breakerErrorRate": 0.6,        # 最近 breakerWindow 次里错误率到这个比例也停
    "breakerMinRequests": 10,       # 至少有这么多次记录才看错误率
    "breakerSuccesses": 2,          # 试探阶段连续成功几次才算恢复
    "queueTimeoutSeconds": 30,      # 所有节点都满并发时最多排队多久
    "maxAttempts": 0,               # 0 = 最多把每个节点都试一遍
    "retryOnStatus": [401, 403, 408, 429, 500, 502, 503, 504, 520, 522, 524],
    "maxBodyMB": 50,
    "unsupportedModelSeconds": 600,  # 节点报“不支持这个模型”后，多久内不再把该模型的请求分给它
    "balanceHintSeconds": 600,       # 监看中心送来的余额读数多久算过期
    "ipLimitPauseSeconds": 15,       # 中转站按客户端 IP 限流后，整个站停多久再发请求
    "captureRefused": False,         # 诊断用：把下一个被内容过滤拒绝的请求原样存到本机一份（见 capture_refused）
}
BREAKER_WINDOW = 20                 # 熔断看错误率时取最近多少次
ROUTES = ("plan", "pay")            # 一个节点的两条结算线路：套餐 Key、余额（按量）Key
ROUTE_NAMES = {"plan": "套餐", "pay": "余额"}
# 中转站的内容过滤拒绝请求时的状态码与回复里的字样（例如 tierflow 用 500 + sensitive_words_detected）
CONTENT_STATUS = (400, 403, 422, 451, 500)
CONTENT_WORDS = (b"sensitive_words", b"content_filter", b"content_policy")
# 中转站按客户端 IP 限流时回复里的字样：限的是这台机器，换同一个站的别的 Key 没有用
IP_LIMIT_WORDS = (b"client ip", b"your ip", b"this ip")
# Key 池自己发请求时用的客户端标识。Python 自带的 Python-urllib 会被一些中转站前面的防护直接拒绝（Cloudflare 1010）
CLIENT_UA = "xiaoba-keypool/2.0"
QUOTA_WORDS = ("额度", "余额", "套餐", "quota", "insufficient", "balance", "subscription", "exhaust", "credit")
HOP = {"host", "connection", "content-length", "authorization", "x-api-key", "accept-encoding", "keep-alive",
       "transfer-encoding", "upgrade", "proxy-connection", "proxy-authorization", "te", "trailer",
       "x-keypool-node"}  # 本机诊断用：指定走哪个节点，不转发给上游
HOP_RES = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding", "proxy-connection",
           "te", "trailer", "upgrade"}
EVENT_LIMIT = 400


def mask(value, head=6, tail=4):
    value = str(value or "")
    if len(value) <= head + tail:
        return "•" * len(value)
    return value[:head] + "••••" + value[-tail:]


def ema(old, value, keep=0.7):
    return value if old is None else int(round(old * keep + value * (1 - keep)))


def model_names(value):
    """配置里的一串模型名：去掉空的、重复的和不像名字的。"""
    names = []
    for item in value if isinstance(value, list) else []:
        name = item.strip() if isinstance(item, str) else ""
        if name and len(name) <= 160 and name not in names and not any(ord(ch) < 32 for ch in name):
            names.append(name)
    return names


def station_of(base_url):
    """节点属于哪个中转站：地址里的域名（含端口）。同一个站的节点共用对方的 IP 限流和内容过滤。"""
    return urllib.parse.urlsplit(str(base_url or "")).netloc.lower()


def join_url(base, path):
    base = base.rstrip("/")
    # baseUrl 可以写成 https://x.com 或 https://x.com/v1，避免拼出 /v1/v1
    if base.endswith("/v1") and (path == "/v1" or path.startswith("/v1/") or path.startswith("/v1?")):
        return base + path[3:]
    return base + path


STREAM_FIRST_CONTENT_SECONDS = 180   # 2026-10-23 用户定：首段 180 秒、断流 120 秒
STREAM_IDLE_SECONDS = 120


def stream_waits(settings):
    """流式请求的 (首段最多等多久, 转发后断流最多多久)。设置里写 0 或没写都用内置值。

    不跟 timeoutSeconds 走：10-23 实测中转站会先回响应头和一条不带内容的事件，然后几百秒不出字，
    原来要等满 300 秒才放弃；10-15 至 10-23 成功的流式请求首段最长 290 秒，5084 次里只有 8 次过 120 秒。
    """
    first = float(settings.get("firstByteTimeoutSeconds") or 0) or STREAM_FIRST_CONTENT_SECONDS
    idle = float(settings.get("streamIdleTimeoutSeconds") or 0) or STREAM_IDLE_SECONDS
    return first, idle


def set_read_timeout(response, seconds):
    """改掉一个已经打开的上游响应的读超时。拿不到底层连接就算了（保持打开时的超时）。"""
    try:
        response.fp.raw._sock.settimeout(seconds)
        return True
    except (AttributeError, OSError, ValueError):
        return False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Node:
    def __init__(self, spec):
        self.id = str(spec["id"])
        self.inflight = 0
        self.holds = collections.deque()  # 每个在途请求占着的是哪个站（节点改了地址也能还对地方）
        self.ok = 0
        self.fail = 0
        self.cooldown_until = 0.0
        self.fail_kind = ""      # cooling（限流/服务端错误） / down（连不上、Key 失效）
        self.breaker = "closed"  # 熔断：closed 正常 / open 停用中 / half 冷却结束、试探恢复中
        self.streak = 0          # 连续失败次数
        self.half_ok = 0         # 试探阶段已连续成功几次
        self.window = collections.deque(maxlen=BREAKER_WINDOW)   # 最近若干次的成败，用来算错误率
        self.last_error = ""
        self.last_error_at = 0.0
        self.latency_ms = None   # 响应头耗时的滑动平均
        self.ttfb_ms = None      # 首个数据块耗时的滑动平均（流式时约等于首字时间）
        self.total_ms = None     # 整个请求耗时的滑动平均
        self.streams = 0         # 流式请求数
        self.buffered = 0        # 流式请求却整段返回（首包几乎等于总耗时）的次数
        self.last_buffered = False
        self.quarantined = False      # 安全检测拦截后隔离，不再分到请求，直到人工放行
        self.quarantine_reason = ""
        self.blocked_count = 0        # 累计拦截次数
        self.last_used = 0.0
        self.route_down = {}     # 线路 → 到什么时候之前不走它（额度不足或 Key 失效）
        self.route_error = {}    # 线路 → 最近一次被拒的说明
        self.hint = None         # 监看中心送来的余额读数 {"plan", "pay", "at"}
        self.last_route = ""     # 最近一次成功走的是哪一路
        self.apply(spec)

    def apply(self, spec):
        self.name = str(spec.get("name") or self.id)
        self.base_url = str(spec.get("baseUrl") or "").rstrip("/")
        self.station = station_of(self.base_url)
        keys = (str(spec.get("key") or ""), str(spec.get("payKey") or ""))
        if keys != (getattr(self, "key", None), getattr(self, "pay_key", None)):
            self.route_down, self.route_error = {}, {}  # 换了 Key：之前的暂停不再作数
        self.key, self.pay_key = keys   # key 是套餐 Key（只有一个 Key 的节点也填在这里），pay_key 是余额 Key
        self.pay_first = bool(spec.get("payFirst"))
        try:
            self.pay_floor = max(0.0, float(spec.get("payFloor") or 0))
        except (TypeError, ValueError):
            self.pay_floor = 0.0
        try:
            self.weight = max(0.1, float(spec.get("weight") or 1))
        except (TypeError, ValueError):
            self.weight = 1.0
        try:
            limit = int(spec.get("maxConcurrency") or 0)
        except (TypeError, ValueError):
            limit = 0
        self.max_concurrency = limit if limit > 0 else None
        self.enabled = spec.get("enabled", True) is not False and bool(self.base_url and (self.key or self.pay_key))
        # 节点指定了调用模型：分到这个节点的请求，模型名换成它再发出去。留空则跟随客户端。
        self.model = str(spec.get("model") or "").strip()
        # 余额路可以另选模型（两个 Key 能用的模型不一样）；留空则和套餐路相同
        self.pay_model = str(spec.get("payModel") or "").strip()
        # 这个节点专用的客户端标识（个别中转站只认特定标识）；留空则客户端带什么就转发什么
        self.user_agent = str(spec.get("userAgent") or "").strip()
        # 每一路实际有哪些模型（监看中心读 /v1/models 后写进配置）；空表示还没读到
        self.models, self.pay_models = model_names(spec.get("models")), model_names(spec.get("payModels"))
        # 别名：客户端要的模型名 → 这个节点上的模型名（同一个模型在不同中转站名字不一样时用）
        aliases = spec.get("aliases") if isinstance(spec.get("aliases"), dict) else {}
        self.aliases = {k.strip(): v.strip() for k, v in aliases.items()
                        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()}
        self.unsupported = {}  # (线路, 模型名) → 到什么时候之前不再把这个模型的请求从这一路发（上游报过“不支持这个模型”）

    # ---- 双路：套餐 Key 与余额 Key ----
    def route_key(self, route):
        return self.pay_key if route == "pay" else self.key

    def order(self):
        routes = ("pay", "plan") if self.pay_first else ROUTES
        return [r for r in routes if self.route_key(r)]

    def dual(self):
        return bool(self.key and self.pay_key)

    def _reading(self, now, ttl):
        hint = self.hint
        return hint if hint and now - hint["at"] <= ttl else None

    def floor_block(self, route, now, ttl):
        """用户设的余额底线：硬规则，读不到新的余额时宁可不走这一路。"""
        if route != "pay" or self.pay_floor <= 0:
            return ""
        hint = self._reading(now, ttl)
        if hint is None or hint.get("pay") is None:
            return "余额读数过期，无法判断底线，已停用"
        return "按量余额已到底线 ¥%g" % self.pay_floor if hint["pay"] <= self.pay_floor else ""

    def blocked(self, route, now, ttl):
        """这一路现在不该走的原因；空串表示可以走。"""
        reason = self.floor_block(route, now, ttl)
        if reason:
            return reason
        if self.route_down.get(route, 0) > now:
            return self.route_error.get(route) or "额度不足或 Key 失效，暂停中"
        hint = self._reading(now, ttl) if self.dual() else None
        value = hint.get(route) if hint else None
        return "%s已用完" % ROUTE_NAMES[route] if value is not None and value <= 0 else ""

    def route(self, now, ttl, skip=(), model=None, strict=False):
        """按顺序挑一路；都不理想时仍然试一试没被底线挡住的。全被底线挡住（或没有一路有这个模型）返回 None。"""
        routes = [r for r in self.lanes(model, strict) if r not in skip]
        for route in routes:
            if not self.blocked(route, now, ttl) and not self.lacks(route, model, now, strict):
                return route
        for route in routes:
            if not self.floor_block(route, now, ttl) and not self.lacks(route, model, now, strict):
                return route
        for route in routes:
            if not self.floor_block(route, now, ttl):
                return route
        return None

    def dry(self, now, ttl, model=None, strict=False):
        return all(self.blocked(r, now, ttl) for r in self.lanes(model, strict))

    # ---- 按模型选路 ----
    def serves(self, route, model):
        """这一路有没有客户端要的模型：True 有（含别名），False 没有，None 还没读到这一路的模型清单。"""
        catalog, alias = (self.pay_models if route == "pay" else self.models), self.aliases.get(model)
        if not catalog:
            return True if alias else None
        return model in catalog or alias in catalog

    def lanes(self, model=None, strict=False):
        """能接这个模型的线路。strict：池里有线路确实有这个模型，这时明确没有它的线路不参与。"""
        return [r for r in self.order() if not (strict and model and self.serves(r, model) is False)]

    def sends(self, model, route="plan", strict=False):
        """这个节点从这一路实际会发给上游的模型名。"""
        if model:
            catalog, alias = (self.pay_models if route == "pay" else self.models), self.aliases.get(model)
            if catalog and model in catalog:
                return model
            if alias and (not catalog or alias in catalog):
                return alias
            if strict:
                return model
        # 池里没有任何一路有这个模型（或客户端没带模型名）：用这一路指定的替代模型，没指定就原样发
        return (self.pay_model if route == "pay" else "") or self.model or model

    def lacks(self, route, model, now, strict=False):
        """这一路报过不支持这个模型。"""
        return bool(model) and self.unsupported.get((route, self.sends(model, route, strict)), 0) > now

    def rejects(self, model, now, strict=False):
        return bool(model) and all(self.lacks(r, model, now, strict) for r in self.lanes(model, strict))


OUTPUT_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


def clamp_output(body, cap):
    """把请求里超过站点上限的输出上限压到上限。返回 (请求体, {字段: 原值})；没有超的原样返回。"""
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, AttributeError):
        return body, {}
    if not isinstance(parsed, dict):
        return body, {}
    over = {f: parsed[f] for f in OUTPUT_FIELDS
            if isinstance(parsed.get(f), (int, float)) and not isinstance(parsed[f], bool) and parsed[f] > cap}
    if not over:
        return body, {}
    for field in over:
        parsed[field] = cap
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), over


class Station:
    """一个中转站（同一个地址下的所有节点）。对方按客户端 IP 限流，所以限速要整个站一起算，不能按节点算。"""

    def __init__(self):
        self.inflight = 0
        self.last_start = 0.0        # 最近一次向这个站发请求的时间
        self.hold_until = 0.0        # 被对方按 IP 限流后，整个站停到什么时候
        self.max_concurrency = None  # 整个站同时最多几个请求；None 不限
        self.min_interval = 0.0      # 两次请求之间至少隔几秒；0 不限
        self.max_output = None       # 这个站接受的输出上限（max_tokens）；None 不管

    def limit(self, spec):
        spec = spec if isinstance(spec, dict) else {}
        try:
            cap = int(spec.get("maxConcurrency") or 0)
            gap = float(spec.get("minIntervalSeconds") or 0)
        except (TypeError, ValueError):
            cap, gap = 0, 0.0
        self.max_concurrency, self.min_interval = (cap if cap > 0 else None), max(0.0, gap)
        try:
            out = int(spec.get("maxOutputTokens") or 0)
        except (TypeError, ValueError):
            out = 0
        self.max_output = out if out > 0 else None

    def opens_in(self, now):
        """还要等几秒才到下一次可以发请求的时间（只算间隔和暂停，不算并发）。"""
        return max(0.0, self.hold_until - now, self.last_start + self.min_interval - now)

    def open(self, now):
        return (self.max_concurrency is None or self.inflight < self.max_concurrency) and self.opens_in(now) <= 0


# 流式回复的第一条事件就是上游的错误时（比如 200 + RequestBurstTooFast），回复里的字样：按限流处理
BURST_WORDS = (b"burst", b"too fast", b"rate_limit", b"rate limit", b"too many requests", b"overloaded", b"busy")


def iter_events(buf):
    """SSE 里已经收全的事件，依次给出 (event 名, data)。注释行（: keep-alive）和空事件跳过。"""
    text = buf.replace(b"\r\n", b"\n")
    while True:
        end = text.find(b"\n\n")
        if end < 0:
            return
        block, text = text[:end], text[end + 2:]
        name, data = b"", []
        for line in block.split(b"\n"):
            if not line or line.startswith(b":"):
                continue
            field, _, value = line.partition(b":")
            value = value[1:] if value.startswith(b" ") else value
            if field == b"event":
                name = value
            elif field == b"data":
                data.append(value)
        if name or data:
            yield name, b"\n".join(data)


def first_event(buf):
    """SSE 里第一条有内容的事件 (event 名, data)；还没收全返回 None。注释行（: keep-alive）和空事件跳过。"""
    return next(iter_events(buf), None)


# Anthropic 流里不带正文的事件：开头、心跳。其余（content_block_*、message_delta、message_stop、error）都算有进展。
_EMPTY_ANTHROPIC_EVENTS = {"message_start", "ping"}
_DELTA_FIELDS = ("content", "reasoning_content", "reasoning", "text", "thinking", "tool_calls", "function_call")


def event_has_content(name, data):
    """这一条事件是不是上游真的开始回答了。

    只带角色、空 delta、心跳不算（2026-10-23 实测：中转站先回一条 role 事件，之后几百秒不出字）；
    正文、思考、工具调用、结束标记、错误、[DONE]、解析不了的 data 都算，交给后面照常处理。
    """
    if name == b"error" or data.strip() == b"[DONE]":
        return True
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return True
    if not isinstance(parsed, dict):
        return True
    if parsed.get("error"):
        return True
    kind = parsed.get("type")
    if isinstance(kind, str) and kind and "choices" not in parsed:
        return kind not in _EMPTY_ANTHROPIC_EVENTS
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        return True     # 认不出的格式、只带用量的收尾块：都算，只把认得出的空事件挡在外面
    for choice in choices:
        if not isinstance(choice, dict):
            return True
        if choice.get("finish_reason") or choice.get("text"):
            return True
        delta = choice.get("delta") or choice.get("message") or {}
        if isinstance(delta, dict) and any(delta.get(key) for key in _DELTA_FIELDS):
            return True
    return False


class FirstContentTimeout(Exception):
    """流式回复在限定时间内没有出现带内容的数据。"""


def peek_first_content(upstream, deadline, limit=1024 * 1024):
    """读到流里第一条带内容的事件为止（最多 limit 字节），读走的部分之后用 _Replay 接回去。

    deadline 是绝对时间：到点还没等到就抛 FirstContentTimeout，调用方换下一个节点重发，客户端什么都没收到。
    """
    reader = getattr(upstream, "read1", None) or upstream.read
    buf = b""
    while len(buf) < limit:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise FirstContentTimeout()
        set_read_timeout(upstream, remaining)
        try:
            chunk = reader(65536)
        except (socket.timeout, TimeoutError) as exc:
            raise FirstContentTimeout() from exc
        if not chunk:
            break
        buf += chunk
        events = list(iter_events(buf))
        if any(event_has_content(name, data) for name, data in events):
            break
        if not events and buf.strip() and not buf.lstrip().startswith((b"data", b"event", b":", b"id", b"retry")):
            break   # 不是 SSE（比如直接给一段 JSON 错误）：不再等，交给后面判断
    return buf


def stream_error(head):
    """流的首段是不是上游的错误：是就返回错误原文，不是返回 None。

    认两种写法：OpenAI 格式的 data 里顶层带 error；Anthropic 格式的 event: error（data 里 type 为 error）。
    上游没按 SSE 回、直接给一段 JSON 错误的也算。只带角色、心跳这类不带内容的事件之后才来的错误也认
    （首段现在读到第一条带内容的事件为止），看到第一条带内容的事件就停。
    """
    events = list(iter_events(head)) or [(b"", head.strip())]
    for name, data in events:
        if not data and name != b"error":
            continue
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            parsed = None
        is_error = name == b"error" or (isinstance(parsed, dict) and (
            parsed.get("type") == "error" or (parsed.get("error") is not None and "choices" not in parsed)))
        if is_error:
            return data.decode("utf-8", "replace")
        if event_has_content(name, data):
            return None
    return None


class _Replay:
    """已经读走一段的上游回复：把读走的部分接回去，后面照常转发。"""

    def __init__(self, upstream, head):
        self._upstream, self._head = upstream, head
        self.status = upstream.status if hasattr(upstream, "status") else upstream.code
        self.headers = upstream.headers

    def read(self, size=-1):
        if self._head:
            if size is None or size < 0:
                head, self._head = self._head, b""
                return head + self._upstream.read()
            head, self._head = self._head[:size], self._head[size:]
            return head
        return self._upstream.read(size)

    def read1(self, size=-1):
        return self.read(size if size and size > 0 else 65536)

    def close(self):
        self._upstream.close()

    def __getattr__(self, name):
        return getattr(self._upstream, name)


class Pool:
    def __init__(self, path, usage_dir=None):
        self.path = path
        self.usage_dir = usage_dir      # 用量记录写到哪个目录；None 表示不记
        self.usage_error = ""
        self.lock = threading.Condition()
        self.nodes = collections.OrderedDict()
        self.stations = {}              # 中转站名 → Station（限速与在途数，整个站共用）
        self.master_keys = []
        self.published_models = []      # 对外公布的模型清单（客户端读 /v1/models 时看到的）；空表示公布全部
        self.settings = dict(DEFAULT_SETTINGS)
        self.mtime = None
        self.load_error = ""
        self.events = collections.deque(maxlen=EVENT_LIMIT)
        self.sec_events = collections.deque(maxlen=200)   # 安全事件（告警、拦截、打码）
        self.sec_counts = {"blocked": 0, "warned": 0, "masked": 0, "checked": 0}
        self.seq = 0
        self.minutes = collections.OrderedDict()  # 分钟 -> [成功, 失败]
        self.started_at = time.time()
        self.reload(force=True)

    # ---- 配置 ----
    def reload(self, force=False):
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except OSError as exc:
            self.load_error = "读不到配置：%s" % exc
            return
        if not force and mtime == self.mtime:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("配置必须是 JSON 对象")
        except (OSError, ValueError) as exc:
            self.load_error = "配置无效，继续使用上一份：%s" % exc
            self.mtime = mtime
            return
        keys = data.get("masterKey")
        keys = [k for k in (keys if isinstance(keys, list) else [keys]) if isinstance(k, str) and k]
        settings = dict(DEFAULT_SETTINGS)
        settings.update({k: v for k, v in (data.get("settings") or {}).items() if k in DEFAULT_SETTINGS})
        with self.lock:
            fresh = collections.OrderedDict()
            for spec in data.get("nodes") or []:
                if not isinstance(spec, dict) or not spec.get("id"):
                    continue
                node = self.nodes.get(str(spec["id"]))
                if node:
                    node.apply(spec)
                else:
                    node = Node(spec)
                fresh[node.id] = node
            self.nodes = fresh
            limits = data.get("stations") if isinstance(data.get("stations"), dict) else {}
            limits = {str(k).lower(): v for k, v in limits.items()}
            for name in {n.station for n in fresh.values()}:
                self.stations.setdefault(name, Station()).limit(limits.get(name))
            self.master_keys = keys
            self.published_models = model_names(data.get("publishedModels"))
            self.settings = settings
            self.mtime = mtime
            self.load_error = ""
            self.lock.notify_all()

    # ---- 调度 ----
    def served(self, model, pin=None):
        """池里（指定了节点就只看那个节点）有没有哪一路确实有这个模型。有的话只分给这些线路，不让别的线路拿替代模型顶上。"""
        with self.lock:
            return bool(model) and any(n.serves(r, model) for n in self.nodes.values()
                                       if n.enabled and (pin is None or pin in (n.id, n.name)) for r in n.order())

    def published(self):
        """对外公布的模型清单；没有设置时是所有启用线路的模型合在一起。一个都不知道时返回 None。"""
        with self.lock:
            if self.published_models:
                return list(self.published_models)
            names = set()
            for node in self.nodes.values():
                if node.enabled:
                    known = set(node.models if node.key else []) | set(node.pay_models if node.pay_key else [])
                    names |= known | {k for k, v in node.aliases.items() if not known or v in known}
            return sorted(names) or None

    def _pick(self, tried, pin=None, model=None, strict=False):
        now, ttl = time.time(), self.hint_ttl()
        free = [n for n in self.nodes.values() if n.enabled and not n.quarantined and n.id not in tried
                and (pin is None or pin in (n.id, n.name)) and n.route(now, ttl, model=model, strict=strict)
                and (n.max_concurrency is None or n.inflight < n.max_concurrency)
                and self.stations[n.station].open(now)]   # 整个站的并发、间隔、被限流后的暂停
        # 熔断过的节点冷却结束后只放一个请求去试探，试探没回来之前不再给它分
        fresh = [n for n in free if n.cooldown_until <= now and (n.breaker == "closed" or n.inflight == 0)]
        pool = fresh or free  # 全部在冷却时也试一试，总比直接失败好
        if strict:  # 模型清单还没读到的节点排到最后：先用确定有这个模型的
            pool = [n for n in pool if any(n.serves(r, model) for r in n.lanes(model, strict))] or pool
        # 报过“不支持这个模型”的节点排到最后：还有别的节点时不选它
        pool = [n for n in pool if not n.rejects(model, now, strict)] or pool
        # 两路都没钱可用的节点同样排到最后
        pool = [n for n in pool if not n.dry(now, ttl, model, strict)] or pool
        # 先比“当前并发 ÷ 权重”，一样时挑最久没用过的：连续进来的请求会轮流落到每个节点上
        best, best_key = None, None
        for node in pool:
            key = (node.inflight / node.weight, node.last_used, random.random())
            if best is None or key < best_key:
                best, best_key = node, key
        return best

    def acquire(self, tried, pin=None, model=None, strict=False):
        deadline = time.time() + float(self.settings["queueTimeoutSeconds"])
        with self.lock:
            while True:
                node = self._pick(tried, pin, model, strict)
                if node:
                    self._take(node)
                    return node
                now, ttl = time.time(), self.hint_ttl()
                left = [n for n in self.nodes.values() if n.enabled and not n.quarantined and n.id not in tried
                        and (pin is None or pin in (n.id, n.name)) and n.route(now, ttl, model=model, strict=strict)]
                remaining = deadline - time.time()
                if not left or remaining <= 0:
                    return None
                # 有站点在等间隔或暂停：正好睡到它放行的那一刻；并发腾出来时 release 会叫醒
                due = [w for w in (self.stations[n.station].opens_in(now) for n in left) if w > 0]
                self.lock.wait(max(0.02, min([remaining, 0.5] + due)))

    def _take(self, node):
        now = time.time()
        if node.breaker == "open" and node.cooldown_until <= now:
            node.breaker, node.half_ok = "half", 0
        node.inflight += 1
        node.last_used = now
        station = self.stations[node.station]
        station.inflight += 1
        station.last_start = now
        node.holds.append(station)

    def retake(self, node):
        """同一个节点换另一路再发一次：重新占上并发。"""
        with self.lock:
            self._take(node)

    def release(self, node):
        with self.lock:
            node.inflight = max(0, node.inflight - 1)
            station = node.holds.popleft() if node.holds else self.stations[node.station]
            station.inflight = max(0, station.inflight - 1)
            self.lock.notify_all()

    def hint_ttl(self):
        return float(self.settings["balanceHintSeconds"])

    def set_hint(self, node_id, plan, pay):
        """监看中心查到余额后送过来：用来在撞错之前就知道哪一路没钱了。"""
        with self.lock:
            node = self.nodes.get(node_id)
            if node:
                node.hint = {"plan": plan, "pay": pay, "at": time.time()}
                self.lock.notify_all()
            return bool(node)

    def mark_route_fail(self, node, route, status, detail):
        """这一路被上游以额度不足或 Key 无效拒绝：只停这一路，节点本身不冷却。"""
        with self.lock:
            node.route_down[route] = time.time() + float(self.settings["authFailCooldownSeconds"])
            node.route_error[route] = ("%s Key 被拒（%s）：%s" % (ROUTE_NAMES[route], status, detail))[:200]
            node.last_error = node.route_error[route]
            node.last_error_at = time.time()

    def mark_unsupported(self, node, model, route="plan"):
        """节点不支持这个模型：不算节点故障（不冷却），只是一段时间内不再把这个模型分给它。"""
        with self.lock:
            node.unsupported[(route, model)] = time.time() + float(self.settings["unsupportedModelSeconds"])
            node.last_error = "不支持模型 %s" % model
            node.last_error_at = time.time()

    def mark_ip_limited(self, node, status, detail, retry_after=None):
        """对方按客户端 IP 限流：整个站停一会儿。换同站别的节点没有用，只会让限流更久；节点本身不算故障。"""
        pause = float(self.settings["ipLimitPauseSeconds"])
        if retry_after:
            pause = min(max(pause, retry_after), 120.0)
        with self.lock:
            station = self.stations[node.station]
            station.hold_until = max(station.hold_until, time.time() + pause)
            node.last_error = ("这个站按 IP 限流，整个站暂停 %d 秒（%s）：%s" % (pause, status, detail))[:240]
            node.last_error_at = time.time()
            self.lock.notify_all()

    def mark_refused(self, node, status, detail):
        """中转站的内容过滤拒了这个请求：是请求内容的问题，不算节点故障（不冷却、不计失败），只记下来给页面看。"""
        with self.lock:
            node.last_error = ("内容被这个站的过滤拒绝（%s）：%s" % (status, detail))[:240]
            node.last_error_at = time.time()
            return {n.id for n in self.nodes.values() if n.station == node.station}

    def capture_refused(self, node, route, path, anthropic, status, reply, body):
        """诊断开关开着时，把被内容过滤拒绝的请求存一份（用来找出是哪段内容触发的）。

        只存一份：文件已经在就不再写，所以不会越积越多；查完删掉文件、关掉开关。
        存的是发给上游的请求体（请求里的密钥已打码），权限 600，只在本机。
        """
        if not (self.settings.get("captureRefused") and self.usage_dir and body):
            return
        target = os.path.join(self.usage_dir, "refused-request.json")
        try:
            os.makedirs(self.usage_dir, mode=0o700, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"t": round(time.time(), 3), "node": node.id, "route": route, "path": path,
                           "anthropic": bool(anthropic), "status": status, "reply": reply.decode("utf-8", "replace"),
                           "body": body.decode("utf-8", "replace")}, handle, ensure_ascii=False)
        except OSError:
            pass   # 已经存过一份，或者写不进去：诊断功能不影响转发

    def log_upstream_error(self, node, route, path, status, text):
        """把上游的报错正文写进 keypool.log：一行，最多 400 字，Key 打码。只留在本机日志里。"""
        try:
            body = (text or b"").decode("utf-8", "replace")
        except Exception:
            body = repr(text)[:400]
        for secret in [k for n in self.nodes.values() for k in (n.key, n.pay_key) if k] + list(self.master_keys):
            if secret and len(secret) >= 8:
                body = body.replace(secret, secret[:3] + "***")
        body = " ".join(body.split())[:400]
        try:
            sys.stderr.write("%s upstream error: %s %s %s status=%s %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), node.name, route or "-", path, status, body))
            sys.stderr.flush()
        except Exception:
            pass

    def mark_fail(self, node, status, detail, force=False):
        """force：不必等连续失败够数，立刻停（比如双路节点两个 Key 都被拒，再试也是一样）。"""
        settings = self.settings
        if status in (401, 403):
            cool, kind = settings["authFailCooldownSeconds"], "down"
        elif status == "network":
            cool, kind = settings["networkFailCooldownSeconds"], "down"
        else:
            cool, kind = settings["cooldownSeconds"], "cooling"
        with self.lock:
            node.fail += 1
            node.streak += 1
            node.window.append(False)
            node.last_error = ("%s：%s" % (status, detail))[:240]
            node.last_error_at = time.time()
            try:
                need = max(1, int(settings["breakerFailures"]))
                rate, least = float(settings["breakerErrorRate"]), max(1, int(settings["breakerMinRequests"]))
            except (TypeError, ValueError):
                need, rate, least = 1, 1.0, BREAKER_WINDOW + 1
            failing = len(node.window) >= least and node.window.count(False) / len(node.window) >= rate
            # 试探中的失败立刻再停；否则要连续失败够数，或最近的错误率过高
            if force or node.breaker != "closed" or node.streak >= need or failing:
                node.breaker, node.half_ok = "open", 0
                node.cooldown_until = time.time() + float(cool)
                node.fail_kind = kind

    def mark_ok(self, node, first_byte_ms, route=None):
        with self.lock:
            if route:
                node.last_route = route
                node.route_down.pop(route, None)
                node.route_error.pop(route, None)
            node.ok += 1
            node.streak = 0
            node.window.append(True)
            if node.breaker != "closed":
                node.half_ok += 1
                try:
                    enough = node.half_ok >= max(1, int(self.settings["breakerSuccesses"]))
                except (TypeError, ValueError):
                    enough = True
                if enough:   # 恢复：之前的失败记录不再算进错误率
                    node.breaker, node.half_ok = "closed", 0
                    node.window.clear()
                else:
                    node.breaker = "half"
            node.cooldown_until = 0.0
            node.fail_kind = ""
            node.latency_ms = ema(node.latency_ms, first_byte_ms)

    def timing(self, node, ttfb_ms, total_ms, stream):
        """请求结束后记下首包与总耗时；流式请求的首包几乎等于总耗时，说明上游没有边生成边推送。"""
        buffered = bool(stream and ttfb_ms is not None and total_ms >= 8000 and ttfb_ms >= total_ms * 0.85)
        with self.lock:
            if ttfb_ms is not None:
                node.ttfb_ms = ema(node.ttfb_ms, ttfb_ms)
            node.total_ms = ema(node.total_ms, total_ms)
            if stream:
                node.streams += 1
                node.buffered += 1 if buffered else 0
                node.last_buffered = buffered
        return buffered

    def record(self, node, status, ms, model, stream, attempt, ok, ttfb=None, total=None, path=None, buffered=False,
               route=None, asked=None, usage=None, gap=None):
        now = time.time()
        if self.usage_dir and node:     # 用量记录：只有元数据，不含请求与回复内容
            usage = usage or {}
            try:
                stats.append_usage(self.usage_dir, {
                    "t": round(now, 3), "node": node.id, "name": node.name, "route": route if node.dual() else None,
                    "model": model, "asked": asked, "path": path, "stream": bool(stream), "status": status, "ok": bool(ok),
                    "attempt": attempt, "ms": ms, "ttfb": ttfb, "total": total if total is not None else ms,
                    "in": usage.get("in"), "out": usage.get("out"), "cache": usage.get("cache"), "gap": gap})
                self.usage_error = ""
            except OSError as exc:
                self.usage_error = "用量记录写不进去：%s" % exc
        with self.lock:
            self.seq += 1
            self.events.append({"seq": self.seq, "t": round(now, 3), "node": node.id if node else None,
                                "nodeName": node.name if node else None, "status": status, "ms": ms,
                                "ttfb": ttfb, "total": total, "path": path, "buffered": buffered,
                                "model": model, "asked": asked, "stream": stream, "attempt": attempt, "ok": ok,
                                "route": route if node and node.dual() else None, "gap": gap})
            minute = int(now // 60)
            bucket = self.minutes.setdefault(minute, [0, 0])
            bucket[0 if ok else 1] += 1
            while self.minutes and next(iter(self.minutes)) < minute - 10:
                self.minutes.popitem(last=False)

    # ---- 安全检测 ----
    def sec_event(self, kind, node, detail, findings=None, path=None):
        now = time.time()
        with self.lock:
            self.seq += 1
            if kind in self.sec_counts:
                self.sec_counts[kind] += 1
            self.sec_events.append({"seq": self.seq, "t": round(now, 3), "kind": kind, "detail": detail,
                                    "node": node.id if node else None, "nodeName": node.name if node else None,
                                    "path": path, "findings": findings or []})

    def quarantine(self, node, findings, path):
        with self.lock:
            node.quarantined = True
            node.blocked_count += 1
            labels = "；".join(sorted({f["label"] for f in findings if f.get("severity") == "block"}))
            node.quarantine_reason = labels or "危险操作"
        self.sec_event("blocked", node, node.quarantine_reason, findings, path)

    def release_quarantine(self, node_id):
        with self.lock:
            node = self.nodes.get(node_id)
            if node:
                node.quarantined = False
                node.quarantine_reason = ""
            return bool(node)

    def guard_on(self):
        return bool(self.settings.get("guardEnabled", True))

    def sec_state(self, since=0):
        with self.lock:
            return {"counts": dict(self.sec_counts),
                    "events": [e for e in self.sec_events if e["seq"] > since][-80:]}

    def balance_summary(self):
        """全池余额合计（元），给 DeepSeek 格式的 /user/balance 用：只算启用节点里没过期的读数。
        没有任何读数返回 None。套餐 = granted_balance，按量 = topped_up_balance。"""
        now, ttl = time.time(), self.hint_ttl()
        with self.lock:
            enabled = [n for n in self.nodes.values() if n.enabled]
            readings = [h for h in (n._reading(now, ttl) for n in enabled) if h]
        if not readings:
            return None
        plan = sum(h["plan"] for h in readings if h.get("plan") is not None)
        pay = sum(h["pay"] for h in readings if h.get("pay") is not None)
        return {"plan": round(plan, 4), "pay": round(pay, 4), "total": round(plan + pay, 4),
                "nodes": len(enabled), "fresh": len(readings),
                "planMissing": sum(1 for h in readings if h.get("plan") is None)}

    # ---- 给监看中心的实时数据 ----
    def state(self, since=0):
        now, ttl = time.time(), self.hint_ttl()
        with self.lock:
            recent = [e for e in self.events if e["t"] >= now - 60]
            rpm_by_node = collections.Counter(e["node"] for e in recent if e["node"])
            nodes = []
            for node in self.nodes.values():
                if not node.enabled:
                    status = "disabled"
                elif node.quarantined:
                    status = "quarantined"
                elif node.cooldown_until > now:
                    status = node.fail_kind or "cooling"
                elif node.inflight:
                    status = "busy"
                else:
                    status = "ok" if node.ok or not node.fail else "idle"
                nodes.append({
                    "id": node.id, "name": node.name, "status": status, "inflight": node.inflight,
                    "maxConcurrency": node.max_concurrency, "weight": node.weight, "ok": node.ok, "fail": node.fail,
                    "cooldownLeft": max(0, int(round(node.cooldown_until - now))), "lastError": node.last_error or None,
                    "breaker": "half" if node.breaker == "open" and node.cooldown_until <= now else node.breaker,
                    "failStreak": node.streak,
                    "lastErrorAt": node.last_error_at or None, "latencyMs": node.latency_ms,
                    "ttfbMs": node.ttfb_ms, "totalMs": node.total_ms, "streams": node.streams,
                    "buffered": node.buffered, "lastBuffered": node.last_buffered,
                    "quarantined": node.quarantined, "quarantineReason": node.quarantine_reason or None,
                    "blockedCount": node.blocked_count,
                    "rpm": rpm_by_node.get(node.id, 0), "lastUsed": node.last_used or None,
                    "dual": node.dual(), "route": node.route(now, ttl) if node.enabled else None,
                    "lastRoute": node.last_route or None,
                    "routes": [{"id": r, "blocked": node.blocked(r, now, ttl) or None,
                                "downLeft": max(0, int(round(node.route_down.get(r, 0) - now)))}
                               for r in node.order()]})
            minute = int(now // 60)
            good = bad = 0
            for key, (ok_count, fail_count) in self.minutes.items():
                if key >= minute - 9:
                    good, bad = good + ok_count, bad + fail_count
            events = [e for e in self.events if e["seq"] > since][-120:]
            return {
                "ok": True, "pid": os.getpid(), "startedAt": self.started_at, "seq": self.seq,
                "configError": self.load_error or None, "hasMasterKey": bool(self.master_keys),
                "usageError": self.usage_error or None,
                "totals": {"inflight": sum(n.inflight for n in self.nodes.values()), "rpm": len(recent),
                           "successRate": round(good * 100.0 / (good + bad), 1) if good + bad else None,
                           "requests10m": good + bad,
                           "capacity": sum(n.max_concurrency or 0 for n in self.nodes.values() if n.enabled)},
                "nodes": nodes, "events": events, "guardEnabled": self.guard_on(),
                "stations": [{"name": name, "inflight": st.inflight, "maxConcurrency": st.max_concurrency,
                              "minIntervalSeconds": st.min_interval, "holdLeft": max(0, int(round(st.hold_until - now)))}
                             for name, st in sorted(self.stations.items())
                             if any(n.station == name for n in self.nodes.values())],
                "security": {"counts": dict(self.sec_counts),
                             "events": [e for e in self.sec_events if e["seq"] > since][-40:]}}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "xiaoba-keypool"
    pool = None
    admin_token = ""
    opener = urllib.request.build_opener(NoRedirect)  # 跟随系统与环境变量里的代理设置

    def log_message(self, *args):
        pass

    # ---- 小工具 ----
    def send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def client_error(self, code, message, kind="invalid_request_error"):
        self.send_json(code, {"error": {"message": message, "type": kind}})

    def read_body(self):
        limit = int(self.pool.settings["maxBodyMB"]) * 1024 * 1024
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks, size = [], 0
            while True:
                line = self.rfile.readline(65537).strip()
                length = int(line.split(b";")[0] or b"0", 16)
                if length == 0:
                    while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                        pass
                    break
                size += length
                if size > limit:
                    raise ValueError("请求体过大")
                chunks.append(self.rfile.read(length))
                self.rfile.readline(3)
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError("请求体过大")
        return self.rfile.read(length) if length > 0 else b""

    def client_key(self):
        value = self.headers.get("x-api-key")
        if value:
            return value.strip(), True
        auth = self.headers.get("Authorization") or ""
        return (auth[7:] if auth[:7].lower() == "bearer " else auth).strip(), False

    # ---- 入口 ----
    def handle_any(self):
        path = self.path
        if path == "/healthz":
            return self.send_json(200, {"ok": True})
        if path.startswith("/_admin/"):
            if not secrets.compare_digest(self.headers.get("X-KeyPool-Admin") or "", self.admin_token):
                return self.send_json(403, {"ok": False, "error": "forbidden"})
            if path.split("?")[0] == "/_admin/state":
                since = 0
                if "since=" in path:
                    try:
                        since = int(path.split("since=")[1].split("&")[0])
                    except ValueError:
                        since = 0
                self.pool.reload()
                return self.send_json(200, self.pool.state(since))
            if path.split("?")[0] == "/_admin/balance":   # 监看中心送来某个节点的余额读数
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)

                def amount(name):
                    try:
                        return float(query[name][0])
                    except (KeyError, ValueError):
                        return None
                return self.send_json(200, {"ok": self.pool.set_hint((query.get("node") or [""])[0],
                                                                      amount("plan"), amount("pay"))})
            if path.split("?")[0] == "/_admin/release":   # 放行被隔离的节点
                node_id = ""
                if "node=" in path:
                    node_id = urllib.parse.unquote(path.split("node=")[1].split("&")[0])
                return self.send_json(200, {"ok": self.pool.release_quarantine(node_id)})
            return self.send_json(404, {"ok": False, "error": "not found"})
        self.pool.reload()
        key, use_x_api_key = self.client_key()
        if not key or not any(secrets.compare_digest(key, k) for k in self.pool.master_keys):
            self.read_body_quietly()
            return self.client_error(401, "invalid api key (key-pool)", "authentication_error")
        try:
            body = self.read_body() if self.command not in ("GET", "HEAD") else None
        except ValueError as exc:
            self.close_connection = True
            return self.client_error(413, str(exc))
        if (self.command == "GET" and path.split("?")[0].rstrip("/") in ("/user/balance", "/v1/user/balance")
                and not self.headers.get("X-KeyPool-Node")):
            return self.send_balance()
        if (self.command == "GET" and path.split("?")[0].rstrip("/") in ("/v1/models", "/models")
                and not self.headers.get("X-KeyPool-Node")):
            # 模型列表由 Key 池自己回答：转给某一个节点的话，客户端只能看到那一路的模型
            names = self.pool.published()
            if names is not None:
                data = [{"id": name, "object": "model", "type": "model", "display_name": name, "created": 0,
                         "owned_by": "key-pool"} for name in names]
                return self.send_json(200, {"object": "list", "data": data, "has_more": False,
                                            "first_id": names[0] if names else None,
                                            "last_id": names[-1] if names else None})
        self.forward(body, use_x_api_key)

    def send_balance(self):
        """DeepSeek 官方 /user/balance 的格式，金额是全池合计；读数来自监看中心的余额查询，不另外请求上游。"""
        summary = self.pool.balance_summary()
        if summary is None:
            return self.client_error(503, "Key 池还没有余额读数（监看中心没开，或节点没填余额查询会话）", "service_unavailable")
        def money(value):
            return "%.4f" % value
        return self.send_json(200, {
            "is_available": summary["total"] > 0,
            "balance_infos": [{"currency": "CNY", "total_balance": money(summary["total"]),
                               "granted_balance": money(summary["plan"]), "topped_up_balance": money(summary["pay"])}],
            # 以下是 Key 池自己加的：读数覆盖了几个启用节点，几个只有按量部分（套餐接口失败）
            "key_pool": {"nodes": summary["nodes"], "fresh": summary["fresh"], "plan_missing": summary["planMissing"]},
        })

    def read_body_quietly(self):
        try:
            self.read_body()
        except Exception:
            self.close_connection = True

    def forward(self, body, use_x_api_key):
        pool = self.pool
        model, stream = None, False
        if body and body[:1] == b"{" and len(body) < 20 * 1024 * 1024:
            try:
                parsed = json.loads(body.decode("utf-8"))
                model, stream = parsed.get("model"), bool(parsed.get("stream"))
            except (ValueError, AttributeError):
                pass
        asked = model if isinstance(model, str) and model else None  # 客户端要的模型；model 之后记的是实际发给节点的
        settings = pool.settings
        retry_on = set(settings["retryOnStatus"])
        enabled = [n for n in pool.nodes.values() if n.enabled]
        max_attempts = int(settings["maxAttempts"]) or len(enabled)
        max_attempts = max(1, min(max_attempts, len(enabled) or 1))
        pin = (self.headers.get("X-KeyPool-Node") or "").strip() or None
        if pin:
            max_attempts = 1
        # 池里有线路确实有客户端要的模型：只在这些线路之间分配，模型名不改（别名除外）
        strict = pool.served(asked, pin)
        path = self.path.split("?")[0][:120]

        # 安全检测：请求侧打码 + 判定回复要不要审查
        api = "anthropic" if use_x_api_key or path.endswith("/messages") else "openai"
        inspectable = pool.guard_on() and (path.endswith("/chat/completions") or path.endswith("/messages"))
        if pool.guard_on() and body:
            extra = [k for n in pool.nodes.values() for k in (n.key, n.pay_key) if k] + list(pool.master_keys)
            do_mask = bool(settings.get("maskSecrets", True))
            new_body, req_findings = guard.scan_request(body, extra, do_mask)
            if req_findings:
                body = new_body
                kinds = "、".join(sorted({f["kind"] for f in req_findings}))
                pool.sec_event("masked" if do_mask else "warned", None,
                               ("请求里发现密钥已打码：" if do_mask else "请求里发现密钥（未打码）：") + kinds, req_findings, path)
        declared = guard.declared_tool_names(body) if inspectable else set()
        tried = set()
        last = None  # (status, headers, body)
        attempt, again = 0, None   # again：同一个节点换另一路重发 (节点, 线路, 已试过的线路)
        while again or attempt < max_attempts:
            if again:
                (node, route, routes_tried), again = again, None
            else:
                node = pool.acquire(tried, pin, asked, strict)
                if node is None:
                    break
                attempt += 1
                tried.add(node.id)
                routes_tried = set()
                route = node.route(time.time(), pool.hint_ttl(), model=asked, strict=strict)
                if route is None:   # 刚好在这一刻被底线挡住
                    pool.release(node)
                    continue
            routes_tried.add(route)
            started = time.time()
            model = node.sends(asked, route, strict)
            send_body = body
            if asked is not None and model != asked:
                try:  # 用打码之后的请求体改写模型名
                    rewritten = json.loads(body.decode("utf-8"))
                    rewritten["model"] = model
                    send_body = json.dumps(rewritten, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                except (ValueError, AttributeError, TypeError):
                    send_body = body
            out_cap = pool.stations[node.station].max_output
            if out_cap and send_body:   # 对方超过上限就整个请求 400，换节点也没用：压到上限再发
                send_body, over = clamp_output(send_body, out_cap)
                if over:
                    sys.stderr.write("%s output cap: %s -> %d (%s %s %s stream=%s bytes=%d)\n" % (
                        time.strftime("%Y-%m-%d %H:%M:%S"), over, out_cap, node.name, route, path, stream, len(send_body)))
                    sys.stderr.flush()
            try:
                wait = float(settings["timeoutSeconds"])
                first_wait, idle_wait = stream_waits(settings) if stream else (0, 0)
                upstream = self.open_upstream(node, send_body, use_x_api_key, first_wait or wait, node.route_key(route))
            except Exception as exc:  # 连不上、超时、TLS 等
                pool.release(node)
                detail = getattr(exc, "reason", None) or exc
                pool.mark_fail(node, "network", detail)
                pool.record(node, "network", int((time.time() - started) * 1000), model, stream, attempt, False,
                            route=route, path=path, asked=asked)
                last = (502, None, json.dumps({"error": {"message": "upstream %s unreachable: %s" % (node.name, detail),
                                                         "type": "upstream_error"}}, ensure_ascii=False).encode("utf-8"))
                continue
            status = upstream.status if hasattr(upstream, "status") else upstream.code
            first_ms = int((time.time() - started) * 1000)
            has_more = attempt < max_attempts and any(n.enabled and n.id not in tried for n in pool.nodes.values())
            if status >= 300:
                # 不是成功的回答：先读出报错正文记进日志（打码），再接回去，下面各分支照常读
                try:
                    text = upstream.read(65536)
                except Exception:
                    text = b""
                pool.log_upstream_error(node, route, path, status, text)
                upstream = _Replay(upstream, text)
            if 300 <= status < 400:
                # 重定向多半是站点换了地址。不跟着跳（会把 Key 发到新地址）：立刻停用这个节点，换别的节点；地址由人确认后再改
                location = (upstream.headers.get("Location") or "?")[:160]
                upstream.close()
                pool.release(node)
                pool.mark_fail(node, status, "上游重定向到 %s：站点可能换了地址，人工确认后再改节点地址" % location, force=True)
                pool.record(node, status, first_ms, model, stream, attempt, False, route=route, path=path, asked=asked)
                last = (502, None, json.dumps({"error": {"message": "upstream %s redirected (%d) to %s; check the node address"
                                                         % (node.name, status, location), "type": "upstream_error"}},
                                              ensure_ascii=False).encode("utf-8"))
                continue
            if status == 429:
                try:
                    text = upstream.read(65536)
                except Exception:
                    text = b""
                if any(word in text.lower() for word in IP_LIMIT_WORDS):
                    # 对方限的是这台机器的 IP：整个站先停一会儿。这个请求先交给别的站；没有别的站能接就等暂停结束再发
                    try:
                        retry_after = float(upstream.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        retry_after = 0
                    upstream.close()
                    pool.release(node)
                    pool.mark_ip_limited(node, status, text[:120].decode("utf-8", "replace"), retry_after)
                    pool.record(node, status, first_ms, model, stream, attempt, False, route=route, path=path, asked=asked)
                    last = (status, upstream.headers, text)
                    continue
                upstream = _Replay(upstream, text)
            if node.dual() and status in (401, 402, 403, 429):
                # 双路节点：这一路被拒可能只是这个 Key 的额度用完了，同一个节点换另一路重发
                try:
                    text = upstream.read(65536)
                except Exception:
                    text = b""
                lowered = text.decode("utf-8", "replace").lower()
                if status != 429 or any(word in lowered for word in QUOTA_WORDS):   # 单纯的 429 是限流，不是没钱
                    pool.mark_route_fail(node, route, status, text[:120].decode("utf-8", "replace"))
                    other = node.route(time.time(), pool.hint_ttl(), skip=routes_tried, model=asked, strict=strict)
                    if other:
                        upstream.close()
                        pool.record(node, status, first_ms, model, stream, attempt, False, route=route, path=path, asked=asked)
                        again = (node, other, routes_tried)
                        continue
                upstream = _Replay(upstream, text)
            if status in CONTENT_STATUS:
                try:
                    text = upstream.read(65536)
                except Exception:
                    text = b""
                if any(word in text.lower() for word in CONTENT_WORDS):
                    # 内容过滤拒的是这个请求本身：同一个站的其它节点结果一样，全部跳过，直接换别的站；都没有就把原话返回
                    upstream.close()
                    pool.release(node)
                    tried |= pool.mark_refused(node, status, text[:160].decode("utf-8", "replace"))
                    pool.capture_refused(node, route, self.path, use_x_api_key, status, text, send_body)
                    pool.record(node, status, first_ms, model, stream, attempt, False, route=route, path=path, asked=asked)
                    last = (status, upstream.headers, text)
                    continue
                upstream = _Replay(upstream, text)
            if status in retry_on and has_more:
                try:
                    text = upstream.read(65536)
                except Exception:
                    text = b""
                upstream.close()
                pool.release(node)
                # 双路节点走到这里说明两个 Key 都被拒了：不用再攒次数
                pool.mark_fail(node, status, text[:200].decode("utf-8", "replace"),
                               force=node.dual() and status in (401, 402, 403))
                pool.record(node, status, first_ms, model, stream, attempt, False, route=route, path=path, asked=asked)
                last = (status, upstream.headers, text)
                continue
            if status in (400, 404) and asked and not pin:
                # 可能是“这个节点没有这个模型”：404 直接算；400 要回复里提到 model 才算（否则是客户端请求本身有问题）
                try:
                    text = upstream.read(65536)
                except Exception:
                    text = b""
                if status == 404 or b"model" in text.lower():
                    upstream.close()
                    pool.release(node)
                    pool.mark_unsupported(node, model, route)
                    pool.record(node, status, first_ms, model, stream, attempt, False, route=route, path=path, asked=asked)
                    last = (status, upstream.headers, text)
                    # 双路节点：另一路的 Key 可能有这个模型，同一个节点换路再试
                    other = node.route(time.time(), pool.hint_ttl(), skip=routes_tried, model=asked, strict=strict)
                    if other and not node.lacks(other, asked, time.time(), strict):
                        pool.retake(node)
                        again = (node, other, routes_tried)
                        continue
                    if has_more:
                        continue
                    break
                upstream = _Replay(upstream, text)
            stream_failed = None   # 流的首段就是上游错误时，记下错误原文
            if stream and status < 400:
                # 先读到第一条带内容的事件再开始转发：这时客户端还什么都没收到，首段是错误、
                # 或者从发出起 first_wait 秒都没有内容，就能悄悄换节点重发
                try:
                    head = peek_first_content(upstream, started + first_wait)
                    set_read_timeout(upstream, idle_wait)   # 开始转发：之后按“两段之间断流多久”算
                except FirstContentTimeout:
                    waited = time.time() - started
                    upstream.close()
                    pool.release(node)
                    detail = "首段 %d 秒内没有内容（只等到响应头或不带内容的事件），换下一个节点" % first_wait
                    sys.stderr.write("%s first content timeout: node=%s route=%s waited=%.0fs path=%s\n" % (
                        time.strftime("%Y-%m-%d %H:%M:%S"), node.name, route, waited, path))
                    sys.stderr.flush()
                    pool.mark_fail(node, "network", detail)
                    pool.record(node, "network", int(waited * 1000), model, stream, attempt, False,
                                route=route, path=path, asked=asked)
                    last = (504, None, json.dumps({"error": {
                        "message": "key-pool: upstream %s sent no content within %d s (last node tried)"
                                   % (node.name, first_wait), "type": "upstream_timeout"}},
                        ensure_ascii=False).encode("utf-8"))
                    continue
                except Exception as exc:  # 等第一条事件时断开：和连不上一样换节点
                    upstream.close()
                    pool.release(node)
                    pool.mark_fail(node, "network", "等流的首段时出错：%s" % exc)
                    pool.record(node, "network", int((time.time() - started) * 1000), model, stream, attempt, False,
                                route=route, path=path, asked=asked)
                    last = (502, None, json.dumps({"error": {"message": "upstream %s stream broke before first event: %s"
                                                             % (node.name, exc), "type": "upstream_error"}},
                                                  ensure_ascii=False).encode("utf-8"))
                    continue
                stream_failed = stream_error(head)
                if stream_failed is not None:
                    pool.log_upstream_error(node, route, path, "stream", head)
                    lowered = head.lower()
                    detail = "流的首段是上游错误：" + stream_failed[:160]
                    if any(word in lowered for word in CONTENT_WORDS):
                        code = 500
                    elif any(word in lowered for word in BURST_WORDS):
                        code = 429
                    else:
                        code = 502
                    if has_more:
                        upstream.close()
                        pool.release(node)
                        if code == 500:   # 内容过滤：同一个站的其它节点结果一样，跳过整个站
                            tried |= pool.mark_refused(node, "stream", stream_failed[:160])
                        elif any(word in lowered for word in IP_LIMIT_WORDS):
                            pool.mark_ip_limited(node, code, stream_failed[:120])
                        else:
                            pool.mark_fail(node, code, detail)
                        pool.record(node, code, first_ms, model, stream, attempt, False, route=route, path=path,
                                    asked=asked)
                        last = (code, None, json.dumps({"error": {"message": "upstream %s: %s" % (node.name, detail),
                                                                  "type": "upstream_error"}},
                                                       ensure_ascii=False).encode("utf-8"))
                        continue
                    if code != 500:   # 没有别的节点可换：原样转给客户端，但这次算失败
                        pool.mark_fail(node, code, detail + "（最后一次尝试，原样返回给客户端）")
                upstream = _Replay(upstream, head)
            ok = status < 400 and stream_failed is None
            if ok:
                pool.mark_ok(node, first_ms, route)
            elif stream_failed is not None:
                pass
            elif status in retry_on:
                pool.mark_fail(node, status, "最后一次尝试，原样返回给客户端")
            first_chunk, verdict = None, None
            tap = stats.UsageTap() if pool.usage_dir and ok else None
            do_inspect = inspectable and ok
            self._last_chunk_at, self.max_gap_ms = None, 0
            try:
                if do_inspect:
                    first_chunk, verdict = self.relay_inspected(upstream, status, api, declared, model, tap)
                else:
                    first_chunk = self.relay(upstream, status, tap)
            except (socket.timeout, TimeoutError):
                # 已经转发了一部分，没法悄悄换节点重发：断开，并在流里给客户端一条明确的错误
                self.close_connection = True
                ok = False
                waited = time.time() - (self._last_chunk_at or started)
                if stream:
                    sys.stderr.write("%s stream idle timeout: node=%s route=%s waited=%.0fs path=%s\n" % (
                        time.strftime("%Y-%m-%d %H:%M:%S"), node.name, route, waited, path))
                    sys.stderr.flush()
                    pool.mark_fail(node, "network", "转发中断流超过 %d 秒，已断开" % idle_wait)
                if stream and not upstream.headers.get("Content-Length"):   # 分块转发的流才能补一条错误事件
                    notice = ("data: " + json.dumps({"error": {
                        "message": "key-pool: upstream %s sent nothing for %d s mid-stream; connection closed"
                                   % (node.name, idle_wait), "type": "upstream_timeout"}}, ensure_ascii=False)
                              + "\n\n").encode("utf-8")
                    try:
                        self.wfile.write(b"%x\r\n" % len(notice) + notice + b"\r\n0\r\n\r\n")
                        self.wfile.flush()
                    except OSError:
                        pass
            except Exception:
                self.close_connection = True
                ok = False
            finally:
                upstream.close()
                pool.release(node)
                total_ms = int((time.time() - started) * 1000)
                ttfb_ms = int((first_chunk - started) * 1000) if first_chunk else None
                buffered = pool.timing(node, ttfb_ms, total_ms, stream) if ok else False
                usage = None
                if tap and ok:
                    try:
                        usage = tap.usage()
                    except Exception:
                        usage = None
                pool.record(node, status, first_ms, model, stream, attempt, ok, ttfb_ms, total_ms, path, buffered, route,
                            asked=asked, usage=usage, gap=self.max_gap_ms if stream else None)
                if do_inspect:
                    pool.sec_counts["checked"] += 1
                    if verdict and verdict.get("block"):
                        pool.quarantine(node, verdict["findings"], path)
                    elif verdict and verdict.get("findings"):
                        warns = [f for f in verdict["findings"] if f.get("severity") == "warn"]
                        if warns:
                            pool.sec_event("warned", node, "；".join(sorted({f["label"] for f in warns})), warns, path)
            return
        if last is None:
            pool.record(None, 503, 0, model, stream, 0, False)
            return self.client_error(503, "key-pool: no upstream available (all disabled, busy or not configured)",
                                     "overloaded_error")
        code, headers, text = last
        self.send_response(code)
        self.send_header("Content-Type", (headers.get("Content-Type") if headers else None)
                         or "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(text)))
        self.end_headers()
        self.wfile.write(text)

    def open_upstream(self, node, body, use_x_api_key, timeout, key=None):
        key = key or node.key or node.pay_key
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        headers["Accept-Encoding"] = "identity"
        if node.user_agent:
            headers = {k: v for k, v in headers.items() if k.lower() != "user-agent"}
            headers["User-Agent"] = node.user_agent   # 节点指定了客户端标识
        elif not any(k.lower() == "user-agent" for k in headers):
            headers["User-Agent"] = CLIENT_UA  # 客户端没带标识：不让 urllib 填默认的 Python-urllib
        if use_x_api_key:
            headers["x-api-key"] = key
        else:
            headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(join_url(node.base_url, self.path), data=body, headers=headers,
                                         method=self.command)
        try:
            return self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:  # 4xx/5xx 也是“上游的回答”，交给调用方判断
            return exc

    def note_chunk(self):
        """记下两段上游数据之间最长隔了多久（毫秒），写进用量记录，供以后调断流超时用。"""
        now = time.time()
        last = getattr(self, "_last_chunk_at", None)
        if last is not None:
            self.max_gap_ms = max(getattr(self, "max_gap_ms", 0), int((now - last) * 1000))
        self._last_chunk_at = now

    def relay(self, upstream, status, tap=None):
        self.send_response(status)
        length = upstream.headers.get("Content-Length")
        encoding = (upstream.headers.get("Content-Encoding") or "").lower()
        for key, value in upstream.headers.items():
            if key.lower() not in HOP_RES:
                self.send_header(key, value)
        if encoding and encoding != "identity":
            self.send_header("Content-Encoding", encoding)
        chunked = length is None or self.command == "HEAD"
        if self.command == "HEAD":
            self.send_header("Content-Length", length or "0")
            self.end_headers()
            return None
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", length)
        self.end_headers()
        reader = getattr(upstream, "read1", None) or upstream.read
        first_chunk = None
        while True:
            chunk = reader(65536)
            if not chunk:
                break
            self.note_chunk()
            if first_chunk is None:
                first_chunk = time.time()
            if tap:
                tap.feed(chunk)
            if chunked:
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            else:
                self.wfile.write(chunk)
            self.wfile.flush()
        if chunked:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        return first_chunk

    def relay_inspected(self, upstream, status, api, declared, model, tap=None):
        """带安全检测地转发回复。返回 (首块时间, verdict)。verdict 为 None 表示无命中。"""
        ctype = (upstream.headers.get("Content-Type") or "").lower()
        is_sse = "text/event-stream" in ctype
        self.send_response(status)
        for key, value in upstream.headers.items():
            if key.lower() not in HOP_RES:
                self.send_header(key, value)
        first = [None]

        def write_chunk(data):
            if not data:
                return
            if first[0] is None:
                first[0] = time.time()
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()

        if is_sse:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            insp = guard.StreamInspector(api, declared, model or "")
            reader = getattr(upstream, "read1", None) or upstream.read
            while True:
                chunk = reader(65536)
                if not chunk:
                    break
                self.note_chunk()
                if tap:
                    tap.feed(chunk)
                write_chunk(insp.feed(chunk))
            write_chunk(insp.finish())
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            verdict = {"block": insp.blocked, "findings": insp.findings}
        else:
            body = upstream.read()
            if tap:
                tap.feed(body)
            verdict, _ = guard.inspect_json(body, declared)
            if verdict["block"]:
                body = guard.blocked_message(verdict["findings"], api, model or "", False)
            first[0] = time.time()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        return first[0], (verdict if (verdict["block"] or verdict["findings"]) else None)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_any


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128  # 默认只有 5：客户端一下子并发连进来多于这个数时，多出来的连接会被系统直接重置


def make_server(config_path, port, admin_token, host="127.0.0.1", usage_dir=None):
    pool = Pool(config_path, usage_dir)
    if usage_dir:
        stats.prune(usage_dir)
    handler = type("KeyPoolHandler", (Handler,), {"pool": pool, "admin_token": admin_token})
    server = Server((host, port), handler)
    server.pool = pool

    def watch():  # 文件变了就重新加载（请求进来时也会检查）
        while True:
            time.sleep(1)
            pool.reload()

    threading.Thread(target=watch, name="keypool-watch", daemon=True).start()
    return server


def main():
    parser = argparse.ArgumentParser(description="小八监看中心 · Key 池代理")
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--usage-dir", default="")
    args = parser.parse_args()
    token = os.environ.get("KEYPOOL_ADMIN_TOKEN", "")
    if len(token) < 16:
        parser.error("缺少管理令牌（KEYPOOL_ADMIN_TOKEN）")
    socket.setdefaulttimeout(None)
    server = make_server(args.config, args.port, token, usage_dir=args.usage_dir or None)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    print("Key pool listening on 127.0.0.1:%d (pid %d)" % (args.port, os.getpid()), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
