#!/bin/bash
# 新建一支片子的项目：bash <skill>/scripts/new_project.sh <项目目录> [9:16|16:9]
# 第二个参数是画幅，默认 9:16 竖版；16:9 是横版。写进项目根目录的 aspect.txt，引擎、拼板、体检都读它。
# 复制引擎/体检/小样片，写 aspect.txt，装 skia-canvas。
set -e
SK="$(cd "$(dirname "$0")/.." && pwd)"; DIR="${1:?用法: new_project.sh <项目目录> [9:16|16:9]}"; ASPECT="${2:-9:16}"
case "$ASPECT" in 9:16|16:9) ;; *) echo "画幅只能是 9:16 或 16:9"; exit 1 ;; esac
mkdir -p "$DIR" && cd "$DIR"
cp -R "$SK/assets/template/"* .
cp "$SK/references/director.md" AGENTS.md
echo "$ASPECT" > aspect.txt
[ -f package.json ] || printf '{\n  "name": "film",\n  "private": true,\n  "type": "module"\n}\n' > package.json
[ -d node_modules/skia-canvas ] || npm install --silent skia-canvas
echo "项目已就绪：$DIR（画幅 $ASPECT）"
