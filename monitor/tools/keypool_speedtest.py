#!/usr/bin/env python3
"""Key 池测速：每个节点“直连中转站”和“经过 Key 池”各发一个极短的流式请求，对比首字时间。

在自己的终端里运行（会消耗很少一点额度）：
  cd "/Users/suzhizhou/Documents/私人助理/工具/小八监看"
  /usr/bin/python3 tools/keypool_speedtest.py --model tierflow_pro
  /usr/bin/python3 tools/keypool_speedtest.py --model 模型名 --api anthropic   # 客户端用 /v1/messages 时

Key 从本机 keypool.json 读取，只用于发请求，不会打印到屏幕上。
需要监看中心开着（经过 Key 池那一路要用到它）。

看结果：
  - “直连”和“经 Key 池”首字差不多 → Key 池没有拖慢，慢在中转站或模型本身；
  - 首字 ≈ 总耗时（标“整段返回”）→ 这个中转站没有真正流式输出，换节点或问中转站；
  - 只有“经 Key 池”慢 → Key 池或本机网络代理的问题，把输出发给 Claude 看。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from monitor_core.keypool_proxy import join_url  # noqa: E402


def one(url, key, api, model, headers_extra=None, timeout=300):
    if api == "anthropic":
        path, body = "/v1/messages", {"model": model, "max_tokens": 16, "stream": True,
                                      "messages": [{"role": "user", "content": "Reply with the single word: ok"}]}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        path, body = "/v1/chat/completions", {"model": model, "max_tokens": 16, "stream": True,
                                              "messages": [{"role": "user", "content": "Reply with the single word: ok"}]}
        headers = {"Authorization": "Bearer " + key}
    headers.update({"Content-Type": "application/json", "Accept": "text/event-stream", "Accept-Encoding": "identity",
                    "User-Agent": "xiaoba-keypool/2.0"})
    headers.update(headers_extra or {})
    request = urllib.request.Request(join_url(url, path), data=json.dumps(body).encode("utf-8"), headers=headers,
                                     method="POST")
    started = time.time()
    result = {"status": None, "head": None, "first": None, "text": None, "total": None, "error": None}
    try:
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        result["status"] = response.status if hasattr(response, "status") else response.code
        result["head"] = time.time() - started
        reader = getattr(response, "read1", None) or response.read
        buffer = b""
        while True:
            chunk = reader(65536)
            if not chunk:
                break
            now = time.time() - started
            if result["first"] is None:
                result["first"] = now
            buffer += chunk
            if result["text"] is None and has_text(buffer):
                result["text"] = now
        result["total"] = time.time() - started
        if result["status"] and result["status"] >= 400:
            result["error"] = buffer[:160].decode("utf-8", "replace").replace("\n", " ")
    except Exception as exc:
        result["error"] = str(getattr(exc, "reason", None) or exc)[:160]
        result["total"] = time.time() - started
    return result


def has_text(buffer):
    """SSE 里出现第一段非空文字（OpenAI 的 delta.content 或 Anthropic 的 text_delta）。"""
    for line in buffer.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        try:
            data = json.loads(line[5:].strip())
        except ValueError:
            continue
        for choice in data.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content") or delta.get("reasoning_content"):
                return True
        delta = data.get("delta") or {}
        if delta.get("text") or delta.get("thinking"):
            return True
    return False


def fmt(value):
    return "—" if value is None else ("%.2fs" % value if value < 100 else "%.0fs" % value)


def main():
    parser = argparse.ArgumentParser(description="Key 池测速：直连 vs 经 Key 池")
    parser.add_argument("--model", required=True, help="要测的模型名，和客户端里用的一致")
    parser.add_argument("--api", choices=["openai", "anthropic"], default="openai",
                        help="openai = /v1/chat/completions（默认），anthropic = /v1/messages")
    parser.add_argument("--node", default=None, help="只测某个节点（名称）")
    parser.add_argument("--config", default=str(ROOT / "keypool.json"))
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--skip-pool", action="store_true", help="只测直连")
    args = parser.parse_args()

    data = json.loads(Path(args.config).read_text(encoding="utf-8"))
    nodes = [n for n in data.get("nodes", []) if n.get("enabled", True) is not False]
    if args.node:
        nodes = [n for n in nodes if args.node in (n.get("name"), n.get("id"))]
    if not nodes:
        print("没有可测的节点。")
        return 1
    print("模型 %s · 接口 %s · %d 个节点（每个请求最多输出 16 个 token）\n" % (args.model, args.api, len(nodes)))
    print("%-14s %-8s %6s %8s %8s %8s %8s  %s" % ("节点", "线路", "状态", "响应头", "首包", "首字", "总耗时", "备注"))
    for node in nodes:
        routes = [("直连", node["baseUrl"], node["key"], None)]
        if not args.skip_pool:
            routes.append(("经Key池", "http://127.0.0.1:%d" % args.port, data["masterKey"], {"X-KeyPool-Node": node["id"]}))
        for label, url, key, extra in routes:
            r = one(url, key, args.api, args.model, extra)
            note = r["error"] or ""
            if not note and r["first"] and r["total"] and r["total"] >= 3 and r["first"] >= r["total"] * 0.85:
                note = "整段返回（不是真流式）"
            print("%-14s %-8s %6s %8s %8s %8s %8s  %s" % (str(node.get("name"))[:14], label, r["status"] or "—",
                                                      fmt(r["head"]), fmt(r["first"]), fmt(r["text"]), fmt(r["total"]), note))
    print("\n响应头 = 上游开始回应；首包 = 第一块数据；首字 = 第一段文字。直连与经 Key 池差别在 0.1 秒内属正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
