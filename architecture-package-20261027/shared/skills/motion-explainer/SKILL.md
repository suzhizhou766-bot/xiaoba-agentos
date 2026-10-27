---
name: motion-explainer
description: 用代码做动效短片（默认竖版 9:16，也可横版 16:9；30–60 秒，有口播或无口播，画面 + 代码合成配乐）。触发：把文案或要点做成动效视频、用代码做视频、motion explainer。
---

# motion-explainer

用代码做动效短片，默认竖版 9:16（1080×1920），也可以横版 16:9（1920×1080）。每一帧都是时间的纯函数；配乐也用代码合成。规则写在导演手册（建项目后是项目里的 `AGENTS.md`），这里只列步骤。每一步做完，满足「完成标准」才进下一步。

## 步骤

0. **先读审美手册**：画面、节奏、配色这类审美判断，先读 `私人助理/审美提升/审美手册.md` 和其中的 taste 卡（小八本地补充，2026-10-22）。
1. **建项目**：`bash <本skill>/scripts/new_project.sh <项目目录> [9:16|16:9]`，进入项目目录。画幅不写就是竖版；用户要横屏（B 站、YouTube、电脑上看）时写 `16:9`。画幅存在项目根目录的 `aspect.txt`，以后不用再管；横版构图看手册第 8 节。
   - 完成标准：目录里有 `AGENTS.md`、`aspect.txt`、`engine/`、`checks/`、`demo/`、`node_modules/skia-canvas`。

2. **定风格**：
   - 用户指定了风格：照用，放成项目根目录的 `frame.md`。
   - 用户有自己的风格库：从里面挑一个最适合这份文案的。
   - 都没有：按文案内容从开源库挑（`bash <本skill>/scripts/get_style.sh lemo --list`，再 `get_style.sh lemo <名字>`），或者自己定一个，用 [references/frame-template.md](references/frame-template.md) 写成 `frame.md`。挑的标准是「这份文案的意思，用这个风格的材料最容易演出来」。
   - 完成标准：根目录有 `frame.md`；打开看过 `style/` 里的参考图；在 `NOTES.md` 里写一句「为什么是这个风格」。

3. **读手册，写 BRIEF.md**：读 `AGENTS.md` 全文。把用户需求整理进 `BRIEF.md`：必须出现的文字（一字不差）、时长、观众、有没有口播。
   - 完成标准：`BRIEF.md` 里每一句必须出现的文字都列出来了。

4. **写 PLAN.md**（手册第 2 节）：3 个概念 → 重音表 → 物变表 → 接力表 → 时间表。
   - 完成标准：重音表里每个重音都有秒数（有口播时写触发的那个词）；物变表每一行都通过遮字检验；接力表里每个段落边界都写了被带过去的物体；换页 ≤ 2 处。

5. **先声音后画面**（手册第 4 节）：有口播时用 `words()` 把时间锚在字上；无口播时先写 `score.py` 生成 `music.wav` + `cues.json`。画面用 `engine/world.mjs` 写，从 `demo/film.mjs` 起步。
   - 完成标准：`film.mjs` 里重音时间都来自 `T.at('词')` 或 `cues.json`，没有写死的秒数。

6. **逐段看**：每写完一段，`node engine/sheet.mjs out/s.png --range 起 止 0.1`，打开图片。
   - 完成标准：物变表里这一段的每一行，在拼板上都能指出「前」和「后」那两格。

7. **出片 + 体检**：`./engine/run.sh` → `./checks/check_all.sh`。
   - 完成标准：0 个 FAIL；响度 −14 LUFS ±1；能量曲线峰值 ≥ 中位数 5 倍，而且「超过 5 倍的秒」落在重音表的秒数上（±0.5 秒）。

8. **找最弱的 3 秒，改掉**（手册第 6 节）：生成 `node engine/sheet.mjs out/all.png --every 1`，按手册第 6 节的检查表找出全片最弱的 3 个时刻，写下秒数、问题、改法，改完重新出片。再做一轮。
   - 完成标准：NOTES.md 里有两轮「最弱 3 秒」的记录，每条都有秒数和改前改后的截图路径。

9. **交付**：`out/final.mp4` + `NOTES.md`（风格理由、选了哪个概念和为什么、时间表、两轮最弱 3 秒、还剩的不足）。

## 参考资料（用到时再打开）

- 每句话找什么画面：[references/visual-metaphors.md](references/visual-metaphors.md)（第 4 步写物变表时）
- 运动、声音、版式的细节数值：[references/craft.md](references/craft.md)（第 5–8 步写代码时）
- 风格从哪来、许可：[references/style-sources.md](references/style-sources.md)
- 这些规则的来源（44 支 Opus 样片的规律）：[references/opus-patterns.md](references/opus-patterns.md)
