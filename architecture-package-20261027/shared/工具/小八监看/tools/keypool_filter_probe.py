#!/usr/bin/env python3
"""找出是请求里的哪段内容触发了中转站的内容过滤（例如 tierflow 的 sensitive_words_detected）。

用法：
  1. 在 keypool.json 的 settings 里加 "captureRefused": true（代理自动重新加载）；
  2. 等下一个被内容过滤拒绝的请求，代理会把它存到 run/keypool-usage/refused-request.json；
  3. /usr/bin/python3 tools/keypool_filter_probe.py
  4. 查完删掉那个文件和旁边的 refused-request.probe.json，并把 captureRefused 去掉。

做法：把请求拆成一段段文字（每条消息、每个工具说明），先看哪一段单独发出去也会被拒，
再对这一段对半拆，一路缩到最短的那几个字。每次只要 1 个 token 的回复。

探测请求经本机的 Key 池发出（用合并 Key，指定回原来拒绝它的那个节点），所以和正常请求
共用同一套站点限速，不会把对方的 IP 限额占满；被限流时自己等。每个片段的结果随时存盘，
中途停掉后再运行会接着上次的进度。Key 只从本机的 keypool.json 读，不打印。
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from monitor_core.keypool_proxy import CONTENT_WORDS, join_url  # noqa: E402

MIN_LEN = 12        # 缩到这么短就停
PAUSE = 2.0         # 两次请求之间再隔一下（站点限速之外的余量）


class Prober:
    def __init__(self, capture, node, pool_url, master_key, cache_path, pause=PAUSE):
        self.model = json.loads(capture["body"]).get("model")
        self.anthropic = bool(capture.get("anthropic"))
        self.url = join_url(pool_url, capture["path"].split("?")[0])
        self.key, self.node = master_key, node["id"]
        self.pause, self.sent, self.cache_path = pause, 0, cache_path
        try:
            self.cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}

    def remember(self, key, value):
        self.cache[key] = value
        fd = os.open(self.cache_path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self.cache, handle)
        os.replace(self.cache_path + ".tmp", self.cache_path)

    def post(self, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept-Encoding": "identity", "X-KeyPool-Node": self.node,
                   "User-Agent": "xiaoba-keypool/2.0"}
        if self.anthropic:
            headers.update({"x-api-key": self.key, "anthropic-version": "2023-06-01"})
        else:
            headers["Authorization"] = "Bearer " + self.key
        for attempt in range(12):
            time.sleep(self.pause)
            self.sent += 1
            try:
                with urllib.request.urlopen(urllib.request.Request(self.url, data=data, headers=headers), timeout=300) as r:
                    return r.status, r.read(65536)
            except urllib.error.HTTPError as exc:
                text = exc.read(65536)
                if exc.code in (429, 503) and not any(w in text.lower() for w in CONTENT_WORDS):
                    time.sleep(20)   # 被限流，或者这个站正在暂停：多等一会儿再试
                    continue
                return exc.code, text
        raise SystemExit("一直被限流。已有的进度存好了，过一会儿再运行会接着来")

    def refused_body(self, body):
        body = dict(body, stream=False)
        body["max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"] = 1
        body.pop("stream_options", None)
        status, text = self.post(body)
        if any(w in text.lower() for w in CONTENT_WORDS):
            return True
        if status >= 400:
            raise SystemExit("探测请求出了别的错（HTTP %s）：%s" % (status, text[:300].decode("utf-8", "replace")))
        return False

    def refused(self, text):
        """这段文字单独作为一条用户消息发出去，会不会被拒。"""
        key = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if key not in self.cache:
            self.remember(key, self.refused_body({"model": self.model, "messages": [{"role": "user", "content": text}]}))
        return self.cache[key]

    def shrink(self, text):
        """把一段会被拒的文字缩到最短还会被拒的那一小段。"""
        while len(text) > MIN_LEN:
            mid = len(text) // 2
            if self.refused(text[:mid]):
                text = text[:mid]
            elif self.refused(text[mid:]):
                text = text[mid:]
            else:
                break   # 触发的词正好跨在中间：改成从两头一点点收
        low, high = 1, len(text)            # 最短的、还会被拒的开头
        while low < high:
            mid = (low + high) // 2
            low, high = (low, mid) if self.refused(text[:mid]) else (mid + 1, high)
        text = text[:low]
        low, high = 0, len(text) - 1        # 最靠后的、还会被拒的起点
        while low < high:
            mid = (low + high + 1) // 2
            low, high = (mid, high) if self.refused(text[mid:]) else (low, mid - 1)
        return text[low:]


def pieces(body):
    """请求里所有成段的文字：(说明, 文字)。"""
    out = []

    def text_of(content, label):
        if isinstance(content, str):
            out.append((label, content))
        elif isinstance(content, list):
            for i, part in enumerate(content):
                if isinstance(part, dict):
                    for field in ("text", "content"):
                        if isinstance(part.get(field), str):
                            out.append(("%s 第 %d 块" % (label, i + 1), part[field]))
    if isinstance(body.get("system"), (str, list)):
        text_of(body["system"], "system")
    for i, message in enumerate(body.get("messages") or []):
        label = "第 %d 条消息（%s）" % (i + 1, message.get("role"))
        text_of(message.get("content"), label)
        for call in message.get("tool_calls") or []:
            out.append((label + " 的工具调用", json.dumps(call, ensure_ascii=False)))
    for tool in body.get("tools") or []:
        spec = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        out.append(("工具 %s 的说明" % spec.get("name"), json.dumps(tool, ensure_ascii=False)))
    return [(label, text) for label, text in out if text.strip()]


def main():
    parser = argparse.ArgumentParser(description="找出触发中转站内容过滤的那段文字")
    parser.add_argument("--capture", default=str(ROOT / "run" / "keypool-usage" / "refused-request.json"))
    parser.add_argument("--config", default=str(ROOT / "keypool.json"))
    parser.add_argument("--pool", default="http://127.0.0.1:8780", help="本机 Key 池的地址")
    parser.add_argument("--max-triggers", type=int, default=5, help="最多找几处（一个请求里可能不止一处）")
    args = parser.parse_args()
    capture = json.loads(Path(args.capture).read_text(encoding="utf-8"))
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    master = config.get("masterKey")
    node = next((n for n in config["nodes"] if n.get("id") == capture["node"]), None)
    if node is None:
        raise SystemExit("存下来的请求属于一个已经删掉的节点")
    body = json.loads(capture["body"])
    probe = Prober(capture, node, args.pool, master[0] if isinstance(master, list) else master,
                   str(Path(args.capture).with_suffix(".probe.json")))
    if probe.cache:
        print("接着上次的进度：已有 %d 个片段的结果。" % len(probe.cache))
    parts = pieces(body)
    print("请求：模型 %s，%d 段文字，共 %d 字" % (body.get("model"), len(parts), sum(len(t) for _, t in parts)))
    if probe.refused("你好"):
        raise SystemExit("连“你好”都被拒：不是内容的问题，先检查这个 Key")
    whole = hashlib.sha1(capture["body"].encode("utf-8")).hexdigest()
    if whole not in probe.cache:
        probe.remember(whole, probe.refused_body(body))
    if not probe.cache[whole]:
        raise SystemExit("原样重发没有被拒：过滤规则可能变了，或者拒绝不是稳定复现的")
    print("原样重发仍然被拒，开始定位。", flush=True)
    found = []
    for label, text in parts:
        if len(found) >= args.max_triggers:
            break
        while len(found) < args.max_triggers and probe.refused(text):
            trigger = probe.shrink(text)
            at = text.find(trigger)
            found.append(trigger)
            print("\n【触发 %d】在 %s，第 %d 个字附近：" % (len(found), label, at))
            print("  触发的内容：%r" % trigger)
            print("  前后文：…%s【%s】%s…" % (text[max(0, at - 40):at].replace("\n", "⏎"), trigger,
                                           text[at + len(trigger):at + len(trigger) + 40].replace("\n", "⏎")), flush=True)
            text = text.replace(trigger, " ")   # 去掉这一处，看同一段里还有没有别的
    if not found:
        print("\n每一段单独发都没有被拒：触发条件可能要几段内容合在一起才成立，或者和文字以外的字段有关。")
    print("\n一共发了 %d 个探测请求。" % probe.sent)


if __name__ == "__main__":
    main()
