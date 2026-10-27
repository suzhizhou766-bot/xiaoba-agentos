"""小八共用守卫。只读 policy.json，不联网、不写日志、不常驻。
用法：
  guard.py                     从 stdin 读钩子 JSON（Claude Code / Codex / Qoder 用 tool_name，Antigravity 用 toolCall）
  guard.py cmd   "<命令>"      其他 Agent 的适配器调用；当前目录取进程目录
  guard.py read  <路径>
  guard.py write <路径>
命令行模式退出码：0 放行；2 禁止；3 需确认。钩子模式见 reply()。兼容 Python 3.9。
作用范围：路径规则（deny_read / deny_write / ask_write）全局生效；命令规则只在 command_scope 内生效，
除非规则标了 "scope": "global"。是否在范围内看会话当前目录，或命令里是否出现范围内的路径。
路径比对同时看符号链接解析后的真实位置；参数里的通配符按实际文件展开；命令里 cd/pushd 过的目录也作为相对路径的起点（2026-10-22 修，V4 复测发现的三种绕过）。
"""
import fnmatch, glob, json, os, re, sys

POLICY = os.environ.get("XIAOBA_POLICY") or "/Users/suzhizhou/Documents/私人助理/系统/治理/policy.json"


def load():
    with open(POLICY, encoding="utf-8") as f:
        p = json.load(f)
    root = p["root"]
    expand = lambda g: g.replace("ROOT", root, 1) if g.startswith("ROOT") else os.path.expanduser(g)
    for k in ("deny_read", "deny_write", "ask_write", "command_scope"):
        p[k] = [expand(g) for g in p.get(k, [])]
    return p


def absolute(path, cwd):
    path = os.path.expanduser(path)
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd or os.getcwd(), path))


def hit(path, globs):
    """路径本身或它解析符号链接后的真实位置命中任一规则都算（防“无辜名字的链接指向凭据”）。"""
    real = os.path.realpath(path)
    for cand in (path,) if real == path else (path, real):
        base = os.path.basename(cand)
        for g in globs:
            if g.startswith("**/"):
                if fnmatch.fnmatch(base, g[3:]):
                    return g
            elif fnmatch.fnmatch(cand, g) or (g.endswith("/**") and cand == g[:-3]):
                return g
    return None


GLOB_CHARS = re.compile(r"[*?\[]")


def expand(path):
    """带通配符的路径展开成实际存在的文件（只看文件名，不读内容），防 cat ~/.x/setting* 绕过。"""
    if not GLOB_CHARS.search(path):
        return [path]
    try:
        return [path] + glob.glob(path, recursive=False)[:200]
    except (OSError, re.error):
        return [path]


CD = re.compile(r"(?:^|[;&|(]|&&|\|\|)\s*(?:cd|pushd)\s+([^\s;&|)]+)")


def bases(s, cwd):
    """命令里 cd/pushd 过的目录也当作相对路径的起点，防 cd ~/.x && cat settings.json 绕过。"""
    out = [cwd]
    for d in CD.findall(s):
        d = d.strip("'\"")
        if d and d != "-":
            out.append(absolute(d, cwd))
    return out


def in_scope(cmd, cwd, p):
    for d in p["command_scope"]:
        if cwd and (cwd == d or cwd.startswith(d + os.sep)):
            return True
        if d in cmd:
            return True
    return False


def check(kind, target, p, cwd=None):
    if kind == "cmd":
        scoped = in_scope(target, cwd, p)
        for rule in p["deny_commands"]:
            if (scoped or rule.get("scope") == "global") and re.search(rule["pattern"], target):
                return 2, rule["reason"]
        for rule in p["ask_commands"]:
            if (scoped or rule.get("scope") == "global") and re.search(rule["pattern"], target):
                return 3, rule["reason"]
        return 0, ""
    target = absolute(target, cwd)
    if any(hit(t, p["deny_read"]) for t in expand(target)):
        return 2, "凭据路径，禁止读写"
    if kind in ("write", "delete"):
        if hit(target, p["deny_write"]):
            return 2, "受保护目录，只能由用户写入"
        if kind == "delete" and in_scope(target, None, p):
            return 2, "不直接删除：请移到 _待删/，由用户清空"
        if hit(target, p["ask_write"]):
            return 3, "改动规则、技能或第二大脑需用户确认"
    return 0, ""


# 各家工具名到 cmd / read / write 的映射。Claude Code、Codex、Qoder 用 tool_name + tool_input；
# Antigravity 用 toolCall.name + toolCall.args。桌面端 2.25.0 起编辑工具叫 edit_file（参数 Path），2026-10-22 实测。
CMD_TOOLS = {"Bash", "shell", "exec_command", "run_command", "run_terminal_command"}
READ_TOOLS = {"Read", "Grep", "Glob", "view_file", "read_file", "list_dir"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "write_to_file", "replace_file_content", "write_file", "edit_file"}
CMD_KEYS = ("command", "cmd", "CommandLine")
PATH_KEYS = ("file_path", "notebook_path", "path", "Path", "AbsolutePath", "TargetFile")
# 不认识、但名字像执行命令或改文件的工具：在范围内一律需确认，防止工具改名后静默放行。
RISKY_NAME = re.compile(r"exec|run|shell|command|terminal|bash|write|edit|patch|create|delete|remove|move|rename", re.I)
PATCH_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)


