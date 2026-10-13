#!/usr/bin/env python3
"""Key 池安全检测：请求打码 + 回复工具调用审查。

威胁模型：中转站是你和模型之间的中间人。它改不了你的电脑，但能
  (1) 看到你发出去的内容（代码、密钥）；
  (2) 篡改模型的回复——最危险的是往“工具调用”里塞危险操作，
      被 Claude Code / Codex 这类会执行命令的客户端自动执行。

本模块做两件事，纯函数，便于单测：
  - scan_request(body)：发出前扫描请求里的密钥，命中就告警并打码（可关）；
  - 回复侧：把回复里的工具调用参数收齐后按模式匹配，命中高危就拦截，
    普通文字照常实时输出（见 StreamInspector / inspect_json）。

规则只能识别典型手法，识别不了藏在正常代码里的隐蔽后门，所以客户端仍要
保持“执行前确认”、少放重要密钥、每个中转站只充少量余额。
"""
import json
import re

# ---- 请求侧：要打码的密钥（保守，尽量不误伤正常代码）----
SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----.+?-----END[^\n]+-----", re.S)),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-|ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{20,}\b")),
    ("dotenv-secret", re.compile(r"(?im)^\s*[A-Z0-9_]*(?:SECRET|PASSWORD|PASSWD|TOKEN|API[_-]?KEY|PRIVATE[_-]?KEY)[A-Z0-9_]*\s*[=:]\s*['\"]?([^\s'\"]{8,})")),
]


def _mask(value):
    value = str(value)
    if len(value) <= 10:
        return "•" * len(value)
    return value[:4] + "…[已打码]…" + value[-2:]


def scan_text_secrets(text, extra=None):
    """返回命中列表 [{"kind","sample"}]；sample 已打码，绝不回传明文。"""
    found = []
    for kind, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            found.append({"kind": kind, "sample": _mask(match.group(0)[:48])})
    for value in (extra or []):
        if value and len(value) >= 8 and value in text:
            found.append({"kind": "pool-key", "sample": _mask(value)})
    return found


def mask_text_secrets(text, extra=None):
    for _, pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m: _mask(m.group(0)), text)
    for value in (extra or []):
        if value and len(value) >= 8:
            text = text.replace(value, _mask(value))
    return text


def _walk_strings(node, fn):
    """就地改写 JSON 里的所有字符串（messages 内容、system 等）。"""
    if isinstance(node, dict):
        return {k: _walk_strings(v, fn) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk_strings(v, fn) for v in node]
    if isinstance(node, str):
        return fn(node)
    return node


def scan_request(body, extra_keys=None, do_mask=True):
    """扫描请求体里的密钥。返回 (新 body, findings)。do_mask=False 只告警不改。"""
    if not body or body[:1] not in (b"{", b"["):
        return body, []
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return body, []
    found = []

    def visit(text):
        found.extend(scan_text_secrets(text, extra_keys))
        return mask_text_secrets(text, extra_keys) if do_mask else text

    new = _walk_strings(parsed, visit)
    if not found:
        return body, []
    out = json.dumps(new, ensure_ascii=False).encode("utf-8") if do_mask else body
    # 去重（同一种类只报一次）
    seen, uniq = set(), []
    for f in found:
        if f["kind"] not in seen:
            seen.add(f["kind"])
            uniq.append(f)
    return out, uniq


