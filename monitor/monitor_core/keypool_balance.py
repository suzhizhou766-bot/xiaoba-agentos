"""中转站账户余额查询（清枢智汇 / tierflow 的网页会话接口）。

节点的 API Key 查不了余额，要用网页登录后的 Session Cookie 和 TF-User。这两个值与 Key 一样
只保存在 keypool.json（权限 600），只发往节点自己的站点，不跟随重定向，也不会返回给页面。

余额规则（2026-10-01 起）：
  总余额(元) = quota ÷ quota_per_unit
             + Σ[ status == "active" 且未到 end_time 的订阅：max(0, amount_total − amount_used) ÷ quota_per_unit ]
  结果保留 4 位小数。基础 token 剩余 = Σ max(0, basic_token_total − basic_token_used)，只作参考，不折算金额。
  套餐接口失败时降级：只报按量部分并注明。
"""
import datetime
import json
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_UNIT = 500000
STATUS_TTL = 600   # 换算单位几乎不变
PLAN_TTL = 60      # 套餐额度变化慢；按量余额每次都查
MAX_BODY = 1024 * 1024


class BalanceError(Exception):
    """可以直接显示给用户的说明，不含凭据和上游原始响应。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def site_origin(base_url):
    """余额接口在节点站点的根路径下；只接受 https，避免会话凭据明文传输。"""
    parsed = urllib.parse.urlparse(str(base_url or ""))
    if parsed.scheme != "https" or not parsed.hostname:
        raise BalanceError("余额查询只支持 https 地址的节点")
    return "https://" + parsed.netloc


def http_get(url, headers, timeout=12):
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
        return response.read(MAX_BODY + 1)


def _call(get, origin, path, headers, pause, retry=True):
    """HTTP 200 不代表成功，要看 success 字段；返回的不是 JSON 说明被限流，稍等重试一次。"""
    for attempt in ((0, 1) if retry else (1,)):
        try:
            raw = get(origin + path, headers)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise BalanceError("会话已失效或 TF-User 不对，请重新填写 Session Cookie 和 TF-User")
            if 300 <= exc.code < 400:
                raise BalanceError("站点返回重定向，已停止（会话可能已失效）")
            raise BalanceError("站点返回 HTTP %d" % exc.code)
        except BalanceError:
            raise
        except Exception:
            raise BalanceError("连不上站点或超时")
        try:
            data = json.loads(raw[:MAX_BODY].decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, UnicodeDecodeError):
            if attempt == 0:
                time.sleep(pause)
                continue
            raise BalanceError("被限流了，稍后再试")
        if data.get("success") is not True:
            raise BalanceError("站点拒绝了请求，会话可能已失效")
        return data.get("data")
    raise BalanceError("被限流了，稍后再试")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def _expired(end_time, now):
    """end_time 可能是秒、毫秒或日期字符串；没有或为 0 表示不限期。"""
    if isinstance(end_time, str) and end_time.strip():
        text = end_time.strip()
        try:
            end_time = float(text)
        except ValueError:
            try:
                moment = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return False
            if moment.tzinfo is None:
                moment = moment.astimezone()
            return moment.timestamp() <= now
    if isinstance(end_time, bool) or not isinstance(end_time, (int, float)) or end_time <= 0:
        return False
    if end_time > 1e12:
        end_time /= 1000.0
    return end_time <= now


def compute(user, status, subscriptions, now=None):
    """按规则算出余额。subscriptions 为 None 表示套餐接口失败（降级）。"""
    now = time.time() if now is None else now
    unit = _number((status or {}).get("quota_per_unit")) if isinstance(status, dict) else 0
    if unit <= 0:
        unit = DEFAULT_UNIT
    if not isinstance(user, dict) or isinstance(user.get("quota"), bool) or not isinstance(user.get("quota"), (int, float)):
        raise BalanceError("站点没有返回按量余额")
    pay_as_you_go = user["quota"] / unit
    plan, tokens, active = 0.0, 0, 0
    degraded = subscriptions is None
    for item in subscriptions or []:
        sub = item.get("subscription") if isinstance(item, dict) else None
        if not isinstance(sub, dict):
            continue
        tokens += max(0, _number(sub.get("basic_token_total")) - _number(sub.get("basic_token_used")))
        if sub.get("status") == "active" and not _expired(sub.get("end_time"), now):
            plan += max(0, _number(sub.get("amount_total")) - _number(sub.get("amount_used"))) / unit
            active += 1
    return {"total": round(pay_as_you_go + plan, 4), "payAsYouGo": round(pay_as_you_go, 4), "plan": round(plan, 4),
            "activePlans": active, "basicTokens": int(tokens), "degraded": degraded,
            "note": "套餐接口失败，只统计了按量余额" if degraded else ""}


def query(base_url, cookie, tf_user, get=http_get, pause=1.5, now=None, retry=True, cache=None):
    """cache 是这个节点专用的字典：自动刷新时用它少发请求（换算单位和套餐不必每 3 秒都查）。"""
    origin = site_origin(base_url)
    clock = time.time() if now is None else now
    cache = {} if cache is None else cache

    def cached(name, ttl):
        entry = cache.get(name)
        return entry if entry and clock - entry[1] < ttl else None

    headers = {"Cookie": "session=" + cookie, "TF-User": tf_user, "Accept": "application/json",
               "Accept-Encoding": "identity", "User-Agent": "xiaoba-keypool-balance"}
    user = _call(get, origin, "/api/user/self", headers, pause, retry)
    entry = cached("status", STATUS_TTL)
    if entry:
        status = entry[0]
    else:
        try:
            status = _call(get, origin, "/api/status", headers, pause, retry)
            cache["status"] = (status, clock)
        except BalanceError:
            status = None  # 取不到换算单位按 500000 算
    entry = cached("plans", PLAN_TTL)
    if entry:
        return compute(user, status, entry[0], now)
    try:
        data = _call(get, origin, "/api/subscription/self", headers, pause, retry)
        subscriptions = data.get("subscriptions") if isinstance(data, dict) else None
        if isinstance(data, dict) and subscriptions is None:
            subscriptions = []  # 没买套餐：不是失败
        elif not isinstance(subscriptions, list):
            subscriptions = None
    except BalanceError:
        subscriptions = None
    if subscriptions is not None:
        cache["plans"] = (subscriptions, clock)
    return compute(user, status, subscriptions, now)
