---
name: origincut
description: 在 OriginCut（AI 原生视频剪辑工作区）里剪片、写程序化音频组件，或为 OriginCut 项目准备素材时使用。
---

# OriginCut

OriginCut 是本机安装的 AI 原生视频剪辑工作区（文档对应 0.1.82）。它和别的工具方向相反：**OriginCut 是宿主，由它启动 Agent**（Codex、Claude Code、OpenCode、WorkBuddy），Agent 在 OriginCut 提供的工具里剪辑。它没有公开的 CLI 或 MCP，OriginCut 之外的 Agent 不能直接驱动它。

资料：https://origincut.com/ja/docs （项目、AI 助手设置、导出各有子页）。本机用法以实际项目为准，2026-10-22 盘点记录见 `工作区/小八融合迭代-20261022/`。

## 开工前

1. 视觉判断（画面、节奏、字幕、调色）先读审美手册 `审美提升/审美手册.md`。
2. 检索 Playbook 里与视频、剪辑、音效相关的踩坑和打法：`Playbook/踩坑/`、`Playbook/打法/`。
3. 先做只读分析：分析素材、提出两个方向，**暂不修改时间线**，等用户选定再动手。这也是 OriginCut 官方建议的第一条请求。

## 在 OriginCut 里工作（Agent 由 OriginCut 启动时）

- 项目位置：`私人助理/OriginCut/`。当前顶层目录本身就是一个项目（含 `.origincut/`、`素材/`、`recipes/`、`components/`、`audio-components/`、`OriginCut Audio/`）。
- 本机记录里出现过的工具：`origincut.resource.read`、`look`、`edit`、`sequence.create`、`command.resolve`、`audio.scaffold`、`audio.synthesize`。改时间线前先读最新时间线，改完核对轨道和版本。
- 出现批准界面时，核对具体操作再请用户确认；不替用户点批准。
- 程序化音频组件放在 `audio-components/<组件名>/`：`component.json`（`kind: origin-procedural-audio`、`apiVersion: 2`、`entry: src/main.ts`）+ `src/main.ts`，用 `origin-audio` 的 `defineSound`。类型定义在 `.origincut/sdk/audio/<runtimeIdentity>/index.d.ts`，只读参考。现成音效优先从 `音效素材库/` 找，按 SFX 模块规则采用。

## 配方库与组件（官方说明，2026-10-22 查）

- 首页功能第 12 项「レシピとコンポーネント」：可以搜索、预览表现效果的配方，按内容调整后使用；做好的部件可以复用。动画在时间线上是独立组件，与视频、字幕并排。
- 文档「常用制作流程」：在配方库里看作例，然后指定素材、要替换的文字、要保留的氛围；应用后检查时长、画面比例、文字量和与素材是否匹配。官方示例：“用这个配方做咖啡视频，把标题改成 MORNING RITUAL”。
- 动效范围：动态文字、图表、粒子、3D 场景，Agent 用代码生成；可做姿态与空间追踪，让图形跟随画面里的人或物。
- 可引用某个动画或参考图做局部修改，例如“把这张图的配色用到这个动态标题上，文字和时长不变”。
- **本机实测（2026-10-22，用户在 OriginCut 里做了一次）**：
  - 概念：配方是一套可复用的动效方案；组件是其中能放进时间线的具体动效。都主要由代码制作，参数、时长和预览示例一起保存。
  - 存放：都在项目目录里。导入的云端配方在 `recipes/<配方id>-v<版本>/`（含 `meta.json`、`LICENSE`、源码、字体、示例，例：`recipes/rcp_signal_error-v0-2-1`，MIT）；自制画面组件在 `components/<组件名>/`；声音组件在 `audio-components/<组件名>/`。
  - 组件结构：`src/main.js` 或 `main.ts` 是源码；`component.json` 存参数、时长、入口、`discovery`（`summaryEn`、`avoidWhen`、预览示例）；`examples/` 存预览配置。画面组件用 `origin-scene`（webgl2）。
  - 做法样板：导入「信号故障」0.2.1 作参考 → 另做 0.3 秒实拍转场 `components/signal-cut-footage` → 配独立音效 `audio-components/signal-cut-click`；配乐 `audio-components/jiangxin-fastcut-120bpm`。
  - **时间线用的是检查过的固定版本**：改了组件源码后，要再更新到时间线，成片才会变。交付前核对时间线引用的是哪一版。
  - 导入的配方原样保留作参考，改动另建组件，并在组件里注明参考来源。

Overlay Studio（`工具/AI剪辑/Overlay Studio`）只是学习参考：不复用它的代码、卡片或导出物，也不把它当成小八动效库的一部分（Playbook/偏好 “外部工具只学习不复用”，2026-10-20）。在 OriginCut 项目里可以直接用它自己的配方与组件；但配方库不等于小八动效库，小八动效库只借鉴形式、由小八自己定义（偏好 “小八动效库的形态”）。

## 不能做的

- **不改 `.origincut/` 里的任何内部文件**（事务、清单、缓存、会话都由 OriginCut 管理）。官方要求：作业中不移动素材、不改内部文件；搬项目要先在应用里关闭，再整个文件夹移动后重开。
- 不删素材、缓存、会话或项目；要删的移到 `私人助理/_待删/`，由用户清空。
- `.origincut/` 和素材不进 git，不额外备份（备份规则以运行宪法为准）。
- 不改 OriginCut 里各 Agent 的登录、自定义 API、base URL 或密钥设置。

## 在 OriginCut 之外（其他 Agent 为它准备素材）

- 可以把字幕、配乐、音效、图片等放进项目的 `素材/` 下新建的子文件夹，按 footage / images / audio 分类、文件名写清内容，然后告诉用户在 OriginCut 里使用。不要改已有素材。
- 导出由用户在 OriginCut 里完成（480P 到 4K，比例跟随时间线，需选保存位置）。成片按归属表放进 `AI视频管线/` 等项目目录。

## 注意

- 会话和工具结果会发给所选的 Agent 服务，不是完全离线。
- OriginCut 启动的 Codex 使用 OriginCut 自己的存储范围（`origincut-private-v1`）。2026-10-22 用户实测：它能读到父目录 `私人助理/AGENTS.md` 并正确复述第 0 节，所以小八入口规则对它生效。是否经过小八守卫钩子，等守卫启用后再测。