# ---- 回复侧：工具调用里的高危操作（直接拦截）----
# 规则按模式匹配命令文本，命中即视为危险。写得克制，只覆盖典型手法。
BLOCK_RULES = [
    ("pipe-to-shell", "下载后直接执行", re.compile(
        r"(?is)(?:curl|wget|fetch)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z|da|)sh\b"
        r"|(?:base64\s+-?-?d(?:ecode)?|xxd\s+-r|openssl\s+enc[^\n|]*-d)[^\n|]*\|\s*(?:ba|z|)sh\b"
        r"|python3?\s+-c\s*['\"].{0,200}(?:urllib|requests|socket).{0,200}(?:exec|system|popen)")),
    ("reverse-shell", "反弹 shell", re.compile(
        r"(?is)(?:bash|sh)\s+-i\s*>?&?\s*/dev/tcp/"
        r"|/dev/(?:tcp|udp)/\d{1,3}(?:\.\d{1,3}){3}/\d+"
        r"|nc\b[^\n]*\s-[a-z]*e[a-z]*\s"
        r"|socat\b[^\n]*exec")),
    ("credential-exfil", "读取凭据后外发", re.compile(
        r"(?is)(?:cat|cp|tar|zip|scp|curl|rsync|xxd|openssl|base64)\b[^\n]{0,200}"
        r"(?:\.ssh/|id_rsa|\.aws/credentials|\.config/gcloud|\.netrc|\.docker/config|"
        r"Library/Keychains|login\.keychain|Cookies\b|key(?:chain|ring))"
        r"[^\n]{0,200}(?:curl|wget|nc\b|scp|/dev/tcp|@|https?://|\|)")),
    ("persistence", "写入开机自启", re.compile(
        r"(?is)(?:>>?|tee)\s*[^\n]{0,80}(?:/LaunchAgents/|/LaunchDaemons/|crontab\b|/etc/cron|"
        r"\.zshrc|\.bashrc|\.bash_profile|\.profile|/etc/rc\.|systemd/system)"
        r"|crontab\s+-\b|launchctl\s+(?:load|bootstrap)\b")),
    ("disable-guard", "关闭系统防护", re.compile(
        r"(?is)spctl\s+--master-disable|csrutil\s+disable|xattr\s+-[a-z]*d[a-z]*\s+com\.apple\.quarantine"
        r"|chmod\s+-R?\s*777\s+/|defaults\s+write[^\n]*GatekeeperEnabled")),
    ("destructive", "大范围删除", re.compile(
        r"(?is)rm\s+-[a-z]*r[a-z]*f?[a-z]*\s+(?:-{2}no-preserve-root\s+)?(?:/|~|\$HOME|\*)\s*$"
        r"|rm\s+-[a-z]*r[a-z]*\s+/\s|find\s+(?:/|~)\s[^\n]*-delete|:\(\)\s*\{\s*:\|:&\s*\}")),
]
# 中危：只告警不拦
WARN_RULES = [
    ("sudo", "用了 sudo", re.compile(r"(?is)\bsudo\s+\S")),
    ("outbound", "访问了外部地址", re.compile(r"(?is)\b(?:curl|wget|nc|scp|ssh)\b[^\n]*(?:https?://|@)[^\n]*")),
    ("chmod-exec", "给文件加可执行权限后运行", re.compile(r"(?is)chmod\s+[0-7]*[1357][0-7]*\s+\S+[^\n]*&&\s*\./")),
]


def scan_commands(text):
    """对一段命令/参数文本按规则匹配。返回 (blocks, warns)，元素为 {"rule","label"}。"""
    if not text:
        return [], []
    blocks = [{"rule": rid, "label": label} for rid, label, pat in BLOCK_RULES if pat.search(text)]
    warns = [{"rule": rid, "label": label} for rid, label, pat in WARN_RULES if pat.search(text)]
    return blocks, warns


# 工具调用里哪些字段像“要执行的东西”：命令、脚本、路径、URL
_EXEC_KEYS = re.compile(r"(?i)(command|cmd|script|code|shell|bash|run|exec|args|content|input|path|file|url|query)")


def tool_text(tool_name, arguments):
    """从一个工具调用里取出要检查的文本：工具名 + 参数里像命令/脚本的字段（取不出就用整体）。"""
    parts = [str(tool_name or "")]
    try:
        obj = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (ValueError, TypeError):
        return (tool_name or "") + "\n" + (arguments if isinstance(arguments, str) else "")
    if isinstance(obj, dict):
        hit = [str(v) for k, v in obj.items() if _EXEC_KEYS.search(str(k)) and isinstance(v, (str, int, float))]
        parts.append("\n".join(hit) if hit else json.dumps(obj, ensure_ascii=False))
    else:
        parts.append(json.dumps(obj, ensure_ascii=False) if obj is not None else str(arguments))
    return "\n".join(parts)


def judge_tools(tool_calls, declared_tools=None):
    """对一组工具调用 [(name, arguments)] 判定。declared_tools：本次请求声明过的工具名集合。
    返回 {"block":bool, "findings":[{"tool","rule","label","severity"}]}。"""
    findings, block = [], False
    declared = set(declared_tools or [])
    for name, arguments in tool_calls:
        if declared and name and name not in declared:
            findings.append({"tool": name, "rule": "undeclared-tool", "label": "调用了未声明的工具（疑似被篡改）",
                             "severity": "block"})
            block = True
        blocks, warns = scan_commands(tool_text(name, arguments))
        for b in blocks:
            findings.append({"tool": name or "?", **b, "severity": "block"})
            block = True
        for w in warns:
            findings.append({"tool": name or "?", **w, "severity": "warn"})
    return {"block": block, "findings": findings}


