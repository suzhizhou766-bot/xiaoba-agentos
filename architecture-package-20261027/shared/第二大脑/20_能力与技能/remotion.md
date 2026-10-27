---
id: "skill-remotion"
type: "skill-card"
parent: "capabilities"
status: "active"
authority: false
source_path: "/Users/suzhizhou/Documents/私人助理/skills/remotion/SKILL.md"
last_verified: "2026-09-26"
---

# remotion

状态：active / callable

类别：video-composition

能力：remotion-composition, data-driven-video, render-qc

可复现的 React/TSX 视频合成、数据驱动动画、地图动画与技术交付。

入口哈希：`0a641ccd3d1eed805147c2bb4f553e00cd3a60bdc67c753d76b8a24ca77d0072`

## 本机工具

可直接运行的 Remotion 项目和 Studio 装在 `/Users/suzhizhou/Documents/私人助理/工具/Remotion`（Remotion 4.0.535），是视频管线的合成核心。不开机自启、不常驻：需要预览时由用户双击该目录的 `启动Remotion.command` 开启，Studio 地址 `http://localhost:3330`；Agent 在该目录的 `src/` 里改 React/TSX，用 `npm run render:demo -- out/<文件名>.mp4 --codec=h264` 导出，一次性导出不需要 Studio 开着。Studio 没开而任务需要预览时提示用户开启，不要自行拉起。说明见同目录 `使用说明.md`，接口见 `私人助理/系统/小八工作体系/interfaces.md` 的 Remotion 一节。

相关检索词：Remotion、Studio、视频合成、渲染、导出视频、3330、程序化视频。
