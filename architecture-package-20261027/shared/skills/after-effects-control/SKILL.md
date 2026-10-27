---
name: after-effects-control
description: >-
  Control Adobe After Effects 2026 on the user's Mac through scripts and expressions — create or modify
  comps, layers, text animation, keyframes, expressions, effects, footage, rendering, batch operations,
  and project inspection. Use whenever a 小八 task touches After Effects / AE / .aep, from any agent
  (local Claude Code, Codex, DSH, AGY, or a remote Claude session that can only write files).
---

# AE 控制（小八通用）

工具本体、安装状态和安全审查在 `/Users/suzhizhou/Documents/私人助理/工具/AE调用/使用说明.md`；本技能讲怎么用、怎么写、怎么验。

## 0. 先选通道

| 你是谁 | 用哪条 | 入口 |
|---|---|---|
| 本机 Agent，客户端已接 `after-effects` MCP | MCP（首选） | `check-bridge` → `inspect-comp` / `execute-script` / `see-frame` 等 57 个工具 |
| 本机 Agent，有终端但没接 MCP | runner.sh 跑 34 个现成脚本（后台模式） | `bash 工具/AE调用/adobe-agent-skills/skills/after-effects/scripts/runner.sh --background <脚本> '<JSON>'`，结果打印到终端 |
| 只能读写文件的 Agent（远程 Claude 等） | 文件队列 | 写 .jsx → `mv` 进 `工作区/ae-scripts/queue/` → 读 `工作区/ae-scripts/logs/` |

runner.sh 是 adobe-agent-skills 自带的，小八本地修过两处（2026-10-17，见 `工具/AE调用/使用说明.md`）：原来会把 “Adobe After Effects 2026” 拆成四个“版本”，也会在 AE 执行完后不输出结果就退出。修好后后台模式实测：自动识别 AE 2026，正常结果和出错结果都在约 1 秒内返回，退出码 0，没有残留进程。一律加 `--background`：不加会把 AE 切到前台，这个模式没测过。远程 Agent 跑不了它，用文件队列的 `xk_lib_call`（第 2 节）调同一批脚本。

屏幕操控只用来看一眼或处理弹窗：AE 不提供辅助功能树，只能全屏接管，点关键帧又慢又不准。

## 1. 上班检查（用户说“上班了小八助理”后，第一次碰 AE 前）

1. 读 `工作区/ae-scripts/logs/_heartbeat.txt`：10 秒内更新过 = AE 开着、队列在工作。没更新就提示用户打开 AE，不要自行拉起。
2. 往队列放 `工具/AE调用/队列桥接/health_check.jsx`，读 `logs/ae-health.json`（版本、当前工程、活动合成、`bridgePanelInstalled` / `bridgePanelRunning` / `bridgeAutoRun`、脚本写文件权限）。
3. 用 MCP 时调用 `check-bridge`。`bridgePanelInstalled` 为 0：面板没装，告诉用户按使用说明装，别自己往 /Applications 写。`bridgePanelRunning` 为 0：请用户从「窗口」菜单最底下（菜单很长，要往下滚）点 `mcp-bridge-auto.jsx`，本机 Agent 也可以用电脑操控去点。脚本打不开面板，不要往队列放面板脚本（会弹出不响应的窗口并让队列停掉）。`bridgeAutoRun` 为 0：请用户勾上 Auto-run commands。
4. 动手前确认目标工程：活动工程不是用户说的那个就先问。

## 2. 文件队列用法（任何 Agent 都能用）

- 写成临时文件再改名进队列，避免 AE 读到写了一半的脚本：`cat > x.tmp … && mv x.tmp queue/任务名.jsx`。
- 执行器自动记 `logs/<任务名>.log`：`OK 用时 …` 或 `ERROR … @line …`。自定义输出用 `$.global.xkLog("任务名", "消息")`。
- 结果数据写成 JSON 文件到 `logs/`，再读回来。
- 执行完的脚本在 `done/`，要重跑就复制回队列。
- 调用 34 个现成脚本：

  ```js
  $.evalFile(new File("/Users/suzhizhou/Documents/私人助理/工具/AE调用/队列桥接/xk_lib_call.jsx"));
  xkLibCall("comp-detail.jsx", {compName: "主合成"}, "comp-detail");   // → logs/comp-detail.json
  ```

  常用：`active-state`（当前状态）、`project-overview`（工程概览）、`comp-detail`、`layer-detail`、`expression-errors`、`project-audit`、`layer-stagger`、`easing-presets`、`batch-rename`、`batch-expression`、`render-queue-batch`、`srt-import`、`comp-from-csv`、`true-comp-duplicator`。参数说明在 `adobe-agent-skills/skills/after-effects/SKILL.md`，领域规则在同目录 `rules/`。

## 3. 写 ExtendScript 的规矩

