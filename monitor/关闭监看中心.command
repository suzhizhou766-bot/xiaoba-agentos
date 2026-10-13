#!/bin/bash
# 关闭小八监看中心：先停看护进程（它会带着监看中心和裁决后端一起停），再清理残留。
DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$DIR/run/supervisor.pid"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  kill "$(cat "$PID_FILE")"
  for _ in $(seq 1 30); do kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null || break; sleep 0.5; done
fi
# 没有经看护启动的监看进程（直接运行的）也一并结束
PIDS="$(/usr/sbin/lsof -nP -tiTCP:8799 -sTCP:LISTEN 2>/dev/null)"
[ -n "$PIDS" ] && kill $PIDS 2>/dev/null
sleep 1
if /usr/sbin/lsof -nP -iTCP:8799 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "监看中心仍在运行，请稍后重试。" >&2; exit 1
fi
echo "小八监看中心已关闭。"
