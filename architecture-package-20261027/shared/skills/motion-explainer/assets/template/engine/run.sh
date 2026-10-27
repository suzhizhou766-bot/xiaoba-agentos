#!/bin/bash
# 用法（在项目根目录）：./engine/run.sh [并行进程数，默认 6]
# 需要：./film.mjs（export drawFrame, DURATION）和 ./music.wav（python3 score.py 生成）
# 输出：out/final.mp4（30fps，H.264 + AAC；竖版 1080×1920，横版 1920×1080，由 aspect.txt 或环境变量 ASPECT 决定）
set -e
cd "$(dirname "$0")/.."
P=${1:-6}
echo "画幅：$(node --input-type=module -e "const m = await import('./engine/lib.mjs'); console.log(m.W + '×' + m.H)")"
DUR=$(node --input-type=module -e "const m = await import('./film.mjs'); console.log(m.DURATION)")
TOTAL=$(python3 -c "print(int(round($DUR*30)))")
STEP=$(( (TOTAL + P - 1) / P ))
mkdir -p out/parts; rm -f out/parts/*.mp4 out/parts/list.txt
for i in $(seq 0 $((P-1))); do
  A=$((i*STEP)); B=$(( (i+1)*STEP )); [ $B -gt $TOTAL ] && B=$TOTAL
  [ $A -ge $TOTAL ] && break
  node engine/render.mjs $A $B out/parts/p$i.mp4 2> out/parts/log$i.txt &
  echo "file 'p$i.mp4'" >> out/parts/list.txt
done
wait
ffmpeg -hide_banner -loglevel error -y -f concat -safe 0 -i out/parts/list.txt -c copy out/video_only.mp4
ffmpeg -hide_banner -loglevel error -y -i out/video_only.mp4 -i music.wav -c:v copy -c:a aac -b:a 256k -ar 48000 -shortest -movflags +faststart out/final.mp4
ffprobe -hide_banner -v error -show_entries format=duration:stream=codec_name,width,height,r_frame_rate -of compact out/final.mp4
# 导出文字时间表给检查脚本用（film.mjs 里 export TIMELINE 时）
node --input-type=module -e "const m = await import('./film.mjs'); if (m.TIMELINE) console.log(JSON.stringify(m.TIMELINE))" > out/timeline.json || true
