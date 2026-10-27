#!/bin/sh
# 小八钩子体检（只读）。检查四家钩子是否还指向 guard.sh、记录并比较各 Agent 版本、守卫自测、全局 AGENTS.md 软链。
# 只取配置里的键、command 与 matcher 字段，不打印整份文件或任何凭据值。唯一会写的文件：工具/治理/run/agent-versions.json（只存版本号）。
# 退出码：0 全部正常；1 有异常。
PY="/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14"
[ -x "$PY" ] || { echo "体检失败：统一解释器缺失 $PY"; exit 1; }
exec "$PY" -I -B - "$@" <<'EOF'
import json, os, re, subprocess, sys

ROOT = "/Users/suzhizhou/Documents/私人助理"
GUARD = ROOT + "/工具/治理/guard.sh"
STATE = ROOT + "/工具/治理/run/agent-versions.json"
H = os.path.expanduser("~")
problems, notes = [], []


def hook_matchers(path, kind):
    """返回调用 guard.sh 的那几组 PreToolUse 钩子的 matcher（没写 matcher 记为 "*"）；读不到返回 None。"""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    blocks = ([v for v in d.values() if isinstance(v, dict) and v.get("enabled", True) is not False]
              if kind == "agy" else [d.get("hooks") or {}])
    out = []
    for b in blocks:
        for group in b.get("PreToolUse") or []:
            if any(isinstance(h, dict) and h.get("command") == GUARD for h in group.get("hooks") or []):
                out.append(group.get("matcher") or "*")
    return out


def covers(matcher, tool):
    if matcher in ("*", ".*", ""):
        return True
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return False


def hook_commands(path, kind):
    """返回该配置里 PreToolUse 钩子的 command 列表；文件缺失或解析失败返回 None。"""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if kind == "agy":
        blocks = [v for v in d.values() if isinstance(v, dict) and v.get("enabled", True) is not False]
    else:
        blocks = [d.get("hooks") or {}]
    cmds = []
    for b in blocks:
        for group in b.get("PreToolUse") or []:
            for h in group.get("hooks") or []:
                if isinstance(h, dict) and h.get("command"):
                    cmds.append(h["command"])
    return cmds


def version(argv):
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=20).stdout.strip().splitlines()
        return out[0].strip() if out else None
    except (OSError, subprocess.TimeoutExpired):
        return None


# 这几家的钩子 matcher 至少要覆盖的工具（其它家的工具名未实测，只列出 matcher 不判定）。
CORE = {"Claude Code": ("Bash", "Read", "Write", "Edit"), "Qoder": ("Bash", "Read", "Write", "Edit"),
        # Antigravity 2.25.0 实测的工具名（2026-10-22）；升级后若改名，这里会报没覆盖。
        "Antigravity": ("run_command", "read_file", "edit_file", "write_file")}

print("小八钩子体检")
print("=" * 30)

# 1. 钩子
print("\n【钩子】")
for name, path, kind in (("Claude Code", H + "/.claude/settings.json", "claude"),
                         ("Qoder", H + "/.qoder/settings.json", "claude"),
                         ("Codex", H + "/.codex/hooks.json", "claude"),
                         ("Antigravity", H + "/.gemini/config/hooks.json", "agy")):
    cmds = hook_commands(path, kind)
    if cmds is None:
        problems.append(f"{name} 配置读不到：{path}")
        print(f"  ✗ {name}：配置缺失或不是合法 JSON")
    elif GUARD in cmds:
        print(f"  ✓ {name}：PreToolUse 指向 guard.sh")
        ms = hook_matchers(path, kind) or []
        if name in CORE:
            missing = [t for t in CORE[name] if not any(covers(m, t) for m in ms)]
            if missing:
                problems.append(f"{name} 的钩子 matcher 没覆盖 {'、'.join(missing)}")
                print(f"    ✗ matcher 没覆盖 {'、'.join(missing)}")
        if ms and not any(covers(m, "mcp__any__tool") for m in ms):
            notes.append(f"{name} 的钩子 matcher 是 {' / '.join(ms)}，MCP 等其它工具不经守卫")
            print(f"    · matcher：{' / '.join(ms)}（不含 MCP 等其它工具）")
    else:
        problems.append(f"{name} 的 PreToolUse 里没有 guard.sh")
        print(f"  ✗ {name}：PreToolUse 里没有 guard.sh")
