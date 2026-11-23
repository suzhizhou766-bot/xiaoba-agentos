#!/bin/bash
# 开启小八监看中心（运行总览 / 裁决中心 / 导演台），并在浏览器里打开。
# 带看护：监看中心意外退出或不响应时会自动重启；它管理的裁决后端也一并拉起和看护。
# 已在运行时只打开页面。关闭：双击同目录的“关闭监看中心.command”。不会开机自启。
# 监管身份与关闭脚本共用 --classify-supervisor-pid（原生 argv + claim），不用 ps 空格拆参。
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14"  # 统一解释器（2026-10-22 融合迭代第 2 步）
# 端口优先级：测试环境变量 XIAOBA_MONITOR_PORT > 同目录 monitor.json > 默认 8799。
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
  # 与入口同一判定。EPERM/读不到命令 → 非 match，不得当成已监管。
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
    echo "启动失败：监看服务仍可响应，但监管进程未运行或身份不匹配（端口有人听不能证明归属）。请先关闭监看中心再重新启动。" >&2
  else
    echo "启动失败。日志：$DIR/logs/supervisor.out.log 与 $DIR/logs/monitor.log" >&2
  fi
  exit 1
fi
