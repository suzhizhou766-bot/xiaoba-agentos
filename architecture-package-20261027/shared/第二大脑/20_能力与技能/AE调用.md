---
id: "after-effects-tool"
type: "tool-card"
parent: "capabilities"
status: "active"
authority: false
source_path: "/Users/suzhizhou/Documents/私人助理/工具/AE调用"
last_verified: "2026-10-17"
---

# AE调用

After Effects 调用入口卡。Agent 用脚本（ExtendScript）和表达式控制用户 Mac 上正在运行的 Adobe After Effects 2026：合成、图层、文字动画、关键帧、表达式、特效、素材导入与重链、渲染、批量操作和工程检查。它属于工具入口卡，不把工具本体复制进第二大脑。

关键词：AE、After Effects、调用AE、控制AE、动效、文字动画、MG、表达式、ExtendScript、jsx、aep、合成、关键帧、渲染、MCP。

- 工具目录：/Users/suzhizhou/Documents/私人助理/工具/AE调用（使用说明.md、deployment.json）
- 技能：/Users/suzhizhou/Documents/私人助理/skills/after-effects-control/SKILL.md（怎么选通道、上班检查、写脚本的规矩、表达式配方、验证方法）
- 模块清单：系统/小八工作体系/modules/after-effects.json；接口契约见 系统/小八工作体系/interfaces.md 的“AE调用”一节

## 装了什么

- after-effects-mcp：开源 MCP 服务（a-y-ibrahim 增强版 v1.13.0，MIT），57 个工具，含看画面帧、检查合成、后台渲染、执行任意脚本。本机 Agent 用。
- adobe-agent-skills：开源 Agent 技能（aedev-tools，Apache-2.0），34 个现成脚本和 9 篇规则文档。本机终端用 runner.sh 直接跑，远程 Agent 经队列桥接调用。
- 队列桥接：小八自己的 xk_lib_call、health_check、open_mcp_bridge 三个脚本（open_mcp_bridge 只检查面板开没开，脚本打不开面板）。

## 三条通道

1. 文件队列（任何 Agent，含远程会话）：.jsx 写临时文件后 mv 进 工作区/ae-scripts/queue/，AE 每秒执行，日志在 logs/。AE 启动时由用户安装的 Startup/xk_bootstrap.jsx 自动加载执行器。
2. MCP（本机 Agent）：命令 配置/node/bin/node，参数 工具/AE调用/after-effects-mcp/build/index.js。桥接面板已装进 AE 的 ScriptUI Panels；用前要开着：「窗口」菜单最底下（要往下滚）的 mcp-bridge-auto.jsx，用户点或本机 Agent 用电脑操控点。配置片段在工具目录 config/。
3. 本机终端：adobe-agent-skills 的 runner.sh。

## 上班后先做

1. 看 工作区/ae-scripts/logs/_heartbeat.txt，10 秒内更新过说明 AE 开着、队列在工作。
2. 队列执行 队列桥接/health_check.jsx，读 logs/ae-health.json。
3. 用 MCP 时调用 check-bridge；health_check 的 bridgePanelRunning 为 0 时先打开面板。

## 已验证的边界（2026-10-17）

- 文件队列：AE 重启后自动加载、自动执行，多次验证通过；做过一个四段文字动效样例（技能 examples 里）。
- 队列桥接调用 34 个现成脚本：active-state、project-overview 通过。
- MCP 端到端已通：面板装好并打开后 check-bridge 返回 ok，Documents/.mcp.json 已接入（2026-10-17）。
- 脚本打不开桥接面板：findMenuCommandId 对所有 ScriptUI 面板返回 0；用 evalFile 直接跑面板脚本会让队列停掉，只能重启 AE 恢复。
- runner.sh：本地修好版本识别和不能正常返回两处问题后，后台模式实测通过，并在真实工程上跑通 7 个只读脚本（2026-10-17）；在工程副本上跑通 5 个会改工程的脚本；顺带修了 batch-rename、easing-presets 两个上游语法错误（用了保留字作键，AE 会弹窗且不执行）。
- AE 不提供辅助功能树，屏幕操控只能全屏接管，只用来看效果或处理弹窗。
- 删除、覆盖已有渲染、丢弃未保存工程属于不可逆操作，动手前先问用户。不开机自启新东西、不往 /Applications 写，这些由用户亲手做。
