---
id: "experience-521b17f0-be50-42f8-aa64-9ba7c189551e"
type: "experience"
parent: "experiences"
status: "active"
authority: false
source_task_id: "521b17f0-be50-42f8-aa64-9ba7c189551e"
last_verified: "2026-10-11"
---

# 发布后必须核验实际 worker，而非仅看启动调用成功

候选经验，尚未由用户初审或终审。

2026-10-11 小八 AgentOS v1.5.3 用户执行切换后，activated=true、read_back.ok=true、两处入口/doctor/技能与知识索引通过，但实际 worker 仍为旧 PID 1167，源码和清单指纹均不匹配。

根因：候选切换脚本 ServiceControl.stop() 使用 launchctl bootout gui/UID LABEL 的错误参数形式，且启停返回码被忽略。running() 仅证明服务已登记，不证明已停启或新源码已加载。队列为空也不能证明旧进程已停止。

现场修复：先只读确认队列与租约为零、目标 PID 和命令精确匹配；用 launchctl bootout gui/UID/com.xiaoba.agentos，检查返回码并等待旧 PID 退出，再 bootstrap 精确 plist，检查返回码并实际读取 worker-state。新 PID 8855，alive/code_matches_disk/manifest_matches_disk/ok 全真。未修改线上源码、凭据、模型或路由，未创建备份。

复用建议：将启停返回码、旧进程已退出、新 PID 已出现和新指纹匹配列为发布门禁。下一阶段固定切换命令时在候选区修正 ServiceControl 与对应失败测试，再独立审核。现有已审核切换脚本未在本轮事后核验中热改；不能宣称其启停代码缺陷已修复。

证据：工作区/小八AgentOS-架构第1步/reports/S1-worker-restart.json、reports/S1-post-activation-live.json、reports/S1-post-activation-final.json；本轮独立审核任务 e0676ada-f0ec-4691-9d6e-766a224aaa00。
