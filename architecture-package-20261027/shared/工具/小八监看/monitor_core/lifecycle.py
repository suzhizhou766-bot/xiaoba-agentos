"""监看启停归属：原生 argv、启动指纹、claim 令牌、诊断性端口探测。

协议（相对 v1.6.16 关闭脚本）：
- 信号目标只来自本目录 lifecycle claim，并经「入口位置 + 启动指纹」核验；
  lsof / 端口探测只诊断，不发信号。
- 无法证明归属时停止/启动返回明确状态，不把“端口有人听”当成“这是我们的进程”。
- 受管（--supervise）正常停止与直接启动（serve）仍受支持：两者都写入 claim。
- v1.6.16 仅有 supervisor.pid、无 lifecycle.claim 时：不信任裸 PID/端口，不假报已关闭；
  需本机先安全停旧实例再由新代码认领（见 inspect_legacy_pid_file / stop_owned）。

发版后置同步须带上本模块：`monitor_core/lifecycle.py`（及调用它的启停入口）。
仅有旧目录拷贝、缺本 helper 时，不得宣称已具备归属停止能力。

残余竞态（如实标注，未消除）：
- 身份检查通过到实际 kill/signal 之间，目标 PID 仍可能退出并被复用；发送前会再验一次，
  仍有极短窗口。不做跨该窗口的内核级冻结。
- 持锁检查到写 claim 之间，其它未走本锁的外部进程仍可能抢端口；port_in_use 只诊断。
- stop_owned 在发信号后释放锁再等待：等待期间新实例可能认领同一 run_dir；清理时必须
  重新持锁并核对 token，未退出或身份未知时保留原 claim。
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

CLAIM_NAME = "lifecycle.claim"
LOCK_NAME = "lifecycle.lock"
PID_NAME = "supervisor.pid"

ROLE_SUPERVISE = "supervise"
ROLE_SERVE = "serve"

KIND_MATCH = "match"
KIND_OTHER = "other"
KIND_UNKNOWN = "unknown"
KIND_STALE = "stale"

# 关闭/启动在无 claim 但残留旧 pid 文件时的状态词
STATUS_LEGACY_UNCLAIMED = "legacy_unclaimed"

LEGACY_MIGRATION = (
    "检测到 v1.6.16 风格的 supervisor.pid 但没有 lifecycle.claim。"
    "不能信任裸 PID 或端口归属，也不能报已关闭。"
    "本机安全接续：① 切换前对旧监管做受控关闭——用原生 argv/启动指纹核验该 pid 确为本目录监管后，"
    "仅向已核验的监管 pid 发 TERM，并确认其子进程与目标端口监听已退出；"
    "禁止改用 v1.6.16 会按 lsof 监听表杀进程的旧关闭脚本。"
    "② 确认目标端口无监听后，删除本目录残留的 run/supervisor.pid（此时仍无 claim）。"
    "③ 用带 monitor_core/lifecycle.py 的候选/发版后入口启动并写入 claim，之后只用新关闭脚本。"
    "前置条件：发版后置同步必须包含 monitor_core/lifecycle.py 与更新后的启停入口。"
)


def entry_path(root: Path) -> Path:
    return Path(root) / "xiaoba_monitor.py"


def same_entry(token, root: Path) -> bool:
    expected = entry_path(root)
    candidate = Path(token)
    if candidate == expected:
        return True
    try:
        return candidate.resolve() == expected.resolve()
    except OSError:
        return False


def claim_path(run_dir: Path) -> Path:
    return Path(run_dir) / CLAIM_NAME


def lock_path(run_dir: Path) -> Path:
    return Path(run_dir) / LOCK_NAME


def supervisor_pid_path(run_dir: Path) -> Path:
    return Path(run_dir) / PID_NAME


class LifecycleLock:
    """进程间启停串行化。with 块内持有排他锁。"""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self._fh = None

    def __enter__(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._fh = open(lock_path(self.run_dir), "a+", encoding="utf-8")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


def read_claim(run_dir: Path):
    path = claim_path(run_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        return {"_error": "read_failed", "detail": str(exc)}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"_error": "invalid_json", "detail": str(exc)}
    if not isinstance(data, dict):
        return {"_error": "invalid_shape", "detail": "claim 不是对象"}
    return data


def write_claim_atomic(run_dir: Path, payload: dict) -> None:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = claim_path(run_dir)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def _read_argv_procfs(pid: int):
    cmdline = Path("/proc") / str(pid) / "cmdline"
    try:
        raw = cmdline.read_bytes()
    except FileNotFoundError:
        return [], ""
    except OSError as exc:
        if exc.errno in (errno.EPERM, errno.EACCES):
            return None, "无权读取进程命令行（EPERM/EACCES）"
        return None, "读不到进程命令行（%s）" % exc
    if not raw:
        return [], ""
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part], ""


def _read_argv_darwin(pid: int):
    """用 KERN_PROCARGS2 取 NUL 分隔参数，避免 ps -ww 空格边界歧义。"""
    import ctypes
    import ctypes.util

    libc_name = ctypes.util.find_library("c")
    if not libc_name:
        return None, "找不到 libc，无法读取进程参数"
    libc = ctypes.CDLL(libc_name, use_errno=True)
    CTL_KERN = 1
    KERN_PROCARGS2 = 49
    mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, int(pid))
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        err = ctypes.get_errno()
        if err in (errno.ESRCH, errno.ENOENT):
            return [], ""
        if err in (errno.EPERM, errno.EACCES):
            return None, "无权读取进程命令行（EPERM/EACCES）"
        return None, "sysctl 探测失败（errno %d）" % err
    if size.value <= 0:
        return [], ""
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        err = ctypes.get_errno()
        if err in (errno.ESRCH, errno.ENOENT):
            return [], ""
        if err in (errno.EPERM, errno.EACCES):
            return None, "无权读取进程命令行（EPERM/EACCES）"
        return None, "sysctl 读取失败（errno %d）" % err
    raw = buf.raw[: size.value]
    if len(raw) < 4:
        return [], ""
    argc = int.from_bytes(raw[:4], "little", signed=False)
    rest = raw[4:]
    nul = rest.find(b"\0")
    if nul < 0:
        return None, "进程参数格式异常"
    rest = rest[nul + 1 :]
    while rest.startswith(b"\0"):
        rest = rest[1:]
    parts = []
    while rest and len(parts) < argc:
        nul = rest.find(b"\0")
        if nul < 0:
            break
        parts.append(rest[:nul].decode("utf-8", "replace"))
        rest = rest[nul + 1 :]
    if len(parts) != argc:
        return None, "进程参数无法完整拆开（argc=%d got=%d）" % (argc, len(parts))
    return parts, ""


def read_process_argv(pid: int):
    """返回 (argv 或 None, 错误说明)。None=读失败（unknown）；[]=已确认无命令。

    不使用 ps -ww 再按空格拆：路径含空格时边界会歧义，可能把无关进程误判成监看。
    """
    if Path("/proc").is_dir():
        return _read_argv_procfs(pid)
    if sys.platform == "darwin":
        return _read_argv_darwin(pid)
    return None, "当前平台没有安全的原生 argv 读取方式"


def _fingerprint_darwin(pid: int):
    import ctypes
    import ctypes.util

    lib_name = ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
    try:
        libproc = ctypes.CDLL(lib_name, use_errno=True)
    except OSError as exc:
        return None, "无法加载 libproc（%s）" % exc
    PROC_PIDTBSDINFO = 3

    class proc_bsdinfo(ctypes.Structure):
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32),
            ("pbi_status", ctypes.c_uint32),
            ("pbi_xstatus", ctypes.c_uint32),
            ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32),
            ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32),
            ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32),
            ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32),
            ("rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * 16),
            ("pbi_name", ctypes.c_char * 32),
            ("pbi_nfiles", ctypes.c_uint32),
            ("pbi_pgid", ctypes.c_uint32),
            ("pbi_pjobc", ctypes.c_uint32),
            ("e_tdev", ctypes.c_uint32),
            ("e_tpgid", ctypes.c_uint32),
            ("pbi_nice", ctypes.c_int32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    info = proc_bsdinfo()
    got = libproc.proc_pidinfo(int(pid), PROC_PIDTBSDINFO, ctypes.c_uint64(0), ctypes.byref(info), ctypes.sizeof(info))
    if got <= 0:
        err = ctypes.get_errno()
        if err in (errno.ESRCH, errno.ENOENT):
            return None, "进程不存在"
        if err in (errno.EPERM, errno.EACCES):
            return None, "无权读取进程启动指纹（EPERM/EACCES）"
        return None, "proc_pidinfo 失败（errno %d）" % err
    if int(info.pbi_pid) != int(pid):
        return None, "进程指纹 pid 不一致"
    return {
        "kind": "darwin_bsdinfo",
        "pid": int(info.pbi_pid),
        "start_tvsec": int(info.pbi_start_tvsec),
        "start_tvusec": int(info.pbi_start_tvusec),
    }, ""


def _fingerprint_procfs(pid: int):
    # /proc/<pid>/stat 字段 22（starttime，自启动以来的时钟嘀嗒）标识该 PID 的启动实例。
    try:
        text = Path("/proc/%d/stat" % int(pid)).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None, "进程不存在"
    except OSError as exc:
        if exc.errno in (errno.EPERM, errno.EACCES):
            return None, "无权读取进程启动指纹（EPERM/EACCES）"
        return None, "读不到进程启动指纹（%s）" % exc
    # comm 可能含空格与括号：取最后一个 ')' 之后再按空格拆。
    close = text.rfind(")")
    if close < 0:
        return None, "进程 stat 格式异常"
    fields = text[close + 2 :].split()
    if len(fields) < 20:
        return None, "进程 stat 字段不足"
    try:
        starttime = int(fields[19])  # 0-based 相对 ')' 后字段，对应 stat 第 22 项
    except ValueError:
        return None, "进程 starttime 无法解析"
    return {"kind": "linux_stat", "pid": int(pid), "starttime": starttime}, ""


def read_process_fingerprint(pid: int):
    """读取进程启动实例指纹。失败返回 (None, detail)，调用方应视为 unknown。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None, "pid 无效"
    if Path("/proc").is_dir():
        return _fingerprint_procfs(pid)
    if sys.platform == "darwin":
        return _fingerprint_darwin(pid)
    return None, "当前平台没有安全的进程启动指纹"