def declared_tool_names(request_body):
    """从请求体里取出本次声明的工具名（OpenAI tools[].function.name / Anthropic tools[].name）。"""
    try:
        req = json.loads(request_body.decode("utf-8")) if isinstance(request_body, (bytes, bytearray)) else request_body
    except (ValueError, UnicodeDecodeError, AttributeError):
        return set()
    names = set()
    for tool in (req.get("tools") or []) if isinstance(req, dict) else []:
        if isinstance(tool, dict):
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            if fn.get("name"):
                names.add(fn["name"])
    for fn in ([req.get("functions")] if isinstance(req, dict) else []):
        for f in fn or []:
            if isinstance(f, dict) and f.get("name"):
                names.add(f["name"])
    return names


# ---- 从完整（非流式）回复里取工具调用 ----
def extract_tools_json(parsed):
    """OpenAI: choices[].message.tool_calls / function_call；Anthropic: content[].type==tool_use。"""
    tools = []
    if not isinstance(parsed, dict):
        return tools
    for choice in parsed.get("choices") or []:
        msg = (choice or {}).get("message") or {}
        for tc in msg.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            tools.append((fn.get("name"), fn.get("arguments")))
        if msg.get("function_call"):
            fc = msg["function_call"]
            tools.append((fc.get("name"), fc.get("arguments")))
    for block in parsed.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tools.append((block.get("name"), block.get("input")))
    return tools


def inspect_json(body, declared=None):
    """检查一段完整 JSON 回复。返回 (verdict, tools)。"""
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"block": False, "findings": []}, []
    tools = extract_tools_json(parsed)
    return judge_tools(tools, declared), tools


# 拦截后替换给客户端的内容：说明原因，且不含可被再次执行的命令
def blocked_message(findings, api="openai", model="", stream=False):
    labels = "；".join(sorted({f["label"] for f in findings if f["severity"] == "block"})) or "危险操作"
    note = "⚠️ Key 池安全检测拦截了这条回复里的危险操作（%s）。该中转站可能在篡改回复，已被隔离。请到监看中心查看。" % labels
    if api == "anthropic":
        payload = {"type": "message", "role": "assistant", "model": model,
                   "content": [{"type": "text", "text": note}], "stop_reason": "end_turn",
                   "usage": {"input_tokens": 0, "output_tokens": 0}}
    else:
        payload = {"object": "chat.completion", "model": model,
                   "choices": [{"index": 0, "finish_reason": "content_filter",
                                "message": {"role": "assistant", "content": note}}]}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


# ---- 流式回复审查 ----
# 策略：普通文字照常实时输出；一旦出现工具调用的片段，就从这里开始把后续事件扣下来，
# 收齐后判定——干净就原样放行，命中高危就丢弃并补一段说明。文字不是被执行的东西，
# 先到客户端没有风险；工具调用客户端总是等消息结束才执行，扣到判定完再放也不影响。
HOLD_CAP = 8 * 1024 * 1024


