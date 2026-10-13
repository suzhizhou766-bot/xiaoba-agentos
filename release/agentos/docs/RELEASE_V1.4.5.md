# 小八 AgentOS v1.4.5 候选

修复 AGY 模型路由：启用 desktop_debug_port 时，每次调用跟随 Antigravity 桌面的全局模型选择，移除固定 --effort low。解析失败不使用备用模型。

完整实现与运行前提见 AGY_DESKTOP_FOLLOW.md。DSH、双后台门槛、数据库 schema 和记忆审批不变。验收通过后仍需用户批准正式提升。
