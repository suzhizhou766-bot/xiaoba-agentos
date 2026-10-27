#!/bin/bash
# Install or remove persistent user LaunchAgents for the five local media tools.
# This script changes only the five com.xiaoba.media.* agents and never touches routing.
set -euo pipefail

ROOT="${XIAOBA_ASSISTANT_ROOT:-$HOME/Documents/私人助理}"
BASE="$ROOT/AI视频管线/06_内部工具"
RUNTIME="$ROOT/.runtime"
LOG="$RUNTIME/logs"
AGENTS="$HOME/Library/LaunchAgents"
PY_CORE="$RUNTIME/venvs/core/bin/python"
PY_DEPTH="$RUNTIME/venvs/depth/bin/python"
PY_SUPER="$RUNTIME/venvs/superres/bin/python"

mkdir -p "$AGENTS" "$LOG"

install_one() {
  local label="$1" python="$2" script="$3" port="$4" plist="$AGENTS/$label.plist"
  shift 4
  python3 - "$plist" "$label" "$python" "$script" "$port" "$LOG" "$@" <<'PY'
import json, plistlib, sys
from pathlib import Path
plist, label, python, script, port, log = sys.argv[1:7]
extra=sys.argv[7:]
args=[python, script, *extra]
entry={
  'Label': label,
  'ProgramArguments': args,
  'WorkingDirectory': str(Path(script).parent),
  'RunAtLoad': True,
  'KeepAlive': {'SuccessfulExit': False},
  'ThrottleInterval': 10,
  'StandardOutPath': f'{log}/{label}.log',
  'StandardErrorPath': f'{log}/{label}.err',
  'ProcessType': 'Interactive',
}
Path(plist).write_bytes(plistlib.dumps(entry, fmt=plistlib.FMT_XML, sort_keys=False))
PY
  chmod 600 "$plist"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
}

case "${1:-install}" in
  install)
    install_one com.xiaoba.media.prompt-reverse "$PY_CORE" "$BASE/prompt_reverse_server.py" 8890
    install_one com.xiaoba.media.depth "$PY_DEPTH" "$BASE/depth_server.py" 8891
    install_one com.xiaoba.media.super-resolution "$PY_SUPER" "$BASE/super_resolution_server.py" 8892
    install_one com.xiaoba.media.local-control "$PY_CORE" "$BASE/local_tools_control_server.py" 8893
    install_one com.xiaoba.media.sfx "$PY_CORE" "$BASE/../scripts/sfx_server.py" 9878 --host 127.0.0.1 --port 9878
    echo '{"ok":true,"action":"install","agents":"com.xiaoba.media.*"}'
    ;;
  remove)
    for label in com.xiaoba.media.prompt-reverse com.xiaoba.media.depth com.xiaoba.media.super-resolution com.xiaoba.media.local-control com.xiaoba.media.sfx; do
      launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
      rm -f "$AGENTS/$label.plist"
    done
    echo '{"ok":true,"action":"remove","agents":"com.xiaoba.media.*"}'
    ;;
  status)
    for label in com.xiaoba.media.prompt-reverse com.xiaoba.media.depth com.xiaoba.media.super-resolution com.xiaoba.media.local-control com.xiaoba.media.sfx; do
      if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then echo "$label=loaded"; else echo "$label=not-loaded"; fi
    done
    ;;
  *) echo '{"ok":false,"error":"usage: install|remove|status"}' >&2; exit 2 ;;
esac
