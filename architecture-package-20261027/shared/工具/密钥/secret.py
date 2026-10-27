"""小八密钥箱：用户填 Key，Agent 只能调用，看不到值。

密钥存在私人助理之外（默认 ~/.config/xiaoba/secrets/，目录 700、文件 600）。
本脚本没有任何“打印密钥”的命令。用法见同目录 README.md。

兼容 Python 3.9+；生产用 配置/ 下的 3.14.7（由 xb-secret 调用）。
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/Users/suzhizhou/Documents/私人助理")
STORE = Path(os.environ.get("XIAOBA_SECRETS_DIR", "~/.config/xiaoba/secrets")).expanduser()
PY = ROOT / "配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def ensure_store():
    STORE.mkdir(parents=True, exist_ok=True)
    os.chmod(STORE, 0o700)


def paths(name):
    if not NAME_RE.match(name):
        die("名字只能用小写字母、数字、- 和 _：%s" % name)
    return STORE / (name + ".env"), STORE / (name + ".json")


def load_meta(name):
    _, meta = paths(name)
    if not meta.exists():
        return {"name": name, "vars": [], "note": "", "allow": {}}
    return json.loads(meta.read_text(encoding="utf-8"))


def save_private(path, text):
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def save_meta(meta):
    _, p = paths(meta["name"])
    save_private(p, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")


def read_values(name):
    env, _ = paths(name)
    if not env.exists():
        die("还没有密钥 %s。请用户运行：xb-secret set %s <变量名>" % (name, name))
    out = {}
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_values(name, values):
    env, _ = paths(name)
    body = "".join("%s=%s\n" % (k, v) for k, v in values.items())
    save_private(env, body)


def ask_user(prompt, hidden=True):
    """弹 macOS 对话框让用户本人输入；值只进本进程内存，不回显。"""
    script = [
        "on run argv",
        'set r to display dialog (item 1 of argv) default answer "" '
        + ("with hidden answer " if hidden else "")
        + 'with title "小八密钥箱" buttons {"取消", "保存"} default button "保存" cancel button "取消"',
        "return text returned of r",
        "end run",
    ]
    cmd = ["osascript"]
    for s in script:
        cmd += ["-e", s]
    cmd += ["--", prompt]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        if not sys.stdin.isatty():
            die("没有可用的输入窗口；请用户在自己的终端里运行这条命令。")
        import getpass
        return getpass.getpass(prompt + "：")
    if r.returncode != 0:
        die("用户取消了输入，什么都没改。")
    return r.stdout.rstrip("\n")


def confirm_user(prompt):
    script = [
        "on run argv",
        'display dialog (item 1 of argv) with title "小八密钥箱" buttons {"拒绝", "允许"} '
        'default button "拒绝" cancel button "拒绝" with icon caution',
        "end run",
    ]
    cmd = ["osascript"]
    for s in script:
        cmd += ["-e", s]
    cmd += ["--", prompt]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        if not sys.stdin.isatty():
            return False
        return input(prompt + "\n输入 yes 允许：").strip() == "yes"
    return r.returncode == 0


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def caller_path(arg):
    p = Path(arg).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    p = p.resolve()
    try:
        p.relative_to(ROOT.resolve())
    except ValueError:
        die("只能授权私人助理目录里的脚本：%s" % p)
    if not p.is_file():
        die("找不到脚本：%s" % p)
    return p


# ---------- 命令 ----------

def cmd_set(args):
    if len(args) < 2:
        die("用法：xb-secret set <名字> <变量名> [用途说明]")
    name, var = args[0], args[1]
    note = " ".join(args[2:])
    if not VAR_RE.match(var):
        die("变量名只能用大写字母、数字和 _：%s" % var)
    ensure_store()
    paths(name)
    value = ask_user("请输入 %s 的 %s%s\n（只保存在本机 %s，Agent 看不到）"
                     % (name, var, ("，用途：" + note) if note else "", STORE))
    if not value:
        die("没有输入内容，什么都没改。")
    env, _ = paths(name)
    values = read_values(name) if env.exists() else {}
    values[var] = value
    write_values(name, values)
    meta = load_meta(name)
    meta["vars"] = sorted(values.keys())
    if note:
        meta["note"] = note
    meta["updated"] = time.strftime("%Y-%m-%d %H:%M")
    save_meta(meta)
    print("已保存 %s 的 %s（值未显示）。" % (name, var))


def cmd_import(args):
    """把现有的 KEY=VALUE 文件收进密钥箱；原文件不动，由用户自己移到 _待删/。"""
    if len(args) < 2:
        die("用法：xb-secret import <名字> <现有 .env 文件> [用途说明]")
    name, src = args[0], Path(args[1]).expanduser()
    note = " ".join(args[2:])
    if not src.is_file():
        die("找不到文件：%s" % src)
    if not confirm_user("把 %s 收进密钥箱，命名为 %s？\n收好后原文件请你自己移到 _待删/。" % (src, name)):
        die("用户拒绝，什么都没改。")
    ensure_store()
    values = {}
    for line in src.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:]
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if VAR_RE.match(k):
            values[k] = v.strip().strip('"').strip("'")
    if not values:
        die("文件里没找到 KEY=VALUE 形式的内容。")
    write_values(name, values)
    meta = load_meta(name)
    meta["vars"] = sorted(values.keys())
    if note:
        meta["note"] = note
    meta["updated"] = time.strftime("%Y-%m-%d %H:%M")
    save_meta(meta)
    print("已收进 %s：%s（值未显示）。原文件 %s 请移到 _待删/。" % (name, ", ".join(meta["vars"]), src))


def cmd_allow(args):
    if len(args) != 2:
        die("用法：xb-secret allow <名字> <脚本路径>")
    name, script = args[0], caller_path(args[1])
    read_values(name)
    rel = str(script.relative_to(ROOT.resolve()))
    if not confirm_user("允许脚本\n%s\n使用密钥 %s 吗？\n脚本以后被改动，需要你重新允许。" % (rel, name)):
        die("用户拒绝，什么都没改。")
    meta = load_meta(name)
    meta.setdefault("allow", {})[rel] = sha256(script)
    save_meta(meta)
    print("已允许 %s 使用 %s。" % (rel, name))


def cmd_revoke(args):
    if len(args) != 2:
        die("用法：xb-secret revoke <名字> <脚本路径>")
    name, rel = args[0], args[1]
    meta = load_meta(name)
    allow = meta.get("allow", {})
    hit = [k for k in allow if k == rel or str(ROOT / k) == rel]
    for k in hit:
        del allow[k]
    save_meta(meta)
    print("已撤销。" if hit else "本来就没有这条授权。")


def cmd_list(_args):
    if not STORE.exists():
        print("密钥箱是空的。")
        return
    metas = sorted(STORE.glob("*.json"))
    if not metas:
        print("密钥箱是空的。")
        return
    for p in metas:
        m = json.loads(p.read_text(encoding="utf-8"))
        print("%s  变量：%s  用途：%s  更新：%s" % (
            m["name"], ", ".join(m.get("vars", [])) or "-", m.get("note") or "-", m.get("updated", "-")))
        for rel in m.get("allow", {}):
            print("    允许：%s" % rel)


def cmd_has(args):
    if len(args) != 1:
        die("用法：xb-secret has <名字>")
    env, _ = paths(args[0])
    if env.exists():
        print("有")
        return
    print("没有。请用户运行：xb-secret set %s <变量名>" % args[0])
    sys.exit(1)


def cmd_run(args):
    if "--" not in args:
        die("用法：xb-secret run <名字>[,<名字>...] -- <已授权脚本> [参数...]")
    i = args.index("--")
    names, rest = args[:i], args[i + 1:]
    if len(names) != 1 or not rest:
        die("用法：xb-secret run <名字>[,<名字>...] -- <已授权脚本> [参数...]")
    script = caller_path(rest[0])
    rel = str(script.relative_to(ROOT.resolve()))
    digest = sha256(script)
    env = os.environ.copy()
    for name in names[0].split(","):
        allow = load_meta(name).get("allow", {})
        if rel not in allow:
            die("脚本 %s 没有被允许使用密钥 %s。请用户运行：xb-secret allow %s %s" % (rel, name, name, rel))
        if allow[rel] != digest:
            die("脚本 %s 在授权后被改过，已拒绝。请用户检查后重新运行：xb-secret allow %s %s" % (rel, name, rel))
        env.update(read_values(name))
    argv = [str(PY), "-B", str(script)] if script.suffix == ".py" else [str(script)]
    argv += rest[1:]
    os.execve(argv[0], argv, env)


COMMANDS = {
    "set": cmd_set, "import": cmd_import, "allow": cmd_allow, "revoke": cmd_revoke,
    "list": cmd_list, "has": cmd_has, "run": cmd_run,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        die("用法：xb-secret {set|import|allow|revoke|list|has|run} ...（说明见 工具/密钥/README.md）", 2)
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
