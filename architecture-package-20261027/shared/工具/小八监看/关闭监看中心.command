#!/bin/bash
# 关闭小八监看中心：公开入口走 --close-report（B11）。
# - 停止结果与端口探测分离；closed_ok 才算对外成功（不能仅凭退出码/文案）。
# - 信号目标仍只来自 lifecycle claim（stop_owned）；旧裸 PID 须先 --preflight-legacy 再 --migration-stop。
# - lsof 失败 → port_probe=unobservable，不得误报已关闭或误认占端。
# 发版后置同步须带上 monitor_core/lifecycle.py 与本入口（不得另增第二部署模块）。
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14"
RESOLVE_ARGS=()
if [ -n "${XIAOBA_MONITOR_PORT:-}" ]; then
  RESOLVE_ARGS=(--port "$XIAOBA_MONITOR_PORT")
fi
if ! PORT="$("$PY" -B "$DIR/xiaoba_monitor.py" --resolve-port "${RESOLVE_ARGS[@]}")"; then
  echo "关闭失败：读不到可用的监听端口（检查同目录 monitor.json 或 XIAOBA_MONITOR_PORT）。" >&2
  exit 1
fi
# close-report：stdout 先打 JSON，再打成功用户消息；失败用户消息在 stderr。
# XIAOBA_LSOF 由 probe_port_status 读取，供诊断替身。
export XIAOBA_LSOF="${XIAOBA_LSOF:-/usr/sbin/lsof}"
"$PY" -B "$DIR/xiaoba_monitor.py" --close-report --port "$PORT"
exit $?
