# motion-explainer

> 一套让 AI 做的动效视频更好看的规则，加上一套能直接跑的代码引擎。
> AI 写代码，逐帧画出画面、合成配乐，输出 30–60 秒的动效短片。有口播、无口播都可以。默认竖版 9:16（1080×1920），也可以横版 16:9（1920×1080）。

**English summary:** `motion-explainer` is a Claude Code / Codex skill: a set of rules plus a code engine that raise the quality of AI-made motion-graphics videos. Frames are drawn with skia-canvas and music is synthesized in code. Output is 9:16 vertical by default, with optional 16:9 landscape. It improves:
- directing, camera and rhythm (`references/director.md`);
- turning abstract lines into visible scenes (`references/visual-metaphors.md`);
- a summary of patterns distilled from a set of mature finished works (`references/opus-patterns.md`);
- motion feel, easing and detail (`references/craft.md`);
- a consistent visual style (`references/frame-template.md`, `references/style-sources.md`);
- a world/camera engine and music engine (`assets/template/engine/`);
- automatic checks (`assets/template/checks/`).

Documentation is in Chinese.

---

## 它从哪几个方面把片子做好

| 方面 | 文件 | 一句话 |
|---|---|---|
| 导演思维、镜头编排、节奏 | `references/director.md` | 先设计整支片：哪里是重音，哪里停顿，哪里变天。段落之间靠接力连起来，镜头在一个连续的世界里推拉，不一页一页翻 PPT。 |
| 把抽象的话演成画面 | `references/visual-metaphors.md` | 每句话找一个观众见过、会变化的东西来演。遮住字也能看懂。 |
| 规律总结（从一批成熟作品中提炼） | `references/opus-patterns.md` | 一批公开的高质量代码动效作品里反复出现的做法，例如结尾呼应开头、高潮前先安静、颜色省着用。 |
| 动效手感、缓动、细节 | `references/craft.md` | 进场出场的时长和缓动、预备和过冲、错相、弧线、快门模糊，还有版式和声音的具体数值。 |
| 风格与画面统一 | `references/frame-template.md`、`references/style-sources.md` | 先定一个风格，材质、配色、字体、转场都照它来。可以用开源风格，也可以攒自己的风格库。 |
| 引擎 | `assets/template/engine/` | 一个持续存在的世界加一台摄像机；配乐和画面读同一份时间表。每一帧都是时间的纯函数。 |
| 体检 | `assets/template/checks/` | 自动查响度、静止段、能量曲线、读字时间、字叠字、数字比例，出片前先过一遍。 |

`SKILL.md` 是入口，把这些串成 9 个步骤，每一步都写了完成标准。

## 安装

需要：

- Node.js（`new_project.sh` 会自动装 `skia-canvas`）
- `ffmpeg` / `ffprobe`
- Python 3 + `numpy`
- **字体**：引擎默认用 macOS 自带字体（PingFang SC、Kaiti SC、Songti SC、Avenir Next Condensed、DIN Condensed、Helvetica Neue、Menlo）。在 Linux / Windows 上，请改 `assets/template/engine/lib.mjs` 里的 `F`，换成你本机有的字体。
- 有口播时：需要自己准备一份 whisper 格式的逐词时间 JSON，放在项目的 `whisper/narration.json` 里，格式是 `segments[].words[]`，每个词有 `word`、`start`、`end`。本 skill 不负责转写。
- 联网（可选）：`scripts/get_style.sh` 会从 GitHub 下载开源风格。

把整个文件夹放进 skills 目录：

```bash
# Claude Code
git clone https://github.com/jeszhou/motion-explainer.git ~/.claude/skills/motion-explainer

# Codex
git clone https://github.com/jeszhou/motion-explainer.git ~/.codex/skills/motion-explainer
```

如果仓库里不止这一个 skill，只把 `motion-explainer/` 这个文件夹复制过去就行。

### 其他 AI 编程助手和国产大模型

这个 skill 本身就是一组 Markdown 规则 + Node / Python 脚本，不绑定哪家模型。任何能**读文件、执行命令**的 AI 编程助手都能用，比如 Cursor、Trae、Qwen Code、Kimi CLI、CodeBuddy，或者接了 DeepSeek、GLM、Kimi、通义千问的 Claude Code / OpenCode 等。

- 用法：把 `motion-explainer/` 放进你的项目（或任意目录），对助手说：「先读 `motion-explainer/SKILL.md`，按它的步骤把这段文案做成动效视频」。
- 支持 skills 目录的工具，可以照上面的方法放进对应目录；不支持的，在开头让它先读 SKILL.md 就行。
- 效果和模型能力有关：需要模型会写代码、能跑命令；第 6、8 步要打开拼板图看画面，需要模型能看图（多模态）。不能看图的模型可以跳过看图，只看体检报告。

## 使用

在对话里直接说，例如：

