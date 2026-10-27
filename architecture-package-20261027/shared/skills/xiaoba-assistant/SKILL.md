---
name: xiaoba-assistant
description: Compatibility trigger for 小八 AgentOS. Use when the user says “上班了小八助理”, continues a 小八 work session, or says “下班”; route all behavior to the current xiaoba-agentos and collaboration-core rules.
---

# 小八助理兼容入口

本技能只保留用户熟悉的触发词，不复制系统规则或维护第二套事实链。

- `上班了小八助理`：调用当前 `xiaoba-agentos` 入口完成发布校验、健康检查、状态发现和 `session-start`，再由 `collaboration-core` 读取当前记忆根。
- 工作中的每条消息：先交给 AgentOS `intake`。`direct` 才由会话模型回答；`agentos_multi` 才提交生产任务；两者必须显示不同的回答来源标识。
- 多 Agent 任务交付：展示裁判证据并等待用户交付初审，不自动发布经验。
- `下班`：按 `collaboration-core` 运行 `session-close`，展示当天全部待终审裁判；仅对用户逐项明确批准的候选执行发布和索引复验。

任何不可达、401、超时或 Provider 失败都按当前任务证据报告；不得擅自切换后台账号、桌面供应商或启用禁用 Agent。AGY+DSH 是当前独立 CLI 路线，就绪只以真实后台探针为准，不以桌面端是否在线代替。