- ES3 语法：没有 `let/const`、箭头函数、模板字符串、`Array.forEach`；`JSON` 不一定有，需要时 `$.evalFile` 引入 `adobe-agent-skills/.../lib/json2.jsx`。
- 所有改动包在 `app.beginUndoGroup("…")` / `app.endUndoGroup()` 里；整段放 `try/catch`，出错写日志并结束撤销组。
- 不要 `alert()`、`confirm()`：弹窗会卡住 AE 和队列。
- 用 matchName 取属性和特效（`"ADBE Text Properties"`、`"ADBE Transform Group"`、`"ADBE Gaussian Blur 2"`），不用界面显示名：用户的 AE 是中文界面。
- **加属性会让之前拿到的引用失效**：`addProperty` 之后从图层重新逐级取，不要复用旧变量（报 “object is invalid” 就是这个）。
- 文字图层：先 `sp.value` 取 `TextDocument`，改完 `sp.setValue(td)`；关全大写用 `try { if (td.allCaps) td.allCaps = false } catch (e) {}`（AE 26.2 里 `allCaps` 是只读，直接赋值会报错中断整段脚本）并 `try { td.fontCapsOption = FontCapsOption.FONT_NORMAL_CAPS }`；字体用 PostScript 名（`Helvetica-Bold`、`Menlo-Bold`、`PingFangSC-Semibold`），设失败要记日志。
- 居中：在加文字动画器之前用 `sourceRectAtTime` 量尺寸，把锚点设到中心再定位。
- 按名字找合成和图层；同名先清理或改名，不要盲目新建重复项。
- 不主动保存工程：渲染出的成片就是交付物，脚本里不要自带 `app.project.save()`。用户要求保存时，存到用户指定的位置，新工程默认存 `工作区/ae-projects/`；用户可能同时在 AE 里改东西，保存前先看 `app.project.dirty` 和活动合成。（2026-10-18 用户裁定）

## 4. 表达式配方

- 逐字动画用“表达式选择器”（`ADBE Text Expressible Selector`，数量 `ADBE Text Expressible Amount`），**返回三维数组** `[v, v, v]`；`v = 100` 表示完全受动画器影响，`0` 表示原样，负值可以做过冲回弹。可用 `textIndex`、`textTotal`、`inPoint`。
- 逐字延迟：`t = time - inPoint - (textIndex - 1) * 0.06`。
- 弹性回弹：`v = t <= 0 ? 100 : 100 * Math.cos(2.2 * t * Math.PI * 2) * Math.exp(-5.5 * t)`，配动画器缩放 0%。
- 随机但稳定：每个字先 `seedRandom(textIndex, true)` 再 `random(a, b)`，要第二个随机数就换种子（`textIndex + 99`）。
- 打字机：源文本表达式 `s.substr(0, n) + 光标`，等宽字体、左对齐，锚点放在左边。
- 从中间向两边展开：延迟用 `Math.abs(textIndex - (textTotal + 1) / 2)`。
- 出场淡出：图层不透明度 `linear(time, outPoint - 0.35, outPoint, 100, 0)`。
- 常用内置：`wiggle(频率, 幅度)`、`loopOut("cycle")`、`valueAtTime(time - 延迟)`、`ease/linear(t, t0, t1, v0, v1)`。
- 完整可跑的例子：`examples/hello_fx_四段文字动效.jsx`（弹性弹出、打字机、散落飞入、字距加发光）。

## 5. 验证（交付前必须做）

- 读日志确认没有 `ERROR`。
- 截预览帧：`comp.saveFrameToPng(秒数, new File(路径))`，存到 `工作区/ae-scripts/preview/`，打开图片自己看；MCP 可用 `see-frame`；`contact-sheet` 目前不带图片（见第 7 节）。几张拼成一张对比图给用户看。
- 技术通过不等于用户采用：动效好不好看由用户决定。

## 6. 边界

- 先问用户再做：删除图层、素材或合成；覆盖已有渲染文件；关闭或丢弃未保存工程；大批量改动别人的工程。
- 不开机自启新东西、不装常驻组件、不往 `/Applications` 写：这些交给用户亲手做（Startup 里的 `xk_bootstrap.jsx` 是用户已装的唯一启动项）。
- 渲染输出到项目目录或 `工作区/`，不写第二大脑。
- 带审美判断的动效先看 `审美提升/审美手册.md`。

## 7. 已知坑

- AE 2024 在 macOS 上用 osascript `DoScriptFile` 会卡死，2025 起已修；还卡就改用 JavaScript 模式：`osascript -l JavaScript -e 'Application("Adobe After Effects 2026").doscriptfile("…")'`。
- 桥接面板版本和 MCP 服务不一致时，`check-bridge` 会报警：重新复制面板并重启 AE。
- 远程 Agent 的命令行在隔离环境里，跑不了 osascript，只能用文件队列。
- MCP 的 `contact-sheet` 在本机实测只返回标题文字、不带图片（2026-10-17）；看效果改用 `execute-script` 里 `comp.saveFrameToPng` 存帧，再拼成一张对比图。
- `execute-script` 中途报错时，报错前已建的合成、图层、纯色会留在工程里：重跑前先按名字清掉本次留下的半成品。
- 用屏幕操控输入中文路径要靠「前往文件夹」（Cmd+Shift+G）；中文输入法下打字可能出错。