def fingerprints_equal(left, right) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if left.get("kind") != right.get("kind"):
        return False
    if left.get("kind") == "darwin_bsdinfo":
        return (
            int(left.get("pid", -1)) == int(right.get("pid", -2))
            and int(left.get("start_tvsec", -1)) == int(right.get("start_tvsec", -2))
            and int(left.get("start_tvusec", -1)) == int(right.get("start_tvusec", -2))
        )
    if left.get("kind") == "linux_stat":
        return (
            int(left.get("pid", -1)) == int(right.get("pid", -2))
            and int(left.get("starttime", -1)) == int(right.get("starttime", -2))
        )
    return False


def _looks_like_python_interpreter(token: str) -> bool:
    name = Path(token).name.lower()
    return name == "python" or name.startswith("python3") or name.startswith("python2")


def _python_flag_arity(flag: str) -> int:
    """返回该 CPython 选项额外消耗的参数个数。未知长选项按 0 处理（保守：可能误判，见下）。"""
    if flag in ("-c", "-m"):
        return 1
    if flag in ("-Q", "-W", "-X"):
        return 1
    if flag.startswith("-") and len(flag) == 2 and flag[1] in "bBOouSEsvIRi":
        return 0
    if flag.startswith("--"):
        return 0
    if flag.startswith("-") and len(flag) > 2 and not flag.startswith("--"):
        # -Bbc 这类组合短选项：不额外吃参数
        return 0
    return 0


def find_executing_entry_index(argv, root: Path):
    """定位『正在执行的入口文件』在 argv 中的下标。

    路径或 flags 仅出现在 `python -c '...'` 的代码字符串里不算。
    `-c` / `-m` 运行方式没有本目录入口文件 → 返回 None。
    """
    if not argv:
        return None
    index = 0
    if _looks_like_python_interpreter(argv[0]):
        index = 1
    while index < len(argv):
        token = argv[index]
        if token == "-c" or token.startswith("-c") and len(token) > 2 and not token.startswith("--"):
            # -c / -c<code>：随后（或贴在同一 argv）是代码，不是文件入口
            return None
        if token == "-m":
            return None
        if token.startswith("-"):
            # 解释器选项。带值的选项跳过下一个 argv。
            if token in ("-c", "-m"):
                return None
            arity = _python_flag_arity(token)
            index += 1 + arity
            continue
        # 第一个非选项参数：脚本路径
        if same_entry(token, root):
            return index
        return None
    return None


def classify_argv(argv, port, root: Path, role=None, require_explicit_port=True):
    """根据已拆开的 argv 判断是否本目录、本端口的受管进程。

    必须命中『执行入口位置』的 xiaoba_monitor.py；仅在 -c 数据里出现路径/flags 不是 match。

    require_explicit_port=True（默认，启停脚本 classify-supervisor-pid）：
      argv 必须带 --port 且与期望端口一致。
    require_explicit_port=False（持有 claim 指纹时）：
      允许仅 --config-file / 配置默认端口、命令行无 --port；实例由指纹钉死。
    """
    if not argv:
        return KIND_STALE, ""
    entry_index = find_executing_entry_index(argv, root)
    if entry_index is None:
        # 可能是无关进程，或 python -c 把路径写进代码字符串
        if any(same_entry(token, root) for token in argv):
            return KIND_STALE, "入口路径未处于执行位置（例如仅出现在 python -c 数据中）"
        # 也检查是否整段埋在某个 -c 参数里（同一 argv 元素含路径子串）
        expected = str(entry_path(root))
        for token in argv:
            if expected in token or "xiaoba_monitor.py" in token:
                return KIND_STALE, "入口路径未处于执行位置（例如仅出现在 python -c 数据中）"
        return KIND_STALE, ""
    script_args = argv[entry_index + 1 :]
    has_supervise = "--supervise" in script_args
    if role == ROLE_SUPERVISE and not has_supervise:
        return KIND_UNKNOWN, "claim 为 supervise 但进程没有 --supervise"
    if role == ROLE_SERVE and has_supervise:
        return KIND_OTHER, "claim 为 serve 但进程带有 --supervise"
    if "--port" in script_args:
        index = script_args.index("--port")
        if index + 1 >= len(script_args):
            return KIND_UNKNOWN, "--port 没有值"
        try:
            seen = int(script_args[index + 1])
            expected = int(port)
        except (TypeError, ValueError):
            return KIND_UNKNOWN, "端口无法比较"
        if seen != expected:
            return KIND_OTHER, "端口为 %d，不是 %d" % (seen, expected)
    elif require_explicit_port:
        return KIND_UNKNOWN, "命令行缺少 --port（配置默认端口须结合 claim 指纹确认）"
    # else: 无显式 --port，且允许隐式——不与 argv 冲突，交由指纹约束实例
    if role == ROLE_SUPERVISE or (role is None and has_supervise):
        if not has_supervise:
            return KIND_UNKNOWN, "缺少 --supervise"
        return KIND_MATCH, ""
    if role == ROLE_SERVE or (role is None and not has_supervise):
        return KIND_MATCH, ""
    return KIND_UNKNOWN, "角色无法判定"


def _linux_process_state(pid: int):
    try:
        text = Path("/proc/%d/stat" % int(pid)).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None, "gone"
    except OSError as exc:
        if exc.errno in (errno.EPERM, errno.EACCES):
            return None, "eperm"
        return None, "error"
    close = text.rfind(")")
    if close < 0:
        return None, "error"
    fields = text[close + 2 :].split()
    if not fields:
        return None, "error"
    return fields[0], ""


def _darwin_proc_pidinfo_reachable(pid: int):
    """zombie 上 kill(0) 仍成功，但 proc_pidinfo 返回失败且 errno=ESRCH。"""
    import ctypes
    import ctypes.util

    lib_name = ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
    try:
        libproc = ctypes.CDLL(lib_name, use_errno=True)
    except OSError:
        return None, "no_libproc"
    PROC_PIDTBSDINFO = 3
    buf = ctypes.create_string_buffer(136)
    ctypes.set_errno(0)
    got = libproc.proc_pidinfo(int(pid), PROC_PIDTBSDINFO, ctypes.c_uint64(0), buf, 136)
    err = ctypes.get_errno()
    if got > 0:
        return True, ""
    if err in (errno.ESRCH, errno.ENOENT):
        return False, "esrch"
    if err in (errno.EPERM, errno.EACCES):
        return None, "eperm"
    return False, "errno_%d" % err


