# 🎬 AI 视频自动化管线

> 文案/模板 → 视频合成 → 可选 BGM/SFX 混音 → 技术 QC → 资产清单 → 最终成片

运行时统一放在 `/Users/suzhizhou/Documents/私人助理/.runtime/`；Remotion 在 `.runtime/remotion/`，服务日志在 `.runtime/logs/`，渲染中间文件在 `.runtime/work/`，不会写入正式交付目录。

交付目录规则见 [`05_输出/README.md`](05_输出/README.md)：正式文件按 `<项目名>/<版本>` 分层保存，版本目录不可覆盖。

## 工具链

| 环节 | 工具 | 状态 |
|------|------|------|
| 📝 文案 | 手动 / AI 生成 | ✅ |
| 🎙️ 旁白/TTS | 已从现行管线移除 | — |
| 🎬 视频合成 | **Remotion**（统一运行时） | ✅ |
| 🔧 音频/SFX | ffmpeg + SFX 匹配引擎 (:9878) | ✅ |

## 快速开始

```bash
# 1. 加载统一运行时（普通渲染不需要加载 API 密钥）
export PATH="../.runtime/bin:../.runtime/node/bin:$PATH"

# 路由由小八4.0主动双线控制：先运行 `xiaoba status`；仅小八线经过 Headroom(:8787)
source ../.runtime/shell/xiaoba-env.zsh

# 2. Remotion 渲染（推荐用脚本，支持多线程）
PATH="../.runtime/node/bin:$PATH" ../.runtime/node/bin/node scripts/remotion_render.mjs 04_合成项目/remotion_entry.tsx RealEstateFull ../.runtime/work/remotion/output.mp4

# 3. 小八全管线一键（无音轨，交付目录统一为 05_输出/交付/<项目名>/）
bash scripts/pipeline.sh 项目名 04_合成项目/remotion_entry.tsx --composition RealEstateFull --version v001

# 带BGM与SFX：输出48k预混、正式成片、技术验收JSON和资产清单
bash scripts/pipeline.sh 项目名 04_合成项目/remotion_entry.tsx --composition RealEstateFull --version v002 \
  --bgm /path/to/bgm.wav --sfx /path/to/sfx.wav

# 只验证输入与命令，不渲染
bash scripts/pipeline.sh 项目名 04_合成项目/remotion_entry.tsx --composition RealEstateFull --version v003 --dry-run

# 外部成片（达芬奇等剪好、混好）：跳过 Remotion 渲染和 BGM/SFX 预混，其余验收照走
# --video 可以是 H.264 成片（原样复制视频流）或 ProRes 等母版（高质量转成 H.264，保留 BT.709 标记）
# --premix 是混好的总轨，WAV 或带音轨的视频都可以；--attach 可重复，把分轨、报告等一起放进交付目录（不覆盖）
bash scripts/pipeline.sh 项目名 --video /path/to/picture_or_final.mov --premix /path/to/premix_48k.wav \
  --attach /path/to/项目名_BGM_48k.wav --attach /path/to/项目名_SFX_48k.wav --version v004
```

## 踩坑记录

| # | 问题 | 解决 |
|---|------|------|
| 1 | Chrome 下载受限 | Remotion 固定使用系统 Chrome：`REMOTION_BROWSER_PATH` |
| 2 | Remotion CLI 多线程 localhost 连接失败 | 用 Node.js API 脚本，并发≤3 稳定 |
| 3 | 依赖散落在项目目录 | Node、Remotion 和 Python 依赖统一放在 `.runtime/` |
| 4 | 中文字体缺失或渲染时联网下载 | 仅使用 macOS 本地字体，不在渲染时下载依赖 |

## 渲染标准

Remotion React/TSX 是唯一视频合成与交付入口；预览使用 `remotion studio`，正式渲染使用 `scripts/pipeline.sh`。

## 目录

```
AI视频管线/
├── 01_文案/              ← 文案输入
├── 04_合成项目/          ← 模板
│   └── remotion_entry.tsx          ← 唯一 Remotion 入口
├── 05_输出/
│   └── 交付/<项目名>/<版本>/ ← 正式成片、技术验收、机器内容预检、元数据、asset_manifest.csv
├── 06_内部工具/          ← 导演台/深度图/反推/试听工具
├── 视觉知识库/           ← 镜头/光影/动态/提示词库
├── 音效指纹库_v3.csv     ← 全素材库的有效 SFX 指纹（由扫描器增量维护）
├── scripts/
│   ├── pipeline.sh                 ← 小八渲染/混音/QC/清单全管线
│   ├── remotion_render.mjs         ← Remotion API 渲染
│   ├── sfx_match.py                 ← SFX 匹配
│   ├── sfx_server.py               ← SFX 常驻服务 :9878
│   ├── scan_classify_v3.py         ← 全素材库增量指纹扫描与自动分类
│   ├── sfx_catalog.py             ← 全库索引、偏好统计与精选候选
│   ├── sfx_register.py            ← ElevenLabs 下载音效登记与刷新
│   └── modular_blocks.js           ← 模块化活动快剪
└── README.md
```
