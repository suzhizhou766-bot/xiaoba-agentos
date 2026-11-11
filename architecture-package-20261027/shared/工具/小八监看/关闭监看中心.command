#!/bin/bash
# 关闭小八监看中心：先停看护进程（它会带着监看中心和裁决后端一起停），再清理残留。
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14"  # 统一解释器（与启动脚本同一套）
PID_FILE="$DIR/run/supervisor.pid"
# 端口监听检测用的 lsof。lsof 找不到时下面的检查只能如实说“查不了”，不能当成已经关了。
LSOF="${XIAOBA_LSOF:-/usr/sbin/lsof}"
# 与启动脚本同一规则解析有效端口；非法配置在杀进程前清楚失败。
RESOLVE_ARGS=()
if [ -n "${XIAOBA_MONITOR_PORT:-}" ]; then
  RESOLVE_ARGS=(--port "$XIAOBA_MONITOR_PORT")
fi
if ! PORT="$("$PY" -B "$DIR/xiaoba_monitor.py" --resolve-port "${RESOLVE_ARGS[@]}")"; then
  echo "关闭失败：读不到可用的监听端口（检查同目录 monitor.json 或 XIAOBA_MONITOR_PORT）。" >&2
  exit 1
fi
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  kill "$(cat "$PID_FILE")"
  for _ in $(seq 1 30); do kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null || break; sleep 0.5; done
fi
# 没有经看护启动的监看进程（直接运行的）也一并结束——只动解析出的端口，不写死 8799。
PIDS="$("$LSOF" -nP -tiTCP:${PORT} -sTCP:LISTEN 2>/dev/null)"
[ -n "$PIDS" ] && kill $PIDS 2>/dev/null
sleep 1
"$LSOF" -nP -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1
PROBE=$?
if [ "$PROBE" -eq 0 ]; then
  echo "监看中心仍在运行，请稍后重试。" >&2; exit 1
fi
if [ "$PROBE" -ne 1 ]; then
  # lsof 不在、跑不起来或超时：端口有没有在监听根本查不到，不能报“已关闭”
  # ${PROBE} 要加花括号：bash 3.2 在 $变量 紧跟全角字符时会吞字节
  echo "查不了端口 ${PORT} 是否在监听（lsof 退出码 ${PROBE}），无法确认是否已关闭。" >&2
  exit 0
fi
echo "小八监看中心已关闭。"
