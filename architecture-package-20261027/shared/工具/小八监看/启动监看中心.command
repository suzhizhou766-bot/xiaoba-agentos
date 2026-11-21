#!/bin/bash
# 开启小八监看中心（运行总览 / 裁决中心 / 导演台），并在浏览器里打开。
# 带看护：监看中心意外退出或不响应时会自动重启；它管理的裁决后端也一并拉起和看护。
# 已在运行时只打开页面。关闭：双击同目录的“关闭监看中心.command”。不会开机自启。
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14"  # 统一解释器（2026-10-22 融合迭代第 2 步）
# 端口优先级：测试环境变量 XIAOBA_MONITOR_PORT > 同目录 monitor.json > 默认 8799。
# 环境变量也先走 --resolve-port（同一校验入口）；通过后再把解析出的有效端口显式传给运行进程，
# 使健康探测、实际监听和打开页面绑定同一结果。非法值在探测/建日志/拉起之前失败。
RESOLVE_ARGS=()
if [ -n "${XIAOBA_MONITOR_PORT:-}" ]; then
  RESOLVE_ARGS=(--port "$XIAOBA_MONITOR_PORT")
fi
if ! PORT="$("$PY" -B "$DIR/xiaoba_monitor.py" --resolve-port "${RESOLVE_ARGS[@]}")"; then
  echo "启动失败：读不到可用的监听端口（检查同目录 monitor.json 或 XIAOBA_MONITOR_PORT）。" >&2
  exit 1
fi
URL="http://127.0.0.1:${PORT}/"
ready() { /usr/bin/curl -fsS --max-time 2 "${URL}healthz" >/dev/null 2>&1; }
supervisor_running() {
  local pid kind
  pid="$(cat "$DIR/run/supervisor.pid" 2>/dev/null)" || return 1
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  [ "$pid" -gt 1 ] && kill -0 "$pid" 2>/dev/null || return 1
  # 与监管进程入口使用同一判定。ps 截断或 PID 复用时，不再用另一套 shell 规则。
  kind="$("$PY" -B "$DIR/xiaoba_monitor.py" --classify-supervisor-pid "$pid" --port "$PORT" 2>/dev/null)" || return 1
  [ "$kind" = "match" ]
}
supervised_ready() { ready && supervisor_running; }
if ! ready; then
  mkdir -p "$DIR/logs"
  cd "$DIR" || exit 1
  nohup "$PY" -B "$DIR/xiaoba_monitor.py" --supervise --detach --port "$PORT" >> "$DIR/logs/supervisor.out.log" 2>&1 &
  for _ in $(seq 1 40); do supervised_ready && break; sleep 0.5; done
fi
if supervised_ready; then
  echo "小八监看中心已开启：$URL"
  echo "（裁决后端首次启动要做发布校验与第二大脑同步，约需几十秒，期间裁决页会提示“正在启动”。）"
  [ "${XIAOBA_MONITOR_NO_BROWSER:-0}" = "1" ] || /usr/bin/open "$URL"
else
  if ready; then
    echo "启动失败：监看服务仍可响应，但监管进程未运行或身份不匹配。请先关闭监看中心再重新启动。" >&2
  else
    echo "启动失败。日志：$DIR/logs/supervisor.out.log 与 $DIR/logs/monitor.log" >&2
  fi
  exit 1
fi
