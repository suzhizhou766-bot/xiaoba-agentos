#!/bin/bash
# 小八 · Overlay Studio 关闭：只结束本工具启动的那个服务进程
DIR="$(cd "$(dirname "$0")" && pwd)"
PIDF="$DIR/logs/server.pid"
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  kill "$(cat "$PIDF")"; rm -f "$PIDF"; echo "Overlay Studio 已关闭"
else
  rm -f "$PIDF"
  PID=$(lsof -nP -tiTCP:5177 -sTCP:LISTEN 2>/dev/null)
  if [ -n "$PID" ] && ps -o command= -p "$PID" | grep -q "vite"; then kill "$PID"; echo "Overlay Studio 已关闭（按端口找到）"; else echo "Overlay Studio 没在运行"; fi
fi
