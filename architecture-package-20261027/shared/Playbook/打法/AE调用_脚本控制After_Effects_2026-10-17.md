# AE调用：用脚本控制 After Effects

来源：2026-10-17 Claude（小克）接入 AE调用、做 Hello AE 文字动效 v2、测试 runner.sh 的一段工作。技能是 `skills/after-effects-control`，工具在 `工具/AE调用/`，交付记录在第二大脑 `70_复利规则/交付记录/AE调用接入-2026-10-17`。

## 流程
1. **上班检查**：看 `工作区/ae-scripts/logs/_heartbeat.txt` 是否 10 秒内更新；往队列放 `工具/AE调用/队列桥接/health_check.jsx`，读 `logs/ae-health.json` 的 `bridgePanelRunning`、`bridgeAutoRun`；用 MCP 的再调 `check-bridge`。判断 AE 有没有真的重启，看 `logs/startup.log` 的时间。
2. **选通道**：本机首选 MCP（面板要开着、Auto-run 勾上）；本机没接 MCP 用 `runner.sh --background`；远程 Agent 只能用文件队列。新会话才会加载 `.mcp.json` 里的 MCP；当前会话没加载时，可以直接用 stdio 连同一个 MCP 服务调用。
3. **先确认目标工程**：看窗口标题有没有未保存标记再切换工程；切换时只在确认没有改动后用“不保存关闭”，避免弹出保存对话框卡住 AE。
4. **会改工程的操作先在副本上做**：复制 .aep 到 `工作区/ae-projects/`，打开副本、改、保存、读回核对，最后比对原文件哈希并切回原工程。
5. **写脚本**：按技能第 3 节的规矩；脚本中途报错会留下半成品，重跑前按名字清掉本次留下的东西。
6. **验证**：`comp.saveFrameToPng` 存几帧拼成一张对比图自己看，再给用户；`contact-sheet` 目前不带图片。
7. **新引入或更新第三方 .jsx**：先在 AE 里整批做语法检查（去掉 #include 后 `new Function(src)`，只编译不执行）。保留字作未加引号的对象键会让 AE 弹窗且不执行，调用方只看到超时。

## 打开桥接面板
- 「窗口」菜单最底下，菜单很长要往下滚；后台读菜单读不到这一段，要全屏操控去点或请用户点。
- 可以停靠成标签页，切走没关系，不能点关闭。脚本打不开它，不要往队列放面板脚本。
