---
id: "experience-a0ab406e-d750-498c-a0df-8aa2ba3e3221"
type: "experience"
parent: "experiences"
status: "active"
authority: false
source_task_id: "a0ab406e-d750-498c-a0df-8aa2ba3e3221"
last_verified: "2026-10-17"
---

# DSH alpha.3 无界面认证兼容

用户 2026-10-16 要求修复。问题：DSH 升级后，小八后台 headless（无界面）调用报 MISSING_CREDENTIAL（缺少凭据），桌面路由引用的密钥其实已存在于原 version 1（版本 1）凭据服务中。

根因：官方 headless bundle（模式补丁）默认禁用 credentials（本地凭据服务），只接受启动环境/.env 等来源。不能据报错就复制密钥、导出到环境变量或改 provider（供应商）。应先核对服务是否加载、引用是否存在和文件权限，诊断只输出元数据。

修复：本机 headless/cordis.patch.yml（无界面配置补丁）是指向 web/cordis.patch.yml 的既有符号链接；在其真实目标的原有补丁末尾添加 credentials 的 disabled:false（启用）覆盖。Web 原本已启用该服务，桌面模型文件不变；authorization（交互授权）仍禁用。cordis.yml 是启动器重建的空入口，不是持久补丁位置。此做法是本机用户明确授权的已有凭据兼容恢复，不应泛化成任何无人值守部署都默认加载个人凭据。

验证：使用与小八一致的 headless + 桌面补丁参数，真实请求退出 0、返回 DSH_CREDENTIAL_SERVICE_OK；原凭据文件和桌面配置哈希未变，权限保持 600，无密钥复制或环境注入。小八完整 session-start（上班探针）返回 ready（可接单），AGY/DSH 均 healthy（健康）。证据在同目录 dsh-auth-repair-test-final.json、dsh-auth-repair-agentos-start.json 与 DSH认证修复说明.md。

边界：健康探针与独立技能审查分别计数；不批准任何经验、不修改 AgentOS 发布包或放宽发布门禁。仍须用户对本候选初审和终审后才能成为正式长期经验。
