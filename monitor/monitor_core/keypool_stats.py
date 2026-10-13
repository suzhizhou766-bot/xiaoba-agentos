"""Key 池用量统计：记录怎么写、怎么读、怎么汇总。只用标准库。

记录由 Key 池代理写（每次尝试一行），监看中心读。只记元数据——时间、节点、线路、模型、接口路径、
状态、耗时、token 数——不记请求和回复的内容，也不记任何 Key。

文件：<用量目录>/usage-YYYY-MM-DD.jsonl（按本机日期分文件，默认留 90 天）
      <用量目录>/balance.jsonl（余额读数变化时记一行，用来估算花了多少钱）
"""
import csv
import io
import json
import os
import threading
import time

KEEP_DAYS = 90
HEAD_BYTES = 16 * 1024      # 回复开头留多少用来找 token 数（Anthropic 流式把输入 token 放在开头）
TAIL_BYTES = 48 * 1024      # 回复结尾留多少（OpenAI 流式把用量放在最后一块）
WHOLE_BYTES = 512 * 1024    # 小于这个的回复整段保留
FIELDS = ("t", "node", "name", "route", "model", "asked", "path", "stream", "status", "ok", "attempt",
          "ms", "ttfb", "total", "in", "out", "cache")
_write_lock = threading.Lock()


# ---------- 从回复里找 token 数 ----------
class UsageTap:
    """转发回复时顺手留下开头和结尾，结束后从里面找用量。不改动转发的内容。"""

    def __init__(self):
        self.head, self.tail, self.size = b"", b"", 0

    def feed(self, chunk):
        if not chunk:
            return
        self.size += len(chunk)
        if len(self.head) < HEAD_BYTES:
            room = HEAD_BYTES - len(self.head)
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail = (self.tail + chunk)[-WHOLE_BYTES:]

    def usage(self):
        if self.size <= len(self.head) + len(self.tail):      # 没丢东西：整段都在
            return extract_usage(self.head + self.tail)
        return extract_usage(self.head + b"\n" + self.tail[-TAIL_BYTES:])


def _int(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _merge(found, usage):
    """把一段 usage 并进结果：OpenAI 与 Anthropic 两种写法都认。"""
    if not isinstance(usage, dict):
        return
    for key, names in (("in", ("prompt_tokens", "input_tokens")), ("out", ("completion_tokens", "output_tokens"))):
        for name in names:
            value = _int(usage.get(name))
            if value is not None:
                found[key] = max(found.get(key) or 0, value)   # 流式里同一个数会重复出现，取最大的
    details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    for value in (details.get("cached_tokens"), usage.get("cache_read_input_tokens"), usage.get("prompt_cache_hit_tokens")):
        value = _int(value)
        if value is not None:
            found["cache"] = max(found.get("cache") or 0, value)


def extract_usage(data):
    """返回 {"in", "out", "cache"}（没找到的项为 None）。认整段 JSON，也认流式（SSE）的 data: 行。"""
    found = {}
    text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data or "")
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            _merge(found, parsed.get("usage"))
        except (ValueError, AttributeError):
            pass
    if not found and "usage" in text:
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:") or '"usage"' not in line:
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue                       # 被截断的一行
            if isinstance(event, dict):
                _merge(found, event.get("usage"))
                if isinstance(event.get("message"), dict):       # Anthropic message_start
                    _merge(found, event["message"].get("usage"))
    return {"in": found.get("in"), "out": found.get("out"), "cache": found.get("cache")}


# ---------- 写 ----------
def day_file(folder, when):
    return os.path.join(folder, "usage-%s.jsonl" % time.strftime("%Y-%m-%d", time.localtime(when)))


def _append(path, record):
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _write_lock:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)


def append_usage(folder, record):
    _append(day_file(folder, record["t"]), {k: record.get(k) for k in FIELDS})


def append_balance(folder, node_id, pay, plan, when=None):
    _append(os.path.join(folder, "balance.jsonl"), {"t": round(when or time.time(), 3), "node": node_id, "pay": pay, "plan": plan})


def prune(folder, keep_days=KEEP_DAYS, now=None):
    """删掉超过保留期的按日文件。返回删了几个。"""
    cutoff = time.strftime("%Y-%m-%d", time.localtime((now or time.time()) - keep_days * 86400))
    removed = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    for name in names:
        if name.startswith("usage-") and name.endswith(".jsonl") and name[6:16] < cutoff:
            try:
                os.unlink(os.path.join(folder, name))
                removed += 1
            except OSError:
                pass
    return removed


# ---------- 读 ----------
def _lines(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("t"), (int, float)):
                    yield row
    except OSError:
        return


def load(folder, since, until=None):
    """读出时间范围内的记录，按时间从旧到新。"""
    until = until or time.time() + 1
    rows = []
    day = since - 86400
    while day <= until + 86400:
        rows.extend(r for r in _lines(day_file(folder, day)) if since <= r["t"] <= until)
        day += 86400
    rows.sort(key=lambda r: r["t"])
    return rows


FILTERS = ("node", "model", "route", "path")