notes.append("Codex 钩子是否已被信任，本脚本查不到；Codex 升级后请到钩子浏览器确认")

# 2. 版本
print("\n【版本】")
current = {
    "claude": version([H + "/.local/bin/claude", "--version"]),
    "codex": version([H + "/.local/bin/codex", "--version"]),
    "qodercli": version([H + "/.local/bin/qodercli", "--version"]),
    "agy": version([H + "/Documents/AGY/agy", "--version"]),
    "dsh": version([H + "/.local/bin/dsh", "--version"]),
}
try:
    with open(STATE, encoding="utf-8") as f:
        last = json.load(f).get("versions", {})
except (OSError, ValueError):
    last = {}
upgraded = []
for k, v in current.items():
    old = last.get(k)
    mark = ""
    if v is None:
        mark = "（取不到版本）"
        notes.append(f"{k} 取不到版本号")
    elif old and old != v:
        mark = f"（上次 {old}，已变化）"
        upgraded.append(k)
    elif not old:
        mark = "（首次记录）"
    print(f"  {k}: {v or '-'} {mark}")
if upgraded:
    problems.append("有 Agent 升级：" + "、".join(upgraded) + "。请在这些 Agent 里跑 V4 卡第 1、2 步复查")
os.makedirs(os.path.dirname(STATE), exist_ok=True)
with open(STATE + ".tmp", "w", encoding="utf-8") as f:
    json.dump({"versions": {**last, **{k: v for k, v in current.items() if v}}}, f, ensure_ascii=False, indent=2)
os.replace(STATE + ".tmp", STATE)

# 3. 守卫自测
print("\n【守卫自测】")
cases = (
    ("私人助理内 rm", 2, ["hook", {"tool_name": "Bash", "tool_input": {"command": "rm a"}, "cwd": ROOT}]),
    ("私人助理外 rm", 0, ["hook", {"tool_name": "Bash", "tool_input": {"command": "rm a"}, "cwd": "/tmp/xiaoba-check"}]),
    ("读 .env", 2, ["read", "/tmp/xiaoba-check/.env"]),
    ("写 AGENTS.md", 2, ["write", ROOT + "/AGENTS.md"]),
    ("写 第二大脑 40", 3, ["write", ROOT + "/第二大脑/40_工作流/体检探针.md"]),
)
for label, want, (mode, arg) in cases:
    try:
        if mode == "hook":
            r = subprocess.run([GUARD], input=json.dumps(arg), capture_output=True, text=True, timeout=15)
        else:
            r = subprocess.run([GUARD, mode, arg], capture_output=True, text=True, timeout=15)
        got = r.returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        got = f"运行失败 {type(e).__name__}"
    ok = got == want
    print(f"  {'✓' if ok else '✗'} {label}：期望 {want}，实际 {got}")
    if not ok:
        problems.append(f"守卫自测失败：{label}（期望 {want}，实际 {got}）")

# 4. 软链
print("\n【全局 AGENTS.md 软链】")
for p in (H + "/.codex/AGENTS.md", H + "/.agents/AGENTS.md"):
    target = os.readlink(p) if os.path.islink(p) else None
    if target == ROOT + "/AGENTS.md" and os.path.exists(p):
        print(f"  ✓ {p}")
    else:
        problems.append(f"{p} 不再是指向 私人助理/AGENTS.md 的软链")
        print(f"  ✗ {p}：{'指向 ' + target if target else '不是软链或已失效'}")

# 结论
print("\n【结论】")
if problems:
    print("  有异常：")
    for p in problems:
        print("  - " + p)
else:
    print("  全部正常。")
for n in notes:
    print("  注：" + n)
sys.exit(1 if problems else 0)
EOF
