#!/bin/bash
# 开启小八监看中心（运行总览 / 裁决中心 / 导演台），并在浏览器里打开。
# 带看护：监看中心意外退出或不响应时会自动重启；它管理的裁决后端也一并拉起和看护。
# 已在运行时只打开页面。关闭：双击同目录的“关闭监看中心.command”。不会开机自启。
DIR="$(cd "$(dirname "$0")" && pwd)"
URL="http://127.0.0.1:8799/"
ready() { /usr/bin/curl -fsS --max-time 2 "${URL}healthz" >/dev/null 2>&1; }
if ! ready; then
  mkdir -p "$DIR/logs"
  cd "$DIR" || exit 1
  nohup /usr/bin/python3 "$DIR/xiaoba_monitor.py" --supervise >> "$DIR/logs/supervisor.out.log" 2>&1 &
  for _ in $(seq 1 40); do ready && break; sleep 0.5; done
fi
if ready; then
  echo "小八监看中心已开启：$URL"
  echo "（裁决后端首次启动要做发布校验与第二大脑同步，约需几十秒，期间裁决页会提示“正在启动”。）"
  /usr/bin/open "$URL"
else
  echo "启动失败。日志：$DIR/logs/supervisor.out.log 与 $DIR/logs/monitor.log" >&2
  exit 1
fi