class StreamInspector:
    def __init__(self, api="openai", declared=None, model=""):
        self.api = api
        self.declared = set(declared or [])
        self.model = model
        self.buf = b""              # 还没凑成完整事件块的字节
        self.holding = False
        self.held = b""             # 扣下来的事件块
        self.tool_calls = {}        # index -> {"name","args"}
        self.anthropic_tool_idx = set()
        self.blocked = False
        self.findings = []
        self.failed_open = False    # 扣太多，放弃审查改为放行（并告警）

    # 输入上游原始字节，返回应当立刻转发给客户端的字节
    def feed(self, chunk):
        self.buf += chunk
        out = bytearray()
        while True:
            sep = self._next_sep()
            if sep is None:
                break
            pos, end, _ = sep
            full = self.buf[:end]            # 事件块（含分隔空行），原样保留以便逐字节转发
            block = self.buf[:pos]           # 不含分隔，用于解析
            self.buf = self.buf[end:]
            self._handle_block(block)        # 无论是否在扣，都要继续累积工具调用片段
            if self.failed_open or not self.holding:
                out += full
            else:
                self.held += full
                if len(self.held) > HOLD_CAP:  # 扣太多：放弃审查，原样放行并告警
                    self.failed_open = True
                    out += self.held
                    self.held = b""
                    self.holding = False
        return bytes(out)

    def _next_sep(self):
        a = self.buf.find(b"\n\n")
        b = self.buf.find(b"\r\n\r\n")
        if a < 0 and b < 0:
            return None
        if b < 0 or (0 <= a < b):
            return (a, a + 2, b"\n\n")
        return (b, b + 4, b"\r\n\r\n")

    def _handle_block(self, block):
        """解析一个事件块，累积工具调用片段；见到工具片段就进入扣留状态。"""
        if self.failed_open:
            return
        for line in block.split(b"\n"):
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]" or not payload:
                continue
            try:
                data = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if self._accumulate(data):
                self.holding = True

    def _accumulate(self, data):
        """把工具调用片段累积起来；返回这个事件是否属于工具调用。"""
        tool = False
        # OpenAI：choices[].delta.tool_calls / function_call
        for choice in data.get("choices") or []:
            delta = (choice or {}).get("delta") or {}
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = self.tool_calls.setdefault(("o", idx), {"name": "", "args": ""})
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
                tool = True
            if delta.get("function_call"):
                slot = self.tool_calls.setdefault(("o", "fc"), {"name": "", "args": ""})
                fc = delta["function_call"]
                if fc.get("name"):
                    slot["name"] = fc["name"]
                if fc.get("arguments"):
                    slot["args"] += fc["arguments"]
                tool = True
        # Anthropic：content_block_start(tool_use) + input_json_delta
        t = data.get("type")
        if t == "content_block_start":
            block = data.get("content_block") or {}
            if block.get("type") == "tool_use":
                idx = data.get("index", 0)
                self.anthropic_tool_idx.add(idx)
                self.tool_calls.setdefault(("a", idx), {"name": block.get("name", ""), "args": ""})
                tool = True
        elif t == "content_block_delta":
            idx = data.get("index", 0)
            delta = data.get("delta") or {}
            if idx in self.anthropic_tool_idx or delta.get("type") == "input_json_delta":
                slot = self.tool_calls.setdefault(("a", idx), {"name": "", "args": ""})
                slot["args"] += delta.get("partial_json", "") or ""
                tool = True
        elif t == "content_block_stop" and data.get("index") in self.anthropic_tool_idx:
            tool = True
        return tool

    def _judge(self):
        calls = [(v["name"], v["args"]) for v in self.tool_calls.values()]
        verdict = judge_tools(calls, self.declared)
        self.findings = verdict["findings"]
        self.blocked = verdict["block"] and not self.failed_open
        return verdict

    def finish(self):
        """流结束。返回最后要写给客户端的字节。"""
        tail = self.buf
        self.buf = b""
        if self.holding:
            self.held += tail
            tail = b""
        self._judge()
        if self.blocked:
            self.held = b""
            return self._replacement()
        # 干净（或 failed_open）：把扣下的原样放出去
        out = self.held + tail
        self.held = b""
        return out

    def _replacement(self):
        note = ("\n\n⚠️ Key 池安全检测拦截了这条回复里的危险操作（%s）。该中转站可能在篡改回复，已被隔离。"
                % "；".join(sorted({f["label"] for f in self.findings if f["severity"] == "block"})))
        if self.api == "anthropic":
            events = [
                ("content_block_start", {"type": "content_block_start", "index": 90,
                                         "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 90,
                                         "delta": {"type": "text_delta", "text": note}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 90}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                                   "usage": {"output_tokens": 0}}),
                ("message_stop", {"type": "message_stop"})]
            return b"".join(b"event: %s\ndata: %s\n\n" % (name.encode(), json.dumps(d, ensure_ascii=False).encode("utf-8"))
                            for name, d in events)
        chunk = {"object": "chat.completion.chunk", "model": self.model,
                 "choices": [{"index": 0, "delta": {"content": note}, "finish_reason": None}]}
        done = {"object": "chat.completion.chunk", "model": self.model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "content_filter"}]}
        return (b"data: " + json.dumps(chunk, ensure_ascii=False).encode("utf-8") + b"\n\n"
                + b"data: " + json.dumps(done, ensure_ascii=False).encode("utf-8") + b"\n\n"
                + b"data: [DONE]\n\n")
