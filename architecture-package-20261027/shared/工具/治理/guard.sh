#!/bin/sh
# 小八共用守卫入口。统一解释器：只用 配置/ 里登记的 Python。
# 解释器或 guard.py 缺失、或 guard.py 异常退出时一律拒绝（fail-closed），不静默放行。
# 拒绝时 stdout 给 Antigravity 的 {"decision":"deny"}，stderr + 退出码 2 给 Claude Code、Codex、Qoder。
PY="/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14"
G="$(dirname "$0")/guard.py"
deny() {
  printf '{"decision": "deny", "reason": "小八守卫：%s"}\n' "$1"
  echo "小八守卫：$1" >&2
  exit 2
}
[ -x "$PY" ] || deny "统一解释器缺失，守卫无法运行，已拒绝"
[ -f "$G" ] || deny "guard.py 缺失，守卫无法运行，已拒绝"
"$PY" -B "$G" "$@"
rc=$?
case $rc in
  0|2|3) exit $rc ;;
  *) deny "守卫内部错误（退出码 $rc），已拒绝" ;;
esac
