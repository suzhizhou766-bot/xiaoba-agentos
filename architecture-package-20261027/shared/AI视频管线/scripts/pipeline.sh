#!/bin/bash
# 小八视频管线：严格检查 -> 渲染 -> 混音 -> 技术 QC -> 内容预检 -> 追溯清单
# 两种用法：
#   Remotion 渲染：pipeline.sh <项目名> <04_合成项目/入口.tsx> [--composition ID] [--bgm FILE] [--sfx FILE] [--version v001] [--dry-run]
#   外部成片：    pipeline.sh <项目名> --video <画面或成片> --premix <混好的音轨> [--attach FILE]... [--version v001] [--dry-run]
#                 （达芬奇等外部工具剪好、混好的片子：跳过 Remotion 渲染与 BGM/SFX 预混，其余验收步骤照走）
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ROOT="$(cd "$PROJECT_ROOT/../.runtime" && pwd)"
export PATH="$RUNTIME_ROOT/bin:$RUNTIME_ROOT/node/bin:$RUNTIME_ROOT/venvs/core/bin:$PATH"
export REMOTION_BROWSER_PATH="${REMOTION_BROWSER_PATH:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
export XIAOBA_REMOTION_ROOT="${XIAOBA_REMOTION_ROOT:-$RUNTIME_ROOT/remotion}"

PROJECT_NAME="${1:?用法: pipeline.sh <项目名> <合成模板.tsx> [--version v001] [--bgm FILE] [--sfx FILE] [--dry-run]；或 pipeline.sh <项目名> --video FILE --premix FILE [--attach FILE]...}"
shift
TEMPLATE_FILE=""
if [[ $# -gt 0 && "$1" != --* ]]; then
    TEMPLATE_FILE="$1"
    shift
fi

BGM_FILE=""
SFX_FILE=""
VERSION="v001"
COMPOSITION="RealEstateFull"
DRY_RUN=0
VIDEO_IN=""
PREMIX_IN=""
ATTACH_FILES=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --video) VIDEO_IN="${2:?--video 缺文件}"; shift 2 ;;
        --premix) PREMIX_IN="${2:?--premix 缺文件}"; shift 2 ;;
        --attach) ATTACH_FILES+=("${2:?--attach 缺文件}"); shift 2 ;;
        --version) VERSION="${2:?--version 缺版本号}"; shift 2 ;;
        --bgm) BGM_FILE="${2:?--bgm 缺文件}"; shift 2 ;;
        --sfx) SFX_FILE="${2:?--sfx 缺文件}"; shift 2 ;;
        --composition) COMPOSITION="${2:?--composition 缺 ID}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