def parse_hook(data):
    """返回 [(kind, target), ...]、会话当前目录、回复格式。"""
    if "toolCall" in data:
        call = data.get("toolCall") or {}
        tool, inp, fmt = call.get("name", ""), call.get("args") or {}, "agy"
        cwd = inp.get("Cwd") or next(iter(data.get("workspacePaths") or []), None)
    else:
        tool, inp, fmt = data.get("tool_name", ""), data.get("tool_input") or {}, "claude"
        cwd = data.get("cwd")
    if isinstance(inp, str):
        inp = {"command": inp}
    # 工具自带的工作目录（Codex exec_command 的 workdir 等）优先于会话目录。
    cwd = next((inp[k] for k in ("workdir", "cwd", "Cwd") if isinstance(inp.get(k), str) and inp.get(k)), None) or cwd
    def pick(keys):
        v = next((inp[k] for k in keys if inp.get(k)), "")
        return " ".join(map(str, v)) if isinstance(v, list) else str(v)
    if tool == "apply_patch":
        # Codex：补丁正文在 tool_input.command，文件路径在补丁头里。
        acts = []
        for verb, path, moved in PATCH_FILE.findall(pick(CMD_KEYS)):
            acts.append(("delete" if verb == "Delete" else "write", (path or moved).strip()))
        return acts, cwd, fmt
    if tool in CMD_TOOLS:
        return [("cmd", pick(CMD_KEYS))], cwd, fmt
    if tool in READ_TOOLS:
        return [("read", pick(PATH_KEYS))], cwd, fmt
    if tool in WRITE_TOOLS:
        return [("write", pick(PATH_KEYS))], cwd, fmt
    if tool and RISKY_NAME.search(tool):
        # 不认识的工具：要确认；它参数里的代码或命令也按命令规则查（如 node_repl 里的 rm）。
        return [("unknown", tool), ("cmd", " ".join(strings(inp)))], cwd, fmt
    return [], cwd, fmt


# 文件正文类参数不扫（文档里提到 .env 不算读凭据）；其余参数里的路径样字串都查凭据规则。
CONTENT_KEYS = {"content", "new_string", "old_string", "edits", "new_source", "text", "description",
                "CodeContent", "ReplacementContent", "ReplacementChunks", "Instruction"}
TOKEN_SPLIT = re.compile(r"[\s'\"`(),;=<>|&{}\[\]]+")


def strings(v, key=None):
    if key in CONTENT_KEYS:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [x for k, val in v.items() for x in strings(val, k)]
    if isinstance(v, list):
        return [x for val in v for x in strings(val)]
    return []


def credential_tokens(inp, cwd, p):
    """任何工具的参数里出现凭据路径（Node 的 readFile、python -c open(...) 等）都拦，不依赖工具名。"""
    for s in strings(inp if isinstance(inp, dict) else {"command": inp}):
        starts = bases(s, cwd)
        for tok in TOKEN_SPLIT.split(s):
            if not tok or not ("/" in tok or "." in tok or GLOB_CHARS.search(tok)):
                continue
            for b in starts:
                for cand in expand(absolute(tok, b)):
                    if hit(cand, p["deny_read"]):
                        return cand if cand != absolute(tok, b) else tok
    return None


def reply(code, reason, fmt):
    """钩子模式：禁止 = 退出码 2 + stderr（Claude Code、Codex、Qoder 通用）；需确认 = 输出 ask 的 JSON。
    Antigravity 一律输出 {"decision": ...}，需确认用 force_ask（ask 会被它的“始终允许”缓存放行）；放行时不输出（它的 "allow" 会自动批准、越过自身权限询问）。"""
    if not code:
        return 0
    if fmt == "agy":
        print(json.dumps({"decision": "deny" if code == 2 else "force_ask", "reason": "小八守卫：" + reason}, ensure_ascii=False))
        return 0
    if code == 2:
        sys.stderr.write("小八守卫：" + reason + "\n")
        return 2
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                             "permissionDecisionReason": "小八守卫：" + reason}}, ensure_ascii=False))
    return 0


def main(argv):
    p = load()
    if len(argv) >= 3:
        code, reason = check(argv[1], argv[2], p, os.getcwd())
        if code:
            sys.stderr.write("小八守卫：" + reason + "\n")
        return code
    data = json.load(sys.stdin)
    acts, cwd, fmt = parse_hook(data)
    inp = (data.get("toolCall") or {}).get("args") if "toolCall" in data else data.get("tool_input")
    tok = credential_tokens(inp or {}, cwd, p)
    if tok:
        return reply(2, "参数里出现凭据路径（" + os.path.basename(tok) + "），禁止读写", fmt)
    worst = (0, "")
    for kind, target in acts:
        if not target:
            continue
        if kind == "unknown":
            res = (3, "未识别的工具 " + target + "，需用户确认") if cwd and in_scope("", cwd, p) else (0, "")
        else:
            res = check(kind, target, p, cwd)
        if res[0] == 2 or (res[0] == 3 and worst[0] == 0):
            worst = res
        if worst[0] == 2:
            break
    return reply(worst[0], worst[1], fmt)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
