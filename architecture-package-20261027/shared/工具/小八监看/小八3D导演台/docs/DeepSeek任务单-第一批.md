# 小八 3D 导演台 · 外派任务单（第一批，给 DeepSeek Harness）

派单方：Claude Code（宿主，负责拆分、收尾和验收）
执行方：DeepSeek Harness（DeepSeek V4.1 Flash）
日期：2026-10-06

## 先读这一段（每张任务卡都适用）

工程目录：`/Users/suzhizhou/Documents/私人助理/工具/小八监看/小八3D导演台`
这是一个 React + TypeScript + Vite + Three.js 的纯前端工程，依赖已装好，**不要联网安装或升级任何依赖**。

硬性规则：

1. **只改任务卡"允许修改"里列出的文件。** 其他文件一律不动，尤其是 `src/App.tsx` 里和本卡无关的部分、`src/core.ts`、`src/store.ts`、`src/storage.ts`、`src/style.css`、`src/DraftsModal.tsx`——宿主正在改它们。
2. **不改功能和界面表现。** 这一批全是"行为不变"的任务。
3. **一次只做一张卡**，按 T1 → T2 → T3 → T4 的顺序。每张卡做完先跑验收命令，通过了再做下一张。
4. **验收命令必须真的运行**，把输出的最后几行原样贴进回执。不要凭推断写"应该通过"。
5. **遇到下面任何一种情况就停下来写回执，不要自己想办法绕过去：**
   - 验收命令失败，改了两次还没过；
   - 需要动"允许修改"之外的文件才能完成；
   - 卡片描述和你看到的代码对不上；
   - 你不确定某个改动会不会改变行为。
6. 不要运行 `git` 的任何写操作，不要删除文件，不要启动开发服务器（`npm run dev`）。

每张卡的回执格式（照抄填空）：

```
任务卡：T?
结果：完成 / 未完成（停在哪一步）
改了哪些文件：
验收命令与输出末尾：
  npm test   → tests ?? / pass ?? / fail ??
  npm run build → （贴 built in … 那一行和 dist/assets 的文件清单）
和卡片描述不一致的地方（没有就写"无"）：
我不确定的地方（没有就写"无"）：
```

---

## T1 · 把第三方库拆成独立的打包文件

**目标**：现在整个应用打成一个 1.6 MB 的脚本。把体积最大的第三方库拆成单独的文件，让浏览器能分别缓存。

**允许修改**：只有 `vite.config.ts`。

**怎么做**：在 `build` 里增加 `rollupOptions.output.manualChunks`，按下面分组（用函数形式，按模块路径里是否包含这些包名判断）：

- `three`：路径含 `/node_modules/three/`
- `r3f`：路径含 `/node_modules/@react-three/`
- `react`：路径含 `/node_modules/react/`、`/node_modules/react-dom/`、`/node_modules/scheduler/`
- 其余保持默认

`base: "/stage/"` 和现有的其他配置**原样保留**。

**验收**：

```sh
npm test
npm run build
ls -la dist/assets
```

- 测试数量不变且全部通过（当前是 82 项）。
- `dist/assets` 里出现名字以 `three`、`r3f`、`react` 开头的三个 `.js` 文件，加上主入口文件。
- `dist/index.html` 里的资源路径仍以 `/stage/assets/` 开头。

---

## T2 · 视频导出模块改成用到时才加载

**目标**：`src/videoExport.ts` 依赖 `mp4-muxer`，只有点"导出视频"时才用得到，现在却在打开页面时就加载。

**允许修改**：只有 `src/App.tsx`，而且只能改下面说的三处。

**怎么做**：

1. 文件顶部第 46 行附近的
   `import { renderShotVideo, downloadVideo, type VideoExportResult } from "./videoExport";`
   改成只保留类型导入：
   `import type { VideoExportResult } from "./videoExport";`
2. 文件里一共有 2 处调用 `renderShotVideo(`、1 处调用 `downloadVideo(`（用搜索确认数量）。在**每个**调用它们的 `async` 函数里，调用之前加一行：
   `const { renderShotVideo, downloadVideo } = await import("./videoExport");`
   （那个函数里只用到其中一个，就只解构用到的那个。）
3. 其他任何代码都不要动。

**验收**：

```sh
npm test
npm run build
ls -la dist/assets
```

- 测试全部通过，构建无 TypeScript 报错。
- `dist/assets` 里多出一个名字里带 `videoExport` 的 `.js` 文件。

---

## T3 · 给运动曲线补充单元测试

**背景**：宿主刚加了"运动曲线"（缓入缓出）。实现在 `src/core.ts` 的 `easeProgress`、`sample`、`sampleCamera`、`sampleTransform`，已有测试在 `src/ease.test.ts`。请先读这个测试文件，照它的写法补充。

**允许修改**：只有新建的 `src/ease-extra.test.ts`。**不要改任何已有文件**——如果你认为实现有 bug，写进回执，不要去修。

**要补的测试**（每条一个 `test(...)`）：

1. 四种曲线在 `t = 0.5` 时的值分别是：`linear` 0.5、`easeIn` 0.25、`easeOut` 0.75、`easeInOut` 0.5。
2. `easeIn` 和 `easeOut` 互为镜像：对 `t` 取 0、0.1、0.2 … 1，`easeIn(t) + easeOut(1 − t)` 都等于 1（允许 1e-12 的误差）。
3. 只有一个关键帧的轨道：`sample` 在任何时间都返回这个关键帧的位置，不管它设了什么曲线。
4. 三个关键帧、三种不同曲线：在每个关键帧的**准确时间点**上，`sample` 返回的就是该关键帧自己的位置（曲线不能让路径偏离关键帧）。
5. 关键帧乱序传入（时间不按从小到大排列）时，`sample` 的结果和排好序时一样。

**验收**：

```sh
npm test
```

- 总数从 82 变成 87，全部通过。
- 如果某条测试按上面的描述写出来却失败了，**保留这条失败的测试**，在回执里说明是哪条、实际得到什么值。这是有用的信息，不要为了让它通过去改期望值。

---

## T4 · 更新工程自己的 README

**允许修改**：只有 `README.md`（工程目录里这一份，不是上一级监看中心的）。

**怎么做**：

1. 「启动」一节：开头加一段说明——这个工程现在由小八监看中心提供，正常使用入口是 `http://127.0.0.1:8799/#stage`，随监看中心启停；`npm run dev` 只在开发调试时用，此时地址是 `http://127.0.0.1:5173/stage/`（注意多了 `/stage/`）。原有内容保留。
2. 「时间轴与摄影约定」一节第一条里写着"数值线性插值"。改成：数值按关键帧的"运动曲线"插值，可选匀速、缓入、缓出、缓入缓出，缺省为匀速；曲线设在一个关键帧上，作用于它到下一个关键帧之间这一段；在时间轴工具栏的"运动曲线"下拉框里设置。
3. 「尚未实现」里有一句"没有骨骼动画、曲线缓动或视频模型 API"，把其中的"曲线缓动"删掉（已实现），其余不动。
4. 不要改写、润色或重排其他任何段落。

**验收**：

```sh
grep -n "8799/#stage" README.md
grep -n "运动曲线" README.md
grep -n "曲线缓动" README.md
```

- 前两条各至少有一行输出；第三条没有输出。
- 在回执里贴出这三条命令的输出。
