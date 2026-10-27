#!/bin/zsh
set -eu
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$HOME/Documents/私人助理/.runtime/node/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
mkdir -p out
output_path="out/hello-world-$(date +%Y%m%d-%H%M%S)-$$.mp4"
npm run render:demo -- "$output_path" --codec=h264
print "导出完成：$PWD/$output_path"
open "$output_path"
