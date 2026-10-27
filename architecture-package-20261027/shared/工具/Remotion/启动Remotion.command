#!/bin/zsh
set -eu
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$HOME/Documents/私人助理/.runtime/node/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [[ ! -x node_modules/.bin/remotion ]]; then
  print -u2 'Remotion 依赖缺失，请在本目录运行 npm ci。'
  exit 1
fi
print 'Remotion Studio：http://localhost:3330'
print '关闭时在此终端按 Control+C。'
exec npm run start -- "$@"
