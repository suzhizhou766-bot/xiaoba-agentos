#!/bin/bash
# 小八 · Overlay Studio 启动：http://localhost:5177/ ；不开机自启，用 关闭.command 关
P=/Users/suzhizhou/Documents/私人助理/配置
DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="$P/node/bin:$P/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PUPPETEER_CACHE_DIR="$P/puppeteer" npm_config_cache="$P/npm-cache" BROWSER=none
mkdir -p "$DIR/logs"
if curl -s -o /dev/null --max-time 1 http://localhost:5177/; then
  echo "Overlay Studio 已在运行：http://localhost:5177/"
else
  if lsof -nP -iTCP:5177 -sTCP:LISTEN >/dev/null 2>&1; then echo "5177 端口被别的程序占用，未启动"; exit 1; fi
  cd "$DIR/motion-playground" || exit 1
  nohup node node_modules/vite/bin/vite.js > "$DIR/logs/server.log" 2>&1 &
  echo $! > "$DIR/logs/server.pid"
  for i in $(seq 1 30); do curl -s -o /dev/null --max-time 1 http://localhost:5177/ && break; sleep 0.5; done
  curl -s -o /dev/null --max-time 1 http://localhost:5177/ || { echo "启动失败，看 logs/server.log"; exit 1; }
  echo "已启动：http://localhost:5177/（日志 logs/server.log）"
fi
[ "$1" = "--no-open" ] || open "http://localhost:5177/"
