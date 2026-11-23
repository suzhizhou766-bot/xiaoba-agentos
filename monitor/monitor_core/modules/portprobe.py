"""端口监听探测：裁决后端认领、本地工具控制器接管、Key 池代理接管三处共用的 lsof 封装。

三处以前都写死 /usr/sbin/lsof，失败一律当成"没有监听"：lsof 缺失、被拒或超时的时候，
旧后端不再被认领，旧控制器和旧代理占着端口却被报成"被其他程序占用"，全部静默失效。
这里把结果分成三态，"查不了"不再冒充"没人监听"：

  - CLEAR        确认端口上没有监听；
  - LISTENING    确认存在监听，pids 是监听进程号；
  - UNAVAILABLE  检测不可用（找不到 lsof、被系统拒绝或超时），不知道有没有人监听。

调用方拿到 UNAVAILABLE 必须当作"不知道"处理：不接管、不停止任何进程，并把 detail 里
的原因写进页面可见的警告。
"""
import os
import shutil
import subprocess

CLEAR = "clear"            # 确认没有监听
LISTENING = "listening"    # 确认存在监听
UNAVAILABLE = "unavailable"  # 检测不可用：不知道有没有人监听

# shutil.which 找不到时再试的固定路径（macOS 自带的在 /usr/sbin）
FALLBACK_PATHS = ("/usr/sbin/lsof", "/usr/bin/lsof", "/bin/lsof", "/usr/local/bin/lsof", "/opt/homebrew/bin/lsof")

DEFAULT_TIMEOUT = 5


def find_lsof(fallback=FALLBACK_PATHS):
    """返回可用的 lsof 路径：先 shutil.which，找不到再试固定路径；都没有就返回 None。"""
    found = shutil.which("lsof")
    if found:
        return found
    for path in fallback:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _brief(text, limit=120):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def listener_pids(port, timeout=DEFAULT_TIMEOUT, lsof=None, fallback=FALLBACK_PATHS):
    """检测 port 上的 TCP LISTEN，返回 (state, pids, detail)。

    state 是 CLEAR / LISTENING / UNAVAILABLE 三态之一；pids 只在 LISTENING 时有意义；
    detail 只在 UNAVAILABLE 时给出给人看的原因。lsof 参数供测试注入假的可执行文件。
    """
    try:
        port = int(port)
    except (TypeError, ValueError):
        return UNAVAILABLE, [], "端口号无效：%r" % (port,)
    path = lsof or find_lsof(fallback)
    if not path:
        return UNAVAILABLE, [], "找不到 lsof 命令，端口 %d 的监听检测不可用" % port
    try:
        done = subprocess.run([path, "-nP", "-tiTCP:%d" % port, "-sTCP:LISTEN"],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return UNAVAILABLE, [], "lsof 查询端口 %d 超时，监听检测不可用" % port
    except OSError as exc:
        return UNAVAILABLE, [], "lsof 无法执行（%s），端口 %d 的监听检测不可用" % (exc, port)
    pids = [int(token) for token in done.stdout.split() if token.isdigit()]
    if done.returncode == 0:
        return (LISTENING, pids, "") if pids else (CLEAR, [], "")
    # lsof 用退出码 1 且无输出表示"没有匹配的监听"，这是唯一可确认的"没有人"；
    # 带着报错的非零退出码一律按检测不可用算，不当作没人监听。
    if done.returncode == 1 and not done.stdout.strip() and not (done.stderr or "").strip():
        return CLEAR, [], ""
    message = _brief(done.stderr)
    suffix = "：%s" % message if message else "（无错误输出，退出码 %d）" % done.returncode
    return UNAVAILABLE, [], "lsof 查询端口 %d 失败%s，监听检测不可用" % (port, suffix)