# 项目名只作为单一目录名使用；保留中文，但拒绝路径穿越、绝对路径和控制字符。
if [[ -z "$PROJECT_NAME" || "$PROJECT_NAME" == -* || "$PROJECT_NAME" == "." || "$PROJECT_NAME" == ".." || "$PROJECT_NAME" == */* || "$PROJECT_NAME" == *\\* || "$PROJECT_NAME" == *$'\n'* || "$PROJECT_NAME" == *$'\r'* || "$PROJECT_NAME" == *$'\t'* ]]; then
    echo "项目名不是安全目录名：$PROJECT_NAME" >&2
    exit 2
fi
if [[ ! "$VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "版本号必须匹配 [A-Za-z0-9][A-Za-z0-9._-]*：$VERSION" >&2
    exit 2
fi

abs_path() { printf '%s/%s' "$(cd "$(dirname "$1")" && pwd)" "$(basename "$1")"; }
EXTERNAL=0
TEMPLATE_ENTRY=""
if [[ -n "$VIDEO_IN" ]]; then
    # 外部成片：画面已由外部工具做好，不再渲染、不再用 BGM/SFX 预混
    EXTERNAL=1
    [[ -z "$TEMPLATE_FILE" ]] || { echo "外部成片模式不需要合成模板：$TEMPLATE_FILE" >&2; exit 2; }
    [[ -z "$BGM_FILE" && -z "$SFX_FILE" ]] || { echo "外部成片模式用 --premix 提供混好的音轨，不能再给 --bgm/--sfx" >&2; exit 2; }
    [[ -f "$VIDEO_IN" ]] || { echo "画面不存在: $VIDEO_IN" >&2; exit 2; }
    [[ -z "$PREMIX_IN" || -f "$PREMIX_IN" ]] || { echo "预混音轨不存在: $PREMIX_IN" >&2; exit 2; }
    VIDEO_IN="$(abs_path "$VIDEO_IN")"
    if [[ -n "$PREMIX_IN" ]]; then PREMIX_IN="$(abs_path "$PREMIX_IN")"; fi
else
    [[ -n "$TEMPLATE_FILE" ]] || { echo "缺少合成模板；外部成片请用 --video" >&2; exit 2; }
    [[ -z "$PREMIX_IN" ]] || { echo "--premix 只用于外部成片（--video）" >&2; exit 2; }
    [[ -f "$TEMPLATE_FILE" ]] || { echo "模板不存在: $TEMPLATE_FILE" >&2; exit 2; }
    [[ -z "$BGM_FILE" || -f "$BGM_FILE" ]] || { echo "BGM不存在: $BGM_FILE" >&2; exit 2; }
    [[ -z "$SFX_FILE" || -f "$SFX_FILE" ]] || { echo "SFX不存在: $SFX_FILE" >&2; exit 2; }

    TEMPLATE_FILE="$(abs_path "$TEMPLATE_FILE")"
    if [[ -n "$BGM_FILE" ]]; then BGM_FILE="$(abs_path "$BGM_FILE")"; fi
    if [[ -n "$SFX_FILE" ]]; then SFX_FILE="$(abs_path "$SFX_FILE")"; fi

    # Remotion entrypoint must live in the composition project.
    case "$TEMPLATE_FILE" in
        "$PROJECT_ROOT/04_合成项目/"*) TEMPLATE_ENTRY="$TEMPLATE_FILE" ;;
        *) echo "Remotion 入口必须位于 04_合成项目/ 内：$PROJECT_ROOT/04_合成项目" >&2; exit 2 ;;
    esac
fi
ATTACH_ABS=()
for f in ${ATTACH_FILES[@]+"${ATTACH_FILES[@]}"}; do
    [[ -f "$f" ]] || { echo "附件不存在: $f" >&2; exit 2; }
    ATTACH_ABS+=("$(abs_path "$f")")
done

for tool in ffmpeg ffprobe python3; do
    command -v "$tool" >/dev/null || { echo "缺少统一运行时依赖: $tool" >&2; exit 2; }
done
if [[ "$EXTERNAL" -eq 0 ]]; then
    [[ -x "$RUNTIME_ROOT/node/bin/node" ]] || { echo "缺少统一 Node.js: $RUNTIME_ROOT/node/bin/node" >&2; exit 2; }
fi

DELIVERY_ROOT="$PROJECT_ROOT/05_输出/交付"
OUTPUT_DIR="$DELIVERY_ROOT/$PROJECT_NAME/$VERSION"
WORK_DIR="$RUNTIME_ROOT/work/$PROJECT_NAME/$VERSION"
LOCK_ROOT="$RUNTIME_ROOT/locks"
LOCK_DIR="$LOCK_ROOT/pipeline-$PROJECT_NAME-$VERSION.lock"
mkdir -p "$WORK_DIR" "$LOCK_ROOT"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "同一项目版本正在运行或锁未清理：$PROJECT_NAME/$VERSION ($LOCK_DIR)" >&2
    exit 3
fi
printf 'pid=%s\nstarted_at=%s\n' "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$LOCK_DIR/owner"
cleanup() { rm -rf "$LOCK_DIR"; }
trap cleanup EXIT

if [[ -d "$OUTPUT_DIR" ]] && find "$OUTPUT_DIR" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo "交付版本已存在且非空：${OUTPUT_DIR}；请使用新的 --version，禁止覆盖正式交付。" >&2
    exit 3
fi

cd "$PROJECT_ROOT"
echo "═══════════════════════════════════════"
echo "🎬 AI 视频管线: $PROJECT_NAME/$VERSION"
echo "═══════════════════════════════════════"

echo ""
if [[ "$EXTERNAL" -eq 1 ]]; then
    echo "📌 Gate 0/5: 外部成片检查..."
    VIDEO_CODEC="$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of default=nw=1:nk=1 "$VIDEO_IN")"
    [[ -n "$VIDEO_CODEC" ]] || { echo "画面里没有可读的视频流：$VIDEO_IN" >&2; exit 2; }
    if [[ -n "$PREMIX_IN" ]]; then
        [[ -n "$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of default=nw=1:nk=1 "$PREMIX_IN")" ]] \
            || { echo "预混文件里没有可读的音频流：$PREMIX_IN" >&2; exit 2; }
    fi
    echo "画面：$VIDEO_IN（$VIDEO_CODEC）"
    echo "预混：${PREMIX_IN:-无}"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "[dry-run] 外部成片：跳过 Remotion 渲染；附件 ${#ATTACH_ABS[@]} 个"
        exit 0
    fi
else
    echo "📌 Gate 0/5: Remotion 入口检查..."
    [[ "$TEMPLATE_ENTRY" == *.tsx || "$TEMPLATE_ENTRY" == *.ts ]] || { echo "Remotion 入口必须是 .tsx/.ts：$TEMPLATE_ENTRY" >&2; exit 2; }
    [[ -d "$XIAOBA_REMOTION_ROOT/node_modules" ]] || { echo "缺少统一 Remotion 运行时：$XIAOBA_REMOTION_ROOT" >&2; exit 2; }

    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "[dry-run] node scripts/remotion_render.mjs $TEMPLATE_ENTRY $COMPOSITION $WORK_DIR/${PROJECT_NAME}_渲染.mp4 --concurrency=1"
        exit 0
    fi
fi

mkdir -p "$OUTPUT_DIR"
echo ""
if [[ "$EXTERNAL" -eq 1 ]]; then
    VIDEO_OUT="$WORK_DIR/${PROJECT_NAME}_外部画面.mp4"
    echo "📌 Step 1/5: 接收外部画面（不渲染）..."
    if [[ "$VIDEO_CODEC" == "h264" ]]; then
        ffmpeg -y -v error -i "$VIDEO_IN" -map 0:v:0 -c:v copy -an "$VIDEO_OUT"
    else
        # ProRes 等不适合直接放进 MP4：高质量转成 H.264，保留 BT.709 标记
        ffmpeg -y -v error -i "$VIDEO_IN" -map 0:v:0 -c:v libx264 -preset slow -crf 14 -pix_fmt yuv420p \
            -color_primaries bt709 -color_trc bt709 -colorspace bt709 -an "$VIDEO_OUT"
    fi
else
    VIDEO_OUT="$WORK_DIR/${PROJECT_NAME}_渲染.mp4"
    echo "📌 Step 1/5: Remotion 视频合成..."
    node "$PROJECT_ROOT/scripts/remotion_render.mjs" "$TEMPLATE_ENTRY" "$COMPOSITION" "$VIDEO_OUT" --concurrency=1
fi
[[ -s "$VIDEO_OUT" ]] || { echo "没有生成有效视频：$VIDEO_OUT" >&2; exit 1; }
echo "✅ 视频输出: $VIDEO_OUT"

echo ""
echo "📌 Step 2/5: 音频处理..."
FINAL_VIDEO="$OUTPUT_DIR/${PROJECT_NAME}_正式成片.mp4"
FINAL_TMP="$OUTPUT_DIR/.${PROJECT_NAME}_${VERSION}_正式成片.tmp.mp4"
PREMIX="$WORK_DIR/${PROJECT_NAME}_预混_48k.wav"
VIDEO_DURATION="$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$VIDEO_OUT")"
EXPECTED_AUDIO=0
rm -f "$PREMIX"
if [[ -n "$PREMIX_IN" ]]; then
    # 外部混好的音轨：只统一成 48 kHz 双声道，不再改动混音
    EXPECTED_AUDIO=1
    ffmpeg -y -v error -i "$PREMIX_IN" -map 0:a:0 -vn -ar 48000 -ac 2 "$PREMIX"
elif [[ -n "$BGM_FILE" || -n "$SFX_FILE" ]]; then
    EXPECTED_AUDIO=1
    AUDIO_ARGS=()
    [[ -z "$BGM_FILE" ]] || AUDIO_ARGS+=(--bgm "$BGM_FILE")
    [[ -z "$SFX_FILE" ]] || AUDIO_ARGS+=(--sfx "$SFX_FILE")
    python3 "$PROJECT_ROOT/scripts/audio_assets.py" "${AUDIO_ARGS[@]}" \
      --duration "$VIDEO_DURATION" --work "$WORK_DIR/audio" --delivery "$OUTPUT_DIR"
    cp "$WORK_DIR/audio/premix_48k.wav" "$PREMIX"
fi

if [[ -f "$PREMIX" ]]; then
    # Keep the video duration authoritative: short BGM/SFX is padded with
    # silence and long audio is trimmed, so muxing never truncates the picture.
    PADDED_PREMIX="$WORK_DIR/${PROJECT_NAME}_预混_对齐_48k.wav"
    ffmpeg -y -i "$PREMIX" -af "apad" -t "$VIDEO_DURATION" -ar 48000 -ac 2 "$PADDED_PREMIX"
    mv "$PADDED_PREMIX" "$PREMIX"
    python3 "$PROJECT_ROOT/../skills/bgm/scripts/audio_qc.py" "$PREMIX"
    ffmpeg -y -i "$VIDEO_OUT" -i "$PREMIX" -map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -b:a 256k -t "$VIDEO_DURATION" "$FINAL_TMP"
else
    cp "$VIDEO_OUT" "$FINAL_TMP"
    echo "ℹ️ 未提供 BGM/SFX，保留无额外混音成片"
fi
mv "$FINAL_TMP" "$FINAL_VIDEO"

echo ""
echo "📌 Step 3/5: 技术验收..."
TECHNICAL_QC="$OUTPUT_DIR/${PROJECT_NAME}_技术验收.json"
python3 "$PROJECT_ROOT/scripts/technical_qc.py" "$FINAL_VIDEO" --output "$TECHNICAL_QC" --expect-audio "$EXPECTED_AUDIO"

echo ""
echo "📌 Step 4/5: 机器内容预检（不替代用户验收）..."
CONTENT_QC="$OUTPUT_DIR/${PROJECT_NAME}_内容预检.json"
python3 "$PROJECT_ROOT/scripts/video_content_qc.py" "$FINAL_VIDEO" --output "$CONTENT_QC" --expect-audio "$EXPECTED_AUDIO"

echo ""
echo "📌 Step 5/5: 交付元数据与资产清单..."
METADATA="$OUTPUT_DIR/${PROJECT_NAME}_交付元数据.json"
sha256_file() { shasum -a 256 "$1" | awk '{print $1}'; }
TEMPLATE_SHA=""
[[ -z "$TEMPLATE_FILE" ]] || TEMPLATE_SHA="$(sha256_file "$TEMPLATE_FILE")"
for f in ${ATTACH_ABS[@]+"${ATTACH_ABS[@]}"}; do
    dest="$OUTPUT_DIR/$(basename "$f")"
    [[ ! -e "$dest" ]] || { echo "附件与交付文件重名，不覆盖：$dest" >&2; exit 3; }
    cp "$f" "$dest"
done
ATTACH_LIST="$(printf '%s\n' ${ATTACH_ABS[@]+"${ATTACH_ABS[@]}"})"
GIT_REV="$(git -C "$PROJECT_ROOT" rev-parse --short HEAD 2>/dev/null || printf 'not-a-git-worktree')"
python3 - "$METADATA" "$PROJECT_NAME" "$VERSION" "$PROJECT_ROOT" "$TEMPLATE_ENTRY" "$TEMPLATE_SHA" "$GIT_REV" "$EXPECTED_AUDIO" "$BGM_FILE" "$SFX_FILE" "$FINAL_VIDEO" "$COMPOSITION" "$VIDEO_IN" "$PREMIX_IN" "$ATTACH_LIST" <<'PY'
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

(out, project, version, root, template, template_sha, git_rev, expected_audio,
 bgm, sfx, final_video, composition, video_in, premix_in, attach_list) = sys.argv[1:]
def file_info(value):
    if not value:
        return None
    path = Path(value)
    exists = path.is_file()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if exists else None, "bytes": path.stat().st_size if exists else None}
payload = {
    "schema_version": 1,
    "provenance_status": "technical_pass_pending_user_acceptance",
    "acceptance": {"technical_pass": True, "machine_content_precheck_pass": True, "user_accepted": False, "user_acceptance_required": True, "accepted_by": None, "accepted_at": None, "notes": None},
    "project": project,
    "version": version,
    "created_at": datetime.now(timezone.utc).isoformat(),
    "source": {"project_root": root, "template": template, "template_sha256": template_sha, "git_revision": git_rev},
    "inputs": {"bgm": file_info(bgm), "sfx": file_info(sfx), "external_video": file_info(video_in), "premix": file_info(premix_in),
               "attachments": [file_info(a) for a in attach_list.splitlines() if a]},
    "render": ({"engine": "external", "source": video_in, "expected_audio": bool(int(expected_audio)), "output": final_video} if video_in else
               {"engine": "Remotion", "composition": composition, "expected_audio": bool(int(expected_audio)), "output": final_video}),
}
Path(out).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
python3 "$PROJECT_ROOT/../skills/assets/scripts/asset_manifest.py" "$OUTPUT_DIR" "$OUTPUT_DIR/asset_manifest.csv"

FINAL_SIZE=$(ls -lh "$FINAL_VIDEO" | awk '{print $5}')
echo "✅ 成片: $FINAL_VIDEO ($FINAL_SIZE)"
echo "✅ 交付包: $OUTPUT_DIR"
ls -lh "$OUTPUT_DIR"
echo "═══════════════════════════════════════"
