#!/bin/bash
# 交付前体检：./checks/check_all.sh [out/final.mp4]
# FAIL 必须修；WARN 用 sheet.mjs 看一眼那个时刻再决定。
cd "$(dirname "$0")/.."
V=${1:-out/final.mp4}
echo "== 规格"; ffprobe -v error -show_entries stream=codec_type,codec_name,width,height,r_frame_rate -show_entries format=duration -of compact "$V"
echo "== 响度（目标 -14 LUFS ±1，峰值 ≤ -1 dBTP）"
ffmpeg -hide_banner -nostats -i "$V" -af ebur128=peak=true -f null - 2>&1 | grep -E "^\s+(I:|Peak:)" | tail -2
echo "== 画面"; python3 checks/vstats.py "$V" --json out/vstats.json
if [ -s out/timeline.json ]; then echo "== 读字时间"; python3 checks/readtime.py out/timeline.json | tail -15; else echo "== 读字时间：没有 out/timeline.json（film.mjs 要 export TIMELINE）"; fi
echo "== 字叠字 / 半截字"; node checks/textcheck.mjs 0.2 2>&1 | tail -3
echo "== 数字比例"; node checks/ratiocheck.mjs 2>&1 | tail -10
