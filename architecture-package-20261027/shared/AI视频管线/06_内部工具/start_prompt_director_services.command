#!/bin/bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

BASE="/Users/suzhizhou/Documents/私人助理/AI视频管线/06_内部工具"
RUNTIME="/Users/suzhizhou/Documents/私人助理/.runtime"
LOG_DIR="$RUNTIME/logs"

mkdir -p "$LOG_DIR"

# 先启动控制器，使页面即使遇到单项依赖缺失也能显示真实原因。
if ! /usr/sbin/lsof -nP -iTCP:8893 -sTCP:LISTEN >/dev/null 2>&1; then
  nohup "$RUNTIME/venvs/core/bin/python" "$BASE/local_tools_control_server.py" > "$LOG_DIR/local-tools-control.log" 2>&1 &
fi

if ! /usr/sbin/lsof -nP -iTCP:8890 -sTCP:LISTEN >/dev/null 2>&1; then
  nohup "$RUNTIME/venvs/core/bin/python" "$BASE/prompt_reverse_server.py" > "$LOG_DIR/prompt-reverse.log" 2>&1 &
fi

if ! /usr/sbin/lsof -nP -iTCP:8891 -sTCP:LISTEN >/dev/null 2>&1; then
  nohup "$RUNTIME/venvs/depth/bin/python" "$BASE/depth_server.py" > "$LOG_DIR/depth-server.log" 2>&1 &
fi

if ! /usr/sbin/lsof -nP -iTCP:8892 -sTCP:LISTEN >/dev/null 2>&1; then
  nohup "$RUNTIME/venvs/superres/bin/python" "$BASE/super_resolution_server.py" > "$LOG_DIR/superres-server.log" 2>&1 &
fi

if ! /usr/sbin/lsof -nP -iTCP:9878 -sTCP:LISTEN >/dev/null 2>&1; then
  nohup "$RUNTIME/venvs/core/bin/python" "$BASE/../scripts/sfx_server.py" --host 127.0.0.1 --port 9878 > "$LOG_DIR/sfx-server.log" 2> "$LOG_DIR/sfx-server.err" &
fi

wait_health() {
  local label="$1" url="$2" deadline=$((SECONDS + 45)) response
  while (( SECONDS < deadline )); do
    if response="$(/usr/bin/curl -fsS --max-time 3 "$url" 2>/dev/null)"; then
      printf '%s %s\n' "$label" "$response"
      return 0
    fi
    sleep 1
  done
  echo "$label 启动超时：$url" >&2
  return 1
}

wait_health "本地控制器" http://127.0.0.1:8893/healthz
wait_health "提示词反推" http://127.0.0.1:8890/healthz
wait_health "深度图" http://127.0.0.1:8891/healthz
wait_health "超分" http://127.0.0.1:8892/healthz
wait_health "SFX" http://127.0.0.1:9878/readyz