def process_is_zombie(pid: int):
    """父进程未 wait 时，已退出子进程会成僵尸：kill(0) 成功但不可再调度。"""
    if Path("/proc").is_dir():
        state, err = _linux_process_state(pid)
        if state is None:
            return False if err == "eperm" else None
        return state == "Z"
    if sys.platform == "darwin":
        reachable, err = _darwin_proc_pidinfo_reachable(pid)
        if reachable is True:
            return False
        if err == "eperm":
            return None
        # kill(0) 已表明 pid 表项还在，但 bsdinfo 读不到 → 视为僵尸/已退出未收割
        if reachable is False and err == "esrch":
            return True
        return None
    return None


def probe_alive(pid: int):
    """返回 (kind_hint, detail)。kind_hint 为 stale/unknown/alive。

    注意：未收割的僵尸进程上 os.kill(pid, 0) 仍成功；必须另判僵尸，否则 stop 等待会空转。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return KIND_UNKNOWN, "pid 无效"
    if pid <= 1:
        return KIND_STALE, "pid 不是受管目标"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return KIND_STALE, "进程不存在"
    except PermissionError:
        return KIND_UNKNOWN, "进程存在但无权限探测（EPERM）"
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return KIND_STALE, "进程不存在"
        if exc.errno in (errno.EPERM, errno.EACCES):
            return KIND_UNKNOWN, "进程存在但无权限探测（EPERM）"
        return KIND_UNKNOWN, "探测进程失败（%s）" % exc
    zombie = process_is_zombie(pid)
    if zombie is True:
        return KIND_STALE, "进程已退出（僵尸，待父进程收割）"
    if zombie is None:
        # 无法判断是否僵尸时不升格为 unknown 打断停止等待；保持 alive 并依赖后续指纹/超时。
        pass
    return "alive", ""


def classify_process(pid, port, root: Path, role=None, expected_fingerprint=None):
    alive, detail = probe_alive(pid)
    if alive != "alive":
        return alive, detail
    argv, error = read_process_argv(int(pid))
    if argv is None:
        return KIND_UNKNOWN, error or "读不到进程命令行"
    if not argv:
        return KIND_STALE, "进程命令行为空"
    # 有 claim 指纹时允许配置默认端口（命令行无 --port）；无指纹时仍要求显式 --port。
    require_explicit = expected_fingerprint is None
    kind, detail = classify_argv(
        argv, port, root, role=role, require_explicit_port=require_explicit,
    )
    if kind != KIND_MATCH:
        return kind, detail
    # 仅 PID + argv 仍不够：必须核对启动实例指纹，防止 PID 复用。
    current_fp, fp_error = read_process_fingerprint(int(pid))
    if current_fp is None:
        return KIND_UNKNOWN, fp_error or "读不到进程启动指纹"
    if expected_fingerprint is not None:
        if not fingerprints_equal(expected_fingerprint, current_fp):
            return KIND_STALE, "进程启动指纹与 claim 不一致（可能是 PID 复用）"
    return KIND_MATCH, ""


def classify_claim(claim, port, root: Path):
    if claim is None:
        return KIND_STALE, "无 claim"
    if claim.get("_error"):
        return KIND_UNKNOWN, claim.get("detail") or claim["_error"]
    try:
        claim_port = int(claim.get("port"))
        pid = int(claim.get("pid"))
    except (TypeError, ValueError):
        return KIND_UNKNOWN, "claim 字段无效"
    if claim_port != int(port):
        return KIND_OTHER, "claim 端口为 %d，不是 %d" % (claim_port, int(port))
    role = claim.get("role")
    if role not in (ROLE_SUPERVISE, ROLE_SERVE):
        return KIND_UNKNOWN, "claim 角色无效"
    fingerprint = claim.get("fingerprint")
    if not isinstance(fingerprint, dict):
        return KIND_UNKNOWN, "claim 缺少启动指纹"
    return classify_process(pid, port, root, role=role, expected_fingerprint=fingerprint)


def read_legacy_supervisor_pid(run_dir: Path):
    path = supervisor_pid_path(run_dir)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if not text or any(ch not in "0123456789" for ch in text):
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    if pid <= 1:
        return None
    return pid


def inspect_legacy_pid_file(run_dir: Path, root: Path, port: int):
    """旧版仅有 supervisor.pid、无 claim 时的只读诊断。不发信号、不删除文件。"""
    claim = read_claim(run_dir)
    if claim is not None and not claim.get("_error"):
        return {
            "status": "has_claim",
            "detail": "已存在 lifecycle.claim",
            "pid": claim.get("pid"),
            "migration": "",
        }
    legacy_pid = read_legacy_supervisor_pid(run_dir)
    if legacy_pid is None:
        return {
            "status": "none",
            "detail": "无 claim 也无有效 supervisor.pid",
            "pid": None,
            "migration": "",
        }
    # 故意不把裸 PID 的 argv 命中当成可停止目标。
    alive, alive_detail = probe_alive(legacy_pid)
    return {
        "status": STATUS_LEGACY_UNCLAIMED,
        "detail": "%s（旧 pid=%d，探测=%s/%s）" % (LEGACY_MIGRATION, legacy_pid, alive, alive_detail or "ok"),
        "pid": legacy_pid,
        "migration": LEGACY_MIGRATION,
        "port": int(port),
        "root": str(root),
    }


def acquire_claim(run_dir: Path, root: Path, port: int, role: str, pid: int | None = None):
    """在锁内认领。成功返回 (token, None)；失败返回 (None, (code, message))。"""
    if role not in (ROLE_SUPERVISE, ROLE_SERVE):
        return None, (2, "内部错误：未知角色 %r" % (role,))
    run_dir = Path(run_dir)
    root = Path(root)
    pid = int(pid if pid is not None else os.getpid())
    port = int(port)
    with LifecycleLock(run_dir):
        existing = read_claim(run_dir)
        if existing is not None:
            kind, detail = classify_claim(existing, port, root)
            if kind == KIND_MATCH:
                return None, (2, "看护进程已在运行（pid %s）。" % existing.get("pid"))
            if kind == KIND_OTHER:
                return None, (
                    2,
                    "监管进程仍在运行，但不是端口 %d 的看护（pid %s）。请先关闭监看中心，未再启动第二个。"
                    % (port, existing.get("pid")),
                )
            if kind == KIND_UNKNOWN:
                return None, (
                    2,
                    "无法确认 pid %s 是否为端口 %d 的监管进程（%s），未再启动第二个。"
                    % (existing.get("pid"), port, detail or "身份不明"),
                )
            # stale：可覆盖
        else:
            legacy = inspect_legacy_pid_file(run_dir, root, port)
            if legacy["status"] == STATUS_LEGACY_UNCLAIMED:
                # 旧 pid 文件在且无 claim：即使 argv 看起来像监看，也不得自动接管。
                alive, _ = probe_alive(legacy["pid"])
                if alive != KIND_STALE:
                    return None, (2, legacy["detail"])
                # 旧 pid 已不存在：可清掉裸文件后继续认领
                try:
                    supervisor_pid_path(run_dir).unlink()
                except FileNotFoundError:
                    pass
        fingerprint, fp_error = read_process_fingerprint(pid)
        if fingerprint is None:
            return None, (2, "无法读取本进程启动指纹，未认领（%s）" % (fp_error or "unknown"))
        token = secrets.token_hex(16)
        try:
            entry_resolved = str(entry_path(root).resolve())
        except OSError:
            entry_resolved = str(entry_path(root))
        payload = {
            "pid": pid,
            "port": port,
            "role": role,
            "token": token,
            "entry": str(entry_path(root)),
            "entry_resolved": entry_resolved,
            "fingerprint": fingerprint,
            "claimed_at": time.time(),
        }
        write_claim_atomic(run_dir, payload)
        if role == ROLE_SUPERVISE:
            supervisor_pid_path(run_dir).write_text(str(pid), encoding="utf-8")
        return token, None


def release_claim(run_dir: Path, token: str):
    """仅当 claim.token 仍是自己的令牌时解除；防止旧实例 finally 拆掉新所有权。"""
    if not token:
        return False
    run_dir = Path(run_dir)
    with LifecycleLock(run_dir):
        claim = read_claim(run_dir)
        if not claim or claim.get("_error"):
            return False
        if claim.get("token") != token:
            return False
        try:
            claim_path(run_dir).unlink()
        except FileNotFoundError:
            pass
        if claim.get("role") == ROLE_SUPERVISE:
            pid_file = supervisor_pid_path(run_dir)
            try:
                text = pid_file.read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
            if text == str(claim.get("pid")):
                try:
                    pid_file.unlink()
                except FileNotFoundError:
                    pass
        return True


def _clear_claim_if_token(run_dir: Path, token: str, pid) -> bool:
    """持锁调用方使用：token 仍匹配才清理 claim / 旧 pid 文件。"""
    current = read_claim(run_dir)
    if not current or current.get("_error"):
        return False
    if current.get("token") != token:
        return False
    try:
        claim_path(run_dir).unlink()
    except FileNotFoundError:
        pass
    pid_file = supervisor_pid_path(run_dir)
    try:
        if pid_file.read_text(encoding="utf-8").strip() == str(pid):
            pid_file.unlink()
    except OSError:
        pass
    return True


def revalidate_before_signal(pid, port, root: Path, role=None, expected_fingerprint=None):
    """发送信号前的第二次核验。仍无法消除检查到信号之间的残余竞态。"""
    return classify_process(pid, port, root, role=role, expected_fingerprint=expected_fingerprint)


def stop_owned(run_dir: Path, root: Path, port: int, kill=os.kill, sleep=time.sleep, wait_rounds=30):
    """停止本目录已证明归属的进程。不根据监听表选目标。

    锁策略（反死锁）：
    - 持锁：读 claim、复验、发信号；然后释放锁。
    - 无锁：有界等待目标退出（目标 finally → release_claim 需要同一把锁）。
    - 再持锁：仅当 token 仍匹配时清理；未退出或身份未知则保留认领。

    返回 dict：
      status: stopped | already_stopped | conflict | unknown | legacy_unclaimed | error
      signals: 实际成功发出的信号次数
      detail / pid / migration（legacy 时）
    """
    run_dir = Path(run_dir)
    root = Path(root)
    port = int(port)
    signals = 0
    token = None
    pid = None
    role = None
    fingerprint = None

    with LifecycleLock(run_dir):
        claim = read_claim(run_dir)
        if claim is None:
            legacy = inspect_legacy_pid_file(run_dir, root, port)
            if legacy["status"] == STATUS_LEGACY_UNCLAIMED:
                return {
                    "status": STATUS_LEGACY_UNCLAIMED,
                    "signals": 0,
                    "detail": legacy["detail"],
                    "pid": legacy["pid"],
                    "migration": LEGACY_MIGRATION,
                }
            return {
                "status": "already_stopped",
                "signals": 0,
                "detail": "无 lifecycle claim",
                "pid": None,
            }
        if claim.get("_error"):
            return {
                "status": "unknown",
                "signals": 0,
                "detail": claim.get("detail") or claim["_error"],
                "pid": None,
            }
        try:
            pid = int(claim["pid"])
            role = claim["role"]
            claim_port = int(claim["port"])
            token = claim["token"]
            fingerprint = claim.get("fingerprint")
        except (KeyError, TypeError, ValueError) as exc:
            return {
                "status": "unknown",
                "signals": 0,
                "detail": "claim 字段无效：%s" % exc,
                "pid": None,
            }
        if claim_port != port:
            return {
                "status": "conflict",
                "signals": 0,
                "detail": "claim 端口 %d 与请求端口 %d 不一致，未发信号" % (claim_port, port),
                "pid": pid,
            }
        kind, detail = classify_claim(claim, port, root)
        if kind == KIND_STALE:
            _clear_claim_if_token(run_dir, token, pid)
            return {
                "status": "already_stopped",
                "signals": 0,
                "detail": detail or "claim 已过期",
                "pid": pid,
            }
        if kind != KIND_MATCH:
            status = "conflict" if kind == KIND_OTHER else "unknown"
            return {
                "status": status,
                "signals": 0,
                "detail": detail or kind,
                "pid": pid,
            }
        # 残余竞态窗口：此处到 kill 之间 PID 可能被复用。再验一次（含指纹）后立即发信号。
        kind2, detail2 = revalidate_before_signal(
            pid, port, root, role=role, expected_fingerprint=fingerprint,
        )
        if kind2 != KIND_MATCH:
            if kind2 == KIND_STALE:
                _clear_claim_if_token(run_dir, token, pid)
            status = "already_stopped" if kind2 == KIND_STALE else (
                "conflict" if kind2 == KIND_OTHER else "unknown"
            )
            return {
                "status": status,
                "signals": 0,
                "detail": detail2 or kind2,
                "pid": pid,
            }
        try:
            kill(pid, signal.SIGTERM)
            signals = 1
        except ProcessLookupError:
            _clear_claim_if_token(run_dir, token, pid)
            return {
                "status": "already_stopped",
                "signals": 0,
                "detail": "发信号时进程已不存在",
                "pid": pid,
            }
        except PermissionError:
            return {
                "status": "unknown",
                "signals": 0,
                "detail": "无权限向目标发信号（EPERM）",
                "pid": pid,
            }
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                _clear_claim_if_token(run_dir, token, pid)
                return {
                    "status": "already_stopped",
                    "signals": 0,
                    "detail": "发信号时进程已不存在",
                    "pid": pid,
                }
            if exc.errno in (errno.EPERM, errno.EACCES):
                return {
                    "status": "unknown",
                    "signals": 0,
                    "detail": "无权限向目标发信号（EPERM）",
                    "pid": pid,
                }
            return {
                "status": "error",
                "signals": 0,
                "detail": "发信号失败：%s" % exc,
                "pid": pid,
            }
        # 故意在此处结束 with：释放锁后再等待，避免与目标 finally→release_claim 死锁。

    # —— 无锁等待 ——
    exited = False
    for _ in range(wait_rounds):
        alive, _ = probe_alive(pid)
        if alive == KIND_STALE:
            exited = True
            break
        sleep(0.5)

    if not exited:
        # 仍存活：无锁状态下再核验；仅对仍 match 的目标补一次 TERM，然后继续有界等待。
        kind3, _ = revalidate_before_signal(
            pid, port, root, role=role, expected_fingerprint=fingerprint,
        )
        if kind3 == KIND_MATCH:
            try:
                kill(pid, signal.SIGTERM)
                signals += 1
            except (ProcessLookupError, PermissionError, OSError):
                pass
            for _ in range(min(10, wait_rounds)):
                alive, _ = probe_alive(pid)
                if alive == KIND_STALE:
                    exited = True
                    break
                sleep(0.5)
        elif kind3 == KIND_UNKNOWN:
            # 身份未知：保留认领，不清理
            return {
                "status": "unknown",
                "signals": signals,
                "detail": "发信号后无法再次证明目标身份，已保留 lifecycle claim",
                "pid": pid,
            }
        elif kind3 == KIND_OTHER:
            return {
                "status": "conflict",
                "signals": signals,
                "detail": "发信号后目标身份变为其他实例，已保留 lifecycle claim",
                "pid": pid,
            }

    # —— 重新持锁清理或保留 ——
    with LifecycleLock(run_dir):
        alive_after, _ = probe_alive(pid)
        if alive_after != KIND_STALE:
            # 未实际退出：保留认领，供下次关闭或人工处理
            return {
                "status": "error",
                "signals": signals,
                "detail": "已向受管进程发信号但仍未退出，已保留 lifecycle claim",
                "pid": pid,
            }
        if not _clear_claim_if_token(run_dir, token, pid):
            # token 已变：新实例已接管，不得再动
            return {
                "status": "stopped",
                "signals": signals,
                "detail": "目标已退出；claim 令牌已变更，未清理新所有权",
                "pid": pid,
            }
        return {
            "status": "stopped",
            "signals": signals,
            "detail": "已停止受管进程",
            "pid": pid,
        }


# ---------------------------------------------------------------------------
# B09–B11：旧监管受控迁移预检、停止与关闭报告（同文件，不另增部署模块）
#
# 复用上方原生 argv / 启动指纹 / claim / 锁；不把裸 supervisor.pid
# 升格为普通 --stop-owned 权限。监听表只诊断，永不单独作为信号目标。
#
# 顺序约束：B09 绑定必须在同步覆盖 r9 入口脚本之前捕获（argv 路径依赖现场入口）；
# 覆盖后再预检可能因入口路径变化而失败——应先 binding，再同步，再 B10。
# ---------------------------------------------------------------------------

BINDING_NAME = "migration.binding.json"
BINDING_SCHEMA = "legacy_migration_binding_v1"

STATUS_BOUND = "bound"
STATUS_MISSING_PID = "missing_pid"
STATUS_STALE_PID = "stale_pid"
STATUS_PID_REUSED_OR_MISMATCH = "pid_reused_or_mismatch"
STATUS_PERMISSION_UNKNOWN = "permission_unknown"
STATUS_INCOMPLETE_IDENTITY = "incomplete_identity"
STATUS_HAS_CLAIM = "has_claim"
STATUS_CLAIM_CORRUPT = "claim_corrupt"
STATUS_PORT_IDLE = "port_idle"
STATUS_NONE = "none"

PORT_FREE = "free"
PORT_BUSY = "busy"
PORT_UNOBSERVABLE = "unobservable"
PORT_UNCONFIRMED = "unconfirmed"


def binding_path(run_dir: Path) -> Path:
    return Path(run_dir) / BINDING_NAME


def _default_lsof_bin() -> str:
    return os.environ.get("XIAOBA_LSOF") or "/usr/sbin/lsof"


def default_listen_pids(port: int, lsof_bin: str | None = None) -> list[int]:
    """诊断用。失败应向上抛出，由 probe_port_status 标 unobservable。"""
    done = subprocess.run(
        [lsof_bin or _default_lsof_bin(), "-w", "-nP", "-tiTCP:%d" % int(port), "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if done.returncode not in (0, 1) or (done.returncode == 1 and (done.stderr or "").strip()):
        raise OSError("lsof 诊断失败，退出码 %d" % done.returncode)
    return [int(x) for x in (done.stdout or "").split() if x.strip().isdigit()]


def probe_port_status(port: int, lsof_bin: str | None = None) -> dict:
    """端口探测与停止结果分离。按通常规则判定任意端口（含默认 8799）。

    lsof 失败 → unobservable，不得当成 busy。不向目标发信号、不主动建连。
    """
    port = int(port)
    bin_path = lsof_bin or _default_lsof_bin()
    cmd = [bin_path, "-w", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": PORT_UNOBSERVABLE,
            "exit_code": None,
            "listeners": [],
            "detail": "lsof 不可用或超时：%s" % exc,
            "lsof_argv": cmd,
        }
    if done.returncode == 0:
        listeners = []
        try:
            listeners = default_listen_pids(port, lsof_bin=bin_path)
        except (OSError, subprocess.TimeoutExpired):
            listeners = []
        return {
            "status": PORT_BUSY,
            "exit_code": done.returncode,
            "listeners": listeners,
            "detail": "端口仍有监听（诊断，非信号目标）",
            "lsof_argv": cmd,
        }
    if done.returncode == 1 and not (done.stderr or "").strip():
        return {
            "status": PORT_FREE,
            "exit_code": 1,
            "listeners": [],
            "detail": "端口无监听",
            "lsof_argv": cmd,
        }
    return {
        "status": PORT_UNOBSERVABLE,
        "exit_code": done.returncode,
        "listeners": [],
        "detail": "lsof 退出码 %d，无法确认端口状态" % done.returncode,
        "lsof_argv": cmd,
    }


def _claim_blocks_legacy_path(claim) -> dict | None:
    """损坏或可读 claim 一律挡住裸 PID 迁移；仅「无 claim 文件」可继续。"""
    if claim is None:
        return None
    if claim.get("_error"):
        return {
            "status": STATUS_CLAIM_CORRUPT,
            "detail": "lifecycle.claim 损坏或不可读（%s），拒绝裸 PID 迁移"
            % (claim.get("detail") or claim.get("_error")),
            "binding": None,
            "listen_diagnostic": None,
        }
    return {
        "status": STATUS_HAS_CLAIM,
        "detail": "已存在 lifecycle.claim，应走普通 --stop-owned，不得走裸 PID 迁移停止",
        "binding": None,
        "listen_diagnostic": None,
    }


def _fingerprint_complete(fp) -> bool:
    if not isinstance(fp, dict):
        return False
    kind = fp.get("kind")
    if kind == "darwin_bsdinfo":
        return all(isinstance(fp.get(k), int) for k in ("pid", "start_tvsec", "start_tvusec"))
    if kind == "linux_stat":
        return all(isinstance(fp.get(k), int) for k in ("pid", "starttime"))
    return False


def list_child_pids(ppid: int) -> list[int]:
    done = subprocess.run(
        ["/bin/ps", "-ax", "-o", "pid=,ppid="],
        capture_output=True,
        text=True,
    )
    out = []
    for line in (done.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
            parent = int(parts[1])
        except ValueError:
            continue
        if parent == int(ppid) and pid > 1:
            out.append(pid)
    return out


def preflight_legacy_migration(
    run_dir: Path,
    root: Path,
    port: int,
    *,
    listen_pids=None,
    write_binding: bool = True,
) -> dict:
    """B09：旧裸 PID 预检。裸 PID 从未登记启动指纹，不足以确证归属。

    成功时输出可审计 binding（原生指纹 + 精确 root/run_dir/entry + 已核验监听子进程）。
    监听结果只进 listen_diagnostic，不进入信号目标列表。
    看不到监听/子进程身份时不签发可停凭证；端口空闲与归属结果分开表达。
    """
    run_dir = Path(run_dir)
    root = Path(root)
    port = int(port)
    listen_pids = listen_pids or default_listen_pids

    blocked = _claim_blocks_legacy_path(read_claim(run_dir))
    if blocked is not None:
        return blocked

    legacy_pid = read_legacy_supervisor_pid(run_dir)
    if legacy_pid is None:
        return {
            "status": STATUS_MISSING_PID,
            "detail": "无有效 run/supervisor.pid",
            "binding": None,
            "listen_diagnostic": _safe_listen_diag(port, listen_pids),
        }

    alive, alive_detail = probe_alive(legacy_pid)
    if alive == KIND_STALE:
        listen_diag = _safe_listen_diag(port, listen_pids)
        # 旧进程已停与端口空闲分开表达，不混进归属绑定结果
        if listen_diag.get("ok") and not (listen_diag.get("pids") or []):
            return {
                "status": STATUS_PORT_IDLE,
                "detail": "旧 pid %d 已退出且端口 %d 无监听（无需迁移绑定）" % (legacy_pid, port),
                "pid": legacy_pid,
                "binding": None,
                "listen_diagnostic": listen_diag,
                "process_status": STATUS_STALE_PID,
            }
        return {
            "status": STATUS_STALE_PID,
            "detail": "旧 pid %d 已不存在或为僵尸：%s" % (legacy_pid, alive_detail),
            "pid": legacy_pid,
            "binding": None,
            "listen_diagnostic": listen_diag,
        }
    if alive == KIND_UNKNOWN:
        return {
            "status": STATUS_PERMISSION_UNKNOWN,
            "detail": "旧 pid %d 权限不确定：%s" % (legacy_pid, alive_detail),
            "pid": legacy_pid,
            "binding": None,
            "listen_diagnostic": _safe_listen_diag(port, listen_pids),
        }

    argv, argv_err = read_process_argv(legacy_pid)
    if argv is None:
        return {
            "status": STATUS_PERMISSION_UNKNOWN,
            "detail": "读不到旧监管 argv：%s" % (argv_err or "unknown"),
            "pid": legacy_pid,
            "binding": None,
            "listen_diagnostic": _safe_listen_diag(port, listen_pids),
        }

    entry_index = find_executing_entry_index(argv, root)
    if entry_index is None:
        return {
            "status": STATUS_PID_REUSED_OR_MISMATCH,
            "detail": "pid %d 存活但入口不在执行位置（可能 PID 复用或非本目录监管）" % legacy_pid,
            "pid": legacy_pid,
            "argv": argv,
            "binding": None,
            "listen_diagnostic": _safe_listen_diag(port, listen_pids),
        }

    kind, detail = classify_argv(
        argv, port, root, role=ROLE_SUPERVISE, require_explicit_port=True,
    )
    if kind != KIND_MATCH:
        return {
            "status": STATUS_PID_REUSED_OR_MISMATCH,
            "detail": "argv 身份不匹配：%s" % (detail or kind),
            "pid": legacy_pid,
            "argv": argv,
            "binding": None,
            "listen_diagnostic": _safe_listen_diag(port, listen_pids),
        }

    fingerprint, fp_err = read_process_fingerprint(legacy_pid)
    if fingerprint is None or not _fingerprint_complete(fingerprint):
        return {
            "status": STATUS_PERMISSION_UNKNOWN if "EPERM" in (fp_err or "") else STATUS_INCOMPLETE_IDENTITY,
            "detail": "无法捕获完整原生启动指纹（旧裸 PID 本身无登记指纹）：%s" % (fp_err or "incomplete"),
            "pid": legacy_pid,
            "argv": argv,
            "binding": None,
            "listen_diagnostic": _safe_listen_diag(port, listen_pids),
        }

    listen_diag = _safe_listen_diag(port, listen_pids)
    # 看不到监听表 → 不签发可停凭证（与归属失败分开：这是身份不完整）
    if not listen_diag.get("ok"):
        return {
            "status": STATUS_INCOMPLETE_IDENTITY,
            "detail": "监听不可观测，无法证明子进程/监听身份，不签发迁移绑定",
            "pid": legacy_pid,
            "argv": argv,
            "fingerprint": fingerprint,
            "binding": None,
            "listen_diagnostic": listen_diag,
        }

    listeners = list(listen_diag.get("pids") or [])
    if not listeners:
        # 端口明确空闲：单独状态，不是 bound
        return {
            "status": STATUS_PORT_IDLE,
            "detail": "端口 %d 无监听；监管仍存活但缺少可核验监听/子进程身份，不签发可停凭证" % port,
            "pid": legacy_pid,
            "argv": argv,
            "fingerprint": fingerprint,
            "binding": None,
            "listen_diagnostic": listen_diag,
        }

    children = []
    for cpid in list_child_pids(legacy_pid):
        c_argv, c_err = read_process_argv(cpid)
        if c_argv is None:
            children.append({"pid": cpid, "status": "permission_unknown", "detail": c_err})
            continue
        c_kind, c_detail = classify_argv(
            c_argv, port, root, role=ROLE_SERVE, require_explicit_port=True,
        )
        entry_ok = find_executing_entry_index(c_argv, root) is not None
        c_fp, _ = read_process_fingerprint(cpid)
        children.append({
            "pid": cpid,
            "ppid": legacy_pid,
            "argv": c_argv,
            "classify": c_kind,
            "detail": c_detail,
            "entry_ok": entry_ok,
            "fingerprint": c_fp if _fingerprint_complete(c_fp) else None,
            "listens_on_port": cpid in listeners,
        })

    verified_children = [
        c for c in children
        if c.get("classify") == KIND_MATCH and c.get("entry_ok") and c.get("fingerprint")
    ]
    verified_listeners = [
        c for c in verified_children if c["pid"] in listeners
    ]
    if legacy_pid in listeners:
        # 监管自身监听也算可核验监听身份
        pass
    elif not verified_listeners:
        return {
            "status": STATUS_INCOMPLETE_IDENTITY,
            "detail": "端口 %d 监听 %s 无法证明为已核验子进程身份；不签发可停凭证，监听不作为信号目标"
            % (port, listeners),
            "pid": legacy_pid,
            "argv": argv,
            "fingerprint": fingerprint,
            "children": children,
            "binding": None,
            "listen_diagnostic": listen_diag,
        }

    ok_listeners = set()
    for lp in listeners:
        if lp == legacy_pid:
            ok_listeners.add(lp)
        elif any(c["pid"] == lp for c in verified_listeners):
            ok_listeners.add(lp)
    if set(listeners) - ok_listeners:
        return {
            "status": STATUS_INCOMPLETE_IDENTITY,
            "detail": "端口 %d 存在未核验监听进程 %s；不签发可停凭证"
            % (port, sorted(set(listeners) - ok_listeners)),
            "pid": legacy_pid,
            "argv": argv,
            "fingerprint": fingerprint,
            "children": children,
            "binding": None,
            "listen_diagnostic": listen_diag,
        }

    try:
        root_res = str(root.resolve())
        run_res = str(run_dir.resolve())
        entry_res = str(entry_path(root).resolve())
    except OSError as exc:
        return {
            "status": STATUS_INCOMPLETE_IDENTITY,
            "detail": "无法解析 root/run_dir/entry：%s" % exc,
            "pid": legacy_pid,
            "binding": None,
            "listen_diagnostic": listen_diag,
        }

    binding_id = secrets.token_hex(16)
    binding = {
        "schema": BINDING_SCHEMA,
        "binding_id": binding_id,
        "pid": legacy_pid,
        "port": port,
        "role": ROLE_SUPERVISE,
        "root": root_res,
        "run_dir": run_res,
        "entry": str(entry_path(root)),
        "entry_resolved": entry_res,
        "entry_index": entry_index,
        "argv": argv,
        "fingerprint": fingerprint,
        "children": [
            {
                "pid": c["pid"],
                "ppid": c["ppid"],
                "argv": c["argv"],
                "fingerprint": c["fingerprint"],
            }
            for c in verified_children
        ],
        "bound_at": time.time(),
        "issuer": {
            "module": "monitor_core.lifecycle",
            "api": "preflight_legacy_migration",
            "binding_id": binding_id,
            "schema": BINDING_SCHEMA,
        },
        "order_constraint": (
            "须在同步覆盖 r9 监看入口脚本之前捕获本 binding；"
            "同步后若入口路径变化，应重新预检或按本 binding 在覆盖前完成 migration_stop。"
        ),
        "signal_targets": [legacy_pid],
        "listen_diagnostic_at_bind": listen_diag,
    }

    if write_binding:
        path = binding_path(run_dir)
        tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
        tmp.write_text(json.dumps(binding, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(path))

    return {
        "status": STATUS_BOUND,
        "detail": "已绑定旧监管身份（现场原生指纹 + 入口 + 已核验监听子进程）",
        "pid": legacy_pid,
        "binding": binding,
        "binding_path": str(binding_path(run_dir)) if write_binding else None,
        "listen_diagnostic": listen_diag,
    }


def _safe_listen_diag(port: int, listen_pids) -> dict:
    try:
        pids = list(listen_pids(port))
        return {"port": int(port), "pids": pids, "note": "诊断 only，永不单独作为信号目标", "ok": True}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "port": int(port),
            "pids": [],
            "note": "诊断失败：%s" % exc,
            "ok": False,
            "status": PORT_UNOBSERVABLE,
        }


def load_binding(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != BINDING_SCHEMA:
        raise ValueError("binding schema 无效")
    return data


def load_binding_result(path) -> dict:
    """公开 CLI 用：缺失/损坏 binding → 可判定 JSON，不抛 traceback。"""
    try:
        return {"ok": True, "binding": load_binding(path), "status": "ok", "signals": 0, "detail": ""}
    except FileNotFoundError:
        return {
            "ok": False,
            "binding": None,
            "status": "rejected",
            "signals": 0,
            "detail": "binding 文件缺失：%s" % path,
            "trace": [],
        }
    except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
        return {
            "ok": False,
            "binding": None,
            "status": "rejected",
            "signals": 0,
            "detail": "binding 无效：%s" % exc,
            "trace": [],
        }


def _validate_binding_for_stop(run_dir: Path, root: Path, port: int, binding: dict) -> dict | None:
    """返回拒绝结果 dict，或 None 表示字段级通过（锁外）。"""
    if not isinstance(binding, dict) or binding.get("schema") != BINDING_SCHEMA:
        return {"status": "rejected", "signals": 0, "detail": "拒绝：缺少有效 B09 binding", "trace": []}
    issuer = binding.get("issuer")
    if not isinstance(issuer, dict) or issuer.get("module") != "monitor_core.lifecycle":
        return {"status": "rejected", "signals": 0, "detail": "拒绝：缺少候选签发 issuer 证据", "trace": []}
    if issuer.get("binding_id") != binding.get("binding_id"):
        return {"status": "rejected", "signals": 0, "detail": "拒绝：issuer.binding_id 与 binding 不一致", "trace": []}
    try:
        b_pid = int(binding["pid"])
        b_port = int(binding["port"])
        b_fp = binding["fingerprint"]
        b_root = Path(binding["root"])
        b_run = Path(binding["run_dir"])
        b_entry = Path(binding["entry"])
        targets = [int(x) for x in binding.get("signal_targets") or [b_pid]]
    except (KeyError, TypeError, ValueError) as exc:
        return {"status": "rejected", "signals": 0, "detail": "binding 字段无效：%s" % exc, "trace": []}
    if not _fingerprint_complete(b_fp):
        return {"status": "rejected", "signals": 0, "detail": "拒绝：binding 缺少完整原生启动指纹", "trace": []}
    if b_port != port:
        return {
            "status": "rejected",
            "signals": 0,
            "detail": "binding 端口 %d 与请求 %d 不一致" % (b_port, port),
            "trace": [],
        }
    try:
        if b_root.resolve() != root.resolve():
            return {"status": "rejected", "signals": 0, "detail": "binding.root 与当前 root 不一致", "trace": []}
        if b_run.resolve() != run_dir.resolve():
            return {"status": "rejected", "signals": 0, "detail": "binding.run_dir 与当前 run_dir 不一致", "trace": []}
        if b_entry.resolve() != entry_path(root).resolve():
            return {"status": "rejected", "signals": 0, "detail": "binding.entry 与当前入口不一致", "trace": []}
    except OSError:
        return {"status": "rejected", "signals": 0, "detail": "无法解析 root/run_dir/entry", "trace": []}
    if set(targets) != {b_pid}:
        return {
            "status": "rejected",
            "signals": 0,
            "detail": "binding.signal_targets 只能是监管 pid，禁止扩监听表",
            "trace": [],
        }
    for child in binding.get("children") or []:
        if not _fingerprint_complete(child.get("fingerprint")):
            return {
                "status": "rejected",
                "signals": 0,
                "detail": "拒绝：binding.children 缺少完整原生启动指纹",
                "trace": [],
            }
    return None


def _cleanup_legacy_artifacts(run_dir: Path, binding: dict, b_pid: int):
    cleaned = []
    evidence = []
    pid_file = supervisor_pid_path(run_dir)
    try:
        text = pid_file.read_text(encoding="utf-8").strip()
        if text == str(b_pid):
            pid_file.unlink()
            cleaned.append("supervisor.pid")
            evidence.append({"action": "unlink", "path": str(pid_file), "matched_pid": b_pid})
        else:
            evidence.append({"action": "skip_pid_file", "content": text, "expected": b_pid})
    except FileNotFoundError:
        evidence.append({"action": "pid_file_already_absent", "path": str(pid_file)})
    except OSError as exc:
        evidence.append({"action": "pid_file_error", "detail": str(exc)})
    bpath = binding_path(run_dir)
    try:
        current = load_binding(bpath)
        if current.get("binding_id") == binding.get("binding_id"):
            bpath.unlink()
            cleaned.append(BINDING_NAME)
            evidence.append({"action": "unlink", "path": str(bpath), "binding_id": binding.get("binding_id")})
        else:
            evidence.append({"action": "skip_binding", "reason": "binding_id mismatch"})
    except FileNotFoundError:
        evidence.append({"action": "binding_absent"})
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        evidence.append({"action": "binding_error", "detail": str(exc)})
    return cleaned, evidence


def _children_survival(children, port, root, wait_rounds, sleep) -> tuple[list[int], dict]:
    expected = []
    state = {}
    for child in children or []:
        try:
            cpid = int(child["pid"])
            cfp = child.get("fingerprint")
        except (KeyError, TypeError, ValueError):
            continue
        ck, cd = revalidate_before_signal(
            cpid, port, root, role=ROLE_SERVE, expected_fingerprint=cfp,
        )
        if ck == KIND_MATCH:
            expected.append(cpid)
            gone = False
            for _ in range(min(20, wait_rounds)):
                alive, _ = probe_alive(cpid)
                if alive == KIND_STALE:
                    gone = True
                    break
                sleep(0.25)
            state[str(cpid)] = "exited" if gone else "survived"
        elif ck == KIND_STALE:
            state[str(cpid)] = "exited"
        else:
            state[str(cpid)] = "unknown:%s" % ck
    return expected, state


def _migration_stale_is_live_mismatch(pid: int, kind: str, detail: str):
    """仅 migration_stop：把『仍存活但身份不符』与『确实已退出』分开。

    classify_argv 在入口不在 argv 时也会回 KIND_STALE；若直接当 already_stopped
    会误清 supervisor.pid。此处用 probe_alive 再判一次，不改全局 classify/stop_owned。
    返回 (disposition, message)：
      exited → 可按 already_stopped/partial 清理
      rejected → 活的无关/不符进程，零信号、不清理
      unknown → 存活与否无法确认，零信号、不清理
    """
    if kind != KIND_STALE:
        return None, detail
    alive, alive_detail = probe_alive(pid)
    if alive == "alive":
        return "rejected", (
            "活进程身份与 binding 不符（%s）；未发信号、未清理旧记录"
            % (detail or "argv/指纹不匹配")
        )
    if alive == KIND_UNKNOWN:
        return "unknown", "无法确认 pid %d 是否仍存活（%s）；未发信号、未清理" % (
            int(pid), alive_detail or "unknown",
        )
    return "exited", detail


def migration_stop(
    run_dir: Path,
    root: Path,
    port: int,
    binding,
    *,
    kill=os.kill,
    sleep=time.sleep,
    wait_rounds: int = 40,
) -> dict:
    """B10：仅接受 B09 binding 的受控迁移停止。不扩大普通关闭权限。"""
    run_dir = Path(run_dir)
    root = Path(root)
    port = int(port)
    if isinstance(binding, (str, Path)):
        loaded = load_binding_result(binding)
        if not loaded["ok"]:
            return {
                "status": loaded["status"],
                "signals": 0,
                "detail": loaded["detail"],
                "trace": [],
            }
        binding = loaded["binding"]

    blocked = _claim_blocks_legacy_path(read_claim(run_dir))
    if blocked is not None:
        return {
            "status": "rejected",
            "signals": 0,
            "detail": blocked["detail"],
            "claim_status": blocked["status"],
            "trace": [],
        }

    rejected = _validate_binding_for_stop(run_dir, root, port, binding if isinstance(binding, dict) else {})
    if rejected is not None:
        return rejected

    b_pid = int(binding["pid"])
    b_fp = binding["fingerprint"]
    children = binding.get("children") or []
    trace = []

    kind, detail = revalidate_before_signal(
        b_pid, port, root, role=ROLE_SUPERVISE, expected_fingerprint=b_fp,
    )
    trace.append({"phase": "revalidate_supervisor", "kind": kind, "detail": detail})

    def _finish_exited_without_signal(status, detail_text, signals=0):
        """仅真实退出路径：可清理裸 pid / binding。"""
        _, children_state = _children_survival(children, port, root, wait_rounds, sleep)
        survived = [k for k, v in children_state.items() if v == "survived"]
        cleaned, evidence = [], []
        with LifecycleLock(run_dir):
            cleaned, evidence = _cleanup_legacy_artifacts(run_dir, binding, b_pid)
        if survived:
            return {
                "status": "partial",
                "signals": signals,
                "detail": "监管已退出，但绑定子进程仍存活：%s" % survived,
                "pid": b_pid,
                "children": children_state,
                "cleaned": cleaned,
                "cleanup_evidence": evidence,
                "trace": trace,
            }
        return {
            "status": status,
            "signals": signals,
            "detail": detail_text,
            "pid": b_pid,
            "children": children_state,
            "cleaned": cleaned,
            "cleanup_evidence": evidence,
            "trace": trace,
        }

    if kind == KIND_STALE:
        disposition, msg = _migration_stale_is_live_mismatch(b_pid, kind, detail)
        trace.append({"phase": "stale_alive_check", "disposition": disposition, "detail": msg})
        if disposition == "rejected":
            return {
                "status": "rejected",
                "signals": 0,
                "detail": msg,
                "pid": b_pid,
                "cleaned": [],
                "trace": trace,
            }
        if disposition == "unknown":
            return {
                "status": "unknown",
                "signals": 0,
                "detail": msg,
                "pid": b_pid,
                "cleaned": [],
                "trace": trace,
            }
        return _finish_exited_without_signal("already_stopped", "监管已退出：%s" % (msg or detail))
    if kind != KIND_MATCH:
        return {
            "status": "unknown" if kind == KIND_UNKNOWN else "rejected",
            "signals": 0,
            "detail": "发信号前核验失败：%s" % (detail or kind),
            "pid": b_pid,
            "cleaned": [],
            "trace": trace,
        }

    # 子进程再核验（不向其发信号）
    expected_children = []
    for child in children:
        try:
            cpid = int(child["pid"])
            cfp = child.get("fingerprint")
        except (KeyError, TypeError, ValueError):
            continue
        ck, cd = revalidate_before_signal(
            cpid, port, root, role=ROLE_SERVE, expected_fingerprint=cfp,
        )
        trace.append({"phase": "revalidate_child", "pid": cpid, "kind": ck, "detail": cd})
        if ck == KIND_MATCH:
            expected_children.append(cpid)

    signals = 0
    with LifecycleLock(run_dir):
        # 锁内复查 claim / 旧 PID 文件 / 指纹
        blocked2 = _claim_blocks_legacy_path(read_claim(run_dir))
        if blocked2 is not None:
            return {
                "status": "rejected",
                "signals": 0,
                "detail": "锁内拒绝：%s" % blocked2["detail"],
                "claim_status": blocked2["status"],
                "trace": trace,
            }
        live_pid = read_legacy_supervisor_pid(run_dir)
        if live_pid != b_pid:
            return {
                "status": "rejected",
                "signals": 0,
                "detail": "锁内拒绝：supervisor.pid 已变为 %r，与 binding.pid %d 不一致" % (live_pid, b_pid),
                "trace": trace,
            }
        kind2, detail2 = revalidate_before_signal(
            b_pid, port, root, role=ROLE_SUPERVISE, expected_fingerprint=b_fp,
        )
        trace.append({"phase": "revalidate_before_kill", "kind": kind2, "detail": detail2})
        if kind2 == KIND_STALE:
            disposition2, msg2 = _migration_stale_is_live_mismatch(b_pid, kind2, detail2)
            trace.append({"phase": "stale_alive_check_locked", "disposition": disposition2, "detail": msg2})
            if disposition2 == "rejected":
                return {
                    "status": "rejected",
                    "signals": 0,
                    "detail": "锁内拒绝：%s" % msg2,
                    "pid": b_pid,
                    "cleaned": [],
                    "trace": trace,
                }
            if disposition2 == "unknown":
                return {
                    "status": "unknown",
                    "signals": 0,
                    "detail": "锁内：%s" % msg2,
                    "pid": b_pid,
                    "cleaned": [],
                    "trace": trace,
                }
            cleaned, evidence = _cleanup_legacy_artifacts(run_dir, binding, b_pid)
            _, children_state = _children_survival(children, port, root, wait_rounds, sleep)
            survived = [k for k, v in children_state.items() if v == "survived"]
            if survived:
                return {
                    "status": "partial",
                    "signals": 0,
                    "detail": "锁内监管已退出，绑定子进程仍存活：%s" % survived,
                    "pid": b_pid,
                    "children": children_state,
                    "cleaned": cleaned,
                    "cleanup_evidence": evidence,
                    "trace": trace,
                }
            return {
                "status": "already_stopped",
                "signals": 0,
                "detail": msg2 or detail2 or kind2,
                "pid": b_pid,
                "children": children_state,
                "cleaned": cleaned,
                "cleanup_evidence": evidence,
                "trace": trace,
            }
        if kind2 != KIND_MATCH:
            return {
                "status": "unknown" if kind2 == KIND_UNKNOWN else "rejected",
                "signals": 0,
                "detail": detail2 or kind2,
                "pid": b_pid,
                "trace": trace,
            }
        try:
            kill(b_pid, signal.SIGTERM)
            signals = 1
            trace.append({"phase": "signal", "pid": b_pid, "sig": int(signal.SIGTERM)})
        except ProcessLookupError:
            cleaned, evidence = _cleanup_legacy_artifacts(run_dir, binding, b_pid)
            _, children_state = _children_survival(children, port, root, wait_rounds, sleep)
            survived = [k for k, v in children_state.items() if v == "survived"]
            status = "partial" if survived else "already_stopped"
            return {
                "status": status,
                "signals": 0,
                "detail": "发信号时监管已不存在" + (("；子进程仍存活：%s" % survived) if survived else ""),
                "pid": b_pid,
                "children": children_state,
                "cleaned": cleaned,
                "cleanup_evidence": evidence,
                "trace": trace,
            }
        except PermissionError:
            return {
                "status": "unknown",
                "signals": 0,
                "detail": "无权限向监管发信号（EPERM）",
                "pid": b_pid,
                "trace": trace,
            }
        except OSError as exc:
            return {
                "status": "error",
                "signals": 0,
                "detail": "发信号失败：%s" % exc,
                "pid": b_pid,
                "trace": trace,
            }

    def _gone(pid: int) -> bool:
        alive, _ = probe_alive(pid)
        return alive == KIND_STALE

    supervisor_gone = False
    for _ in range(wait_rounds):
        if _gone(b_pid):
            supervisor_gone = True
            break
        sleep(0.5)

    children_state = {}
    for cpid in expected_children:
        gone = False
        for _ in range(min(20, wait_rounds)):
            if _gone(cpid):
                gone = True
                break
            sleep(0.25)
        children_state[str(cpid)] = "exited" if gone else "survived"

    cleaned, evidence = [], []
    if supervisor_gone:
        with LifecycleLock(run_dir):
            cleaned, evidence = _cleanup_legacy_artifacts(run_dir, binding, b_pid)

    survived_children = [k for k, v in children_state.items() if v == "survived"]
    if not supervisor_gone:
        return {
            "status": "timeout",
            "signals": signals,
            "detail": "监管在时限内未退出；现场可追溯",
            "pid": b_pid,
            "children": children_state,
            "cleaned": cleaned,
            "cleanup_evidence": evidence,
            "trace": trace,
        }
    if survived_children:
        return {
            "status": "partial",
            "signals": signals,
            "detail": "监管已退出，但已核验子进程仍存活：%s" % survived_children,
            "pid": b_pid,
            "children": children_state,
            "cleaned": cleaned,
            "cleanup_evidence": evidence,
            "trace": trace,
        }
    return {
        "status": "stopped",
        "signals": signals,
        "detail": "受控迁移停止完成",
        "pid": b_pid,
        "children": children_state,
        "cleaned": cleaned,
        "cleanup_evidence": evidence,
        "trace": trace,
    }


def close_report(
    run_dir: Path,
    root: Path,
    port: int,
    *,
    stop_result: dict | None = None,
    lsof_bin: str | None = None,
) -> dict:
    """B11：停止结果与端口状态分开，供 stage75r 机器判定。"""
    if stop_result is None:
        stop_result = stop_owned(run_dir, root, port)
    port_probe = probe_port_status(port, lsof_bin=lsof_bin)

    stop_ok = stop_result.get("status") in ("stopped", "already_stopped")
    port_free = port_probe.get("status") == PORT_FREE
    closed_ok = bool(stop_ok and port_free)

    if closed_ok:
        user_message = "小八监看中心已关闭。"
        exit_code = 0
    elif not stop_ok:
        user_message = "关闭未成功：%s" % stop_result.get("detail", stop_result.get("status"))
        exit_code = 3 if stop_result.get("status") in (
            "conflict", "unknown", STATUS_LEGACY_UNCLAIMED, "rejected",
        ) else 1
    elif port_probe.get("status") == PORT_BUSY:
        user_message = (
            "停止已返回成功，但端口 %d 仍有监听（可能被其他进程占用）；"
            "未向监听表选中的进程发信号。" % int(port)
        )
        exit_code = 1
    elif port_probe.get("status") == PORT_UNOBSERVABLE:
        user_message = (
            "停止侧已完成，但查不了端口 %d 是否在监听（%s），不能确认已关闭。"
            % (int(port), port_probe.get("detail"))
        )
        exit_code = 4
    else:
        user_message = "关闭状态未确认：%s" % port_probe.get("detail")
        exit_code = 4

    return {
        "schema": "monitor_close_report_v1",
        "stop": stop_result,
        "port_probe": port_probe,
        "closed_ok": closed_ok,
        "user_message": user_message,
        "exit_code": exit_code,
    }
