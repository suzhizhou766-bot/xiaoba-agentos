#!/bin/bash
# 取一个开源 Opus 5.5 风格放进当前项目（frame.md + style/ 参考图）。在项目根目录运行。
#   bash get_style.sh lemo --list          列出 lemo-opuscar 的全部风格
#   bash get_style.sh lemo <风格名>        例：lemo watercolor（CC BY 4.0，记得注明出处）
#   bash get_style.sh ovs kinetic-reel     opus-video-skills（MIT）
#   bash get_style.sh ovs painted-animation
#   bash get_style.sh cab                  ClaudeAnimationBase（MIT）
set -e
SRC=${1:?用法见脚本开头}; NAME=${2:-}
RAW=https://raw.githubusercontent.com
get() { curl -fsSL "$1" -o "$2"; }
prep() { rm -rf ./style; mkdir -p style; }   # 确认参数有效后才清空旧的 style/
case "$SRC" in
  lemo)
    if [ "$NAME" = "--list" ] || [ -z "$NAME" ]; then
      curl -fsSL https://api.github.com/repos/lemomo-ai/lemo-opuscar/contents/styles | python3 -c "import sys,json; print('\n'.join(x['name'] for x in json.load(sys.stdin) if x['type']=='dir' and not x['name'].startswith('_')))"; exit 0; fi
    prep; B=$RAW/lemomo-ai/lemo-opuscar/main/styles/$NAME
    get $B/STYLE.md frame.md
    get $B/DEMO.md style/DEMO.md || true
    get $B/poster.jpg style/poster.jpg || true
    printf '\n\n---\n来源：lemo-opuscar（lemomo-ai）styles/%s/STYLE.md，CC BY 4.0。成片发布时请注明出处。\n' "$NAME" >> frame.md ;;
  ovs)
    B=$RAW/tuzhechen2005/opus-video-skills/main/skills/$NAME
    case "$NAME" in
      kinetic-reel) prep; get $B/references/style-guide.md frame.md; get $B/references/sound.md style/sound.md || true ;;
      painted-animation) prep; get $B/template/ANIMATION_GUIDE.md frame.md; get $B/references/music-video.md style/music-video.md || true ;;
      *) echo "ovs 只有 kinetic-reel / painted-animation"; exit 1 ;;
    esac
    printf '\n\n---\n来源：opus-video-skills（tuzhechen2005）%s，MIT。\n' "$NAME" >> frame.md ;;
  cab)
    prep; get $RAW/JohnHeibel/ClaudeAnimationBase/main/ANIMATION_GUIDE.md frame.md
    printf '\n\n---\n来源：ClaudeAnimationBase（John Heibel）ANIMATION_GUIDE.md，MIT。\n' >> frame.md ;;
  *) echo "未知来源：$SRC（可选 lemo / ovs / cab）"; exit 1 ;;
esac
echo "已放好：frame.md（风格规范）$(ls style 2>/dev/null | sed 's/^/ style\//' | tr '\n' ' ')"
echo "注意：只借风格规范，不借引擎。规范里提到对方代码函数的地方，用本 skill 的 skia-canvas 引擎做出同样效果。"
