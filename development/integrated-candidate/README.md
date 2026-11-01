# 小八 AgentOS v1.6.6

谁审查、谁协作，只由用户在 Agent 池里设定。没有默认审查节点。要审查的任务至少两份彼此独立的合格结论才交付；独立性看入口程序和模型。宿主可以补一票，条件是这次的执行记录显示宿主没干活、也不是作者，而且不能两票都是宿主。池里没有够格的审查节点时，要审查的任务不入队，页面写明原因。配置模板里不再带固定后台名单；旧配置里的线路资料仍能读，不参与派活。

统一入口：`~/.agents/skills/xiaoba-agentos/scripts/xiaoba`；Codex 自动发现 skill：`~/.codex/skills/xiaoba-agentos`。入口 fail-closed 验证发布后运行已绑定 CLI。

运行源码：`/Users/suzhizhou/Documents/小八AgentOS`。不可变发布目录由 `.runtime/releases/index.json` 的当前指针决定，用 `verify-release` 回读实际 release_dir；本文件不写死发布目录的版本号。Vault 沿用现有 `第二大脑` 目录名，目录名不含版本号，不随版本变化，也不代表运行版本；正文/技能卡/索引统一为当前软件版本。

执行协议见内容目录里的协作技能（`私人助理/skills/collaboration-core/SKILL.md`，不随本包发布），分工接口见 `docs/COLLABORATION.md`。运维见 `docs/OPERATIONS.md`，验收边界见 `docs/ACCEPTANCE.md`。软件版本/知识协议版本/索引格式版本/历史版本的分层与兼容名映射见 `docs/VERSIONING.md`；当前软件版本唯一声明点是 `pyproject.toml` 的 `[project] version`，由 `scripts/agentos_version.py` 读取。

只有验证通过的新候选才可提升。旧版本及被移出的系统文档保留在私有回滚归档，不进入活动技能/知识索引。现有业务技能、媒体知识和素材原件保留，不因旧创建日期删除。

软件发布不批准待审经验。任务/审核/审计链与 outbox 持久数据不随版本替换；使用 `review-list --stage all` 进入人工审核。


本版新增：审计任务仅保留审计回执；方法类候选需要任务卡、真实隔离执行和独立双审后才可由用户终审。使用与限制见 docs/EXPERIENCE_TRIALS.md。新流程不替换已发布经验原有的复用计数、自动恢复与退役机制。旧候选保留旧流程并明确标注，未迁移数据。


本地第一批修复候选补充（2026-10-28）：验证扩展 v1、执行前原子认领、真实控制器回执与原生双审/外部接入口证据同源校验、暂停恢复和两轮材料补交 CLI/HTTP、人工拒绝保护及历史重放规则。使用和迁移边界见 docs/EXPERIENCE_TRIALS.md。本地测试通过不等于正式双审、构包或生产启用。

第二批 B4（2026-10-28）：待验证派生索引、显式真实应用、交付验收回原卡及人工终审证据绑定，见 [EXPERIENCE_LOOP](docs/EXPERIENCE_LOOP.md)。只增加材料路径和门禁条件，不替用户批准；本地开发候选尚未正式发布。
