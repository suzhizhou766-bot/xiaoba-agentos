# 小八 AgentOS v1.6.1 · AGY + DSH

当前唯一启用后台：agy_review + dsh_review；两份独立合格结果才交付。两者都只走各自的本地 CLI 路由，模型由对应桌面端自行选择。

统一入口：`~/.agents/skills/xiaoba-agentos/scripts/xiaoba`；Codex 自动发现 skill：`~/.codex/skills/xiaoba-agentos`。入口 fail-closed 验证发布后运行已绑定 CLI。

运行源码：`/Users/suzhizhou/Documents/小八AgentOS`。不可变发布目录由 `.runtime/releases/index.json` 的当前指针决定，用 `verify-release` 回读实际 release_dir；本文件不写死发布目录的版本号。Vault 沿用现有 `第二大脑` 目录名，目录名不含版本号，不随版本变化，也不代表运行版本；正文/技能卡/索引统一为当前软件版本。

执行协议见内容目录里的协作技能（`私人助理/skills/collaboration-core/SKILL.md`，不随本包发布），分工接口见 `docs/COLLABORATION.md`。运维见 `docs/OPERATIONS.md`，验收边界见 `docs/ACCEPTANCE.md`。软件版本/知识协议版本/索引格式版本/历史版本的分层与兼容名映射见 `docs/VERSIONING.md`；当前软件版本唯一声明点是 `pyproject.toml` 的 `[project] version`，由 `scripts/agentos_version.py` 读取。

只有验证通过的新候选才可提升。旧版本及被移出的系统文档保留在私有回滚归档，不进入活动技能/知识索引。现有业务技能、媒体知识和素材原件保留，不因旧创建日期删除。

软件发布不批准待审经验。任务/审核/审计链与 outbox 持久数据不随版本替换；使用 `review-list --stage all` 进入人工审核。