```
用 motion-explainer 把下面这段文案做成一支 30 秒的竖版动效视频，无口播：
<你的文案>
```

要横版就说「做成 16:9 横版」。

agent 会按 `SKILL.md` 里的 9 步走：

1. 建项目
2. 定风格
3. 写 BRIEF
4. 写 PLAN（重音表、物变表、接力表）
5. 先声音后画面
6. 逐段看
7. 出片 + 体检
8. 找最弱的 3 秒，改两轮
9. 交付 `out/final.mp4` 和 `NOTES.md`

也可以手动建项目：

```bash
bash ~/.claude/skills/motion-explainer/scripts/new_project.sh my-film          # 竖版 9:16（默认）
bash ~/.claude/skills/motion-explainer/scripts/new_project.sh my-film 16:9     # 横版 16:9
```

### 竖版和横版

- 画幅只有一个开关：项目根目录的 `aspect.txt`，内容是 `9:16` 或 `16:9`。建项目时第二个参数会写好它。临时想换，也可以用环境变量 `ASPECT=16:9` 覆盖。
- 引擎（`W`、`H`、安全区 `SAFE`）、渲染、拼板、体检都读这个开关，不用改别的地方。
- 安全区：竖版要躲开抖音的点赞栏、标题和顶栏（x 90–920、y 220–1480）。横版没有平台遮挡，四边各留 5%。
- 横版的构图建议见导演手册第 8 节。
- 自带的 6 秒示范片（`demo/film.mjs`）按竖版坐标写，横版项目里只能当写法参考。

## 文件结构

```
motion-explainer/
├── SKILL.md                    入口：9 个步骤和每步的完成标准
├── references/
│   ├── director.md             导演手册（建项目时成为 AGENTS.md）
│   ├── craft.md                运动、声音、版式的具体数值
│   ├── visual-metaphors.md     语义 → 画面对照表
│   ├── opus-patterns.md        规律总结（从一批成熟作品中提炼）
│   ├── frame-template.md       自建风格规范的模板
│   └── style-sources.md        开源风格从哪来、许可
├── scripts/
│   ├── new_project.sh          新建一支片子的项目（可选画幅）
│   └── get_style.sh            下载一个开源风格（frame.md + 参考图）
├── assets/template/            复制进每个项目的模板
│   ├── engine/                 画面引擎、渲染、配乐引擎、拼板
│   ├── checks/                 体检脚本
│   └── demo/                   6 秒示范片
├── CHANGELOG.md
├── MAINTAINING.md
└── LICENSE
```

## 这些规则从哪来

来自 10 轮盲测：同一份需求，Opus 和其他模型各做一版，逐秒对比成片；另外分析了 44 支公开的 Opus 风格样片。几个主要结论：

- Opus 赢在「整支片的设计」（哪里重、哪里停、段落怎么接），其他模型赢在「每一帧的执行」。导演手册主要补前者。
- 差距最大的地方是「这句话用什么画面来演」：Opus 会找一个具体、看得懂的东西，其他模型容易用抽象图形去「象征」。`visual-metaphors.md` 补这一层。
- 对其他模型帮助最大的一样东西是一个验证过的风格：其他模型 + 本 skill + 验证过的风格，在盲测里几乎追平了 Opus 裸跑。所以第 2 步先定风格。

实验记录没有公开。

## 局限

- 规则能防住格式问题，比如像 PPT、画面抢话。「把一句话翻译成什么画面」这类判断，规则只能帮一部分。
- 只支持 1080×1920 和 1920×1080 两种画幅，固定 30fps。
- 默认字体依赖 macOS（见上）。
- 规律来自少量公开的高质量代码动效作品，代表「做得好的片子」的规律，不是放之四海皆准的标准。
- 用 `get_style.sh` 下载的第三方风格各有许可。用它们发布成片前，请先看对方仓库的许可要求。

## 致谢

这些地方给了思路，或者是我们参考过的：

- **[lemo-opuscar](https://github.com/lemomo-ai/lemo-opuscar)**（lemomo-ai）：开源的 Opus 5.5 动效风格库。`get_style.sh` 能直接拿它的风格规范来用；`opus-patterns.md`、`visual-metaphors.md` 里的规律，很多是看它的公开成片总结出来的。
- **[opus-video-skills](https://github.com/tuzhechen2005/opus-video-skills)**（tuzhechen2005）：动态排版、手绘水彩角色动画两种风格。
- **[ClaudeAnimationBase](https://github.com/JohnHeibel/ClaudeAnimationBase)**（John Heibel）：p5.brush 手绘水彩角色动画，带表情库和动画原则。
- **观默的长文（2026-09-30）**：导演手册「运动语言」一节（错相、完整动作、动量连续、景深、接触阴影）的思路来源。

规则文字和引擎代码都是自己写的，没有搬上面这些项目的源码或原文。

## 许可

[MIT](LICENSE)：可以自由使用、修改、再分发，保留版权声明即可。
