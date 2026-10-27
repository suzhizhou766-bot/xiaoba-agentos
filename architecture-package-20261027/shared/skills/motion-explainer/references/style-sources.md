# 风格从哪来

开工前**先选一个验证过的风格**，由它决定「这支片子长什么样」。风格有两个来源。

---

## 一、开源的 Opus 5.5 风格库

这些风格都是作者用 Opus 5.5 做出成片、验证过的。用之前看一眼许可证，按要求注明出处。

| 风格库 | 里面有什么 | 许可 | 怎么用 |
|---|---|---|---|
| **lemo-opuscar**（lemomo-ai） | **43 种**风格。每种有 `STYLE.md`（材质、配色、字体、转场、声音）、`DEMO.md`（一支示范片的分镜）、`poster.jpg` 和一支成片，覆盖水彩、水墨、版画、蓝图、像素、瑞士排版、数据可视化等 | 代码 MIT；风格说明和成片 **CC BY 4.0**（要注明出处） | `bash scripts/get_style.sh lemo <风格名>`；列出全部风格：`bash scripts/get_style.sh lemo --list` |
| **opus-video-skills**（tuzhechen2005） | 2 种：`kinetic-reel`（动态排版作品集）、`painted-animation`（手绘水彩角色动画） | MIT | `bash scripts/get_style.sh ovs kinetic-reel`，或 `ovs painted-animation` |
| **ClaudeAnimationBase**（John Heibel） | 1 种：p5.brush 手绘水彩角色动画，带角色表情库和动画原则 | MIT | `bash scripts/get_style.sh cab` |

**注意：**
- 这些风格原本配的是各自作者的引擎（headless Chrome、p5.js 等）。本 skill 用的是 skia-canvas 引擎。**只借风格规范，不借引擎**：规范里提到对方代码函数的地方（比如 `engine.js` 的 `mk`、`drawS`），用本 skill 的引擎做出同样的效果即可。
- lemo 的作者说过，他的风格是按 Opus 5.5 调的，其他模型不保证效果一样。所以第一次用某个风格时，先出 3 张样张看看再做全片。
- 选风格时，看风格说明里的「适合什么内容」：媒介要能演你的内容：讲「选择」适合路口、地图、目录；讲「过程」适合说明书、蓝图；讲「回忆」适合水彩、版画。

## 二、自己积累的风格库（推荐长期做）

每做成一条你满意的片子，就把它的风格写成一份 `FRAME.md` 存起来，慢慢攒成自己的风格库。格式见 `references/frame-template.md`。

自己的风格库比开源的更好用，原因有三个：
- 它是**你**确认过好看的，审美是你的；
- 它已经在**你的内容**上验证过；
- 别人没有，片子不会和别人撞风格。

---

## 三、选好风格后怎么接进项目

1. 把风格规范放进项目根目录，命名为 `frame.md`。`get_style.sh` 会自动放好，同时把参考图放进 `style/`。
2. 打开 `style/` 里的参考图看一下。
3. 写 PLAN.md 时，媒介和配色直接用这个风格的；3 个概念里写清楚各用这个风格的哪几招（导演手册第 2、7 节）。
4. **冲突时的优先级**：`frame.md` 风格规范 > 导演手册。风格规范里没写的，按导演手册来。