def matches(row, filters):
    for key in FILTERS:
        want = filters.get(key)
        if want and str(row.get(key) or "") != want:
            return False
    result = filters.get("result")
    if result == "ok" and not row.get("ok"):
        return False
    if result == "fail" and row.get("ok"):
        return False
    return True


def _tokens(row):
    return (row.get("in") or 0) + (row.get("out") or 0)


def _share(rows, key, label=None):
    groups = {}
    for row in rows:
        name = str(row.get(key) or "（未知）")
        item = groups.setdefault(name, {"key": name, "label": name, "requests": 0, "ok": 0, "tokens": 0})
        if label:
            item["label"] = str(row.get(label) or name)
        item["requests"] += 1
        item["ok"] += 1 if row.get("ok") else 0
        item["tokens"] += _tokens(row)
    return sorted(groups.values(), key=lambda g: (-g["requests"], g["label"]))[:30]


def summarize(rows, since, until, bucket):
    """汇总：总数、各种分布、按时间的趋势。bucket 是一格多少秒（3600 或 86400）。"""
    good = [r for r in rows if r.get("ok")]
    timed = [r["total"] for r in good if isinstance(r.get("total"), (int, float))]
    first = [r["ttfb"] for r in good if isinstance(r.get("ttfb"), (int, float))]
    counted = [r for r in good if r.get("in") is not None or r.get("out") is not None]
    # 一次客户端请求可能换线路重试多次：attempt 为 1 的才算一次新的请求
    calls = sum(1 for r in rows if (r.get("attempt") or 1) <= 1)
    offset = time.localtime(since).tm_gmtoff or 0           # 按本机时区对齐到整点 / 整天
    start = int((since + offset) // bucket) * bucket - offset
    buckets = {}
    slot = start
    while slot <= until and len(buckets) < 800:
        buckets[slot] = {"t": slot, "requests": 0, "fail": 0, "in": 0, "out": 0}
        slot += bucket
    for row in rows:
        cell = buckets.get(int((row["t"] + offset) // bucket) * bucket - offset)
        if cell is None:
            continue
        cell["requests"] += 1
        cell["fail"] += 0 if row.get("ok") else 1
        cell["in"] += row.get("in") or 0
        cell["out"] += row.get("out") or 0
    return {
        "requests": len(rows), "calls": calls, "succeeded": len(good), "fail": len(rows) - len(good),
        "tokensIn": sum(r.get("in") or 0 for r in good), "tokensOut": sum(r.get("out") or 0 for r in good),
        "tokensCache": sum(r.get("cache") or 0 for r in good), "tokenCounted": len(counted),
        "avgMs": int(sum(timed) / len(timed)) if timed else None,
        "avgTtfbMs": int(sum(first) / len(first)) if first else None,
        "byModel": _share(rows, "model"), "byNode": _share(rows, "node", "name"),
        "byPath": _share(rows, "path"), "byRoute": _share([r for r in rows if r.get("route")], "route"),
        "trend": [buckets[key] for key in sorted(buckets)],
    }


def spend(folder, since, until=None):
    """按余额读数的下降估算花了多少钱：每个节点把相邻两次读数之间“变少的部分”加起来，变多（充值、套餐续期）不算。
    范围开始之前的最后一次读数作为起点。返回 {"total", "pay", "plan", "nodes": {节点: 金额}, "readings"}。"""
    until = until or time.time() + 1
    last, out, readings = {}, {"pay": 0.0, "plan": 0.0, "nodes": {}}, 0
    for row in _lines(os.path.join(folder, "balance.jsonl")):
        if row["t"] > until:
            break
        node = str(row.get("node") or "")
        before = last.get(node)
        if before is not None and row["t"] >= since:
            readings += 1
            for kind in ("pay", "plan"):
                a, b = before.get(kind), row.get(kind)
                if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b < a:
                    out[kind] += a - b
                    out["nodes"][node] = out["nodes"].get(node, 0.0) + (a - b)
        last[node] = row
    out["pay"], out["plan"] = round(out["pay"], 4), round(out["plan"], 4)
    out["total"] = round(out["pay"] + out["plan"], 4)
    out["nodes"] = {k: round(v, 4) for k, v in out["nodes"].items()}
    out["readings"] = readings
    return out


CSV_HEAD = ("时间", "节点", "线路", "实际模型", "客户端要的模型", "接口", "流式", "状态", "成功", "第几次尝试",
            "响应头毫秒", "首字毫秒", "总耗时毫秒", "输入token", "输出token", "缓存token")


def to_csv(rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_HEAD)
    routes = {"plan": "套餐", "pay": "余额"}
    for r in rows:
        writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["t"])), r.get("name") or "",
                         routes.get(r.get("route"), ""), r.get("model") or "", r.get("asked") or "", r.get("path") or "",
                         "是" if r.get("stream") else "否", r.get("status"), "是" if r.get("ok") else "否", r.get("attempt"),
                         r.get("ms"), r.get("ttfb"), r.get("total"), r.get("in"), r.get("out"), r.get("cache")])
    return "﻿" + out.getvalue()      # 带 BOM，Excel 直接打开不乱码
