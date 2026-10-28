# 小八 AgentOS v1.4.3

正式方案仍为 AGY+DSH 双后台，两份独立合格结果才进入本地确定性裁判；Codex/Claude 后台保持禁用，桌面 Headroom 独立保留。统一 skill 入口先校验发布与源码哈希再执行。

本版实质内容是**记忆晋升与检索门禁加固**，不是后台拓扑变化：

- `store.py` 引入显式经验状态机：`MEMORY_CANDIDATE_TRANSITIONS`、`MEMORY_REVIEW_STATUS_MAP`、`MEMORY_ACTIVE_RETRIEVAL_STATES`、`validate_memory_transition()`；活动检索只认 `published`。
- `cli.py` 新增 `memory-receipt`，输出两次批准计数、`fully_approved`、`publish_ready` 与事件列表；`review` 输出带 `review_id`/`previous_status`/`reviewed_status`。
- `knowledge.py` 收紧检索双重门禁：KnowLP 只定位，命中还须回读 Vault 原文 `status: "active"` 且数据库为 `published`，读不到就 fail closed。
- `docs/ACCEPTANCE.md` 增加“经验晋升状态机”边界，`docs/COLLABORATION.md` 增加 Memory Promotion Boundary。
- 新增 `tests/test_knowledge_lock.py` 与 `tests/fixtures/`：用离线 fixture 覆盖“正确页面 vs 相邻错误页面”的近邻误引用回归，不读取真实 Vault、不调用模型。
- 数据库 schema 不变（`user_version=7`），无迁移。

## 本版遗留的版本标识缺陷

v1.4.3 提升时只更新了 `config/agentos.json` 的 `release.expected_version`、发布索引与 `RELEASE.json`，**没有更新软件版本声明与正文版本标识**：活动包内 `agentos/pyproject.toml` 仍写 `version = "1.4.2"`，`README.md`、入口 `SKILL.md`、`docs/ARCHITECTURE.md`、`docs/OPERATIONS.md`、`skills/index.json` 与 `knowledge/index.json` 的 `release`、第二大脑生成器仍写 v1.4.2；`RELEASE.json` 的 `release_origin` 与 `contents.second_brain` 也仍是 v1.4.2 字面量；本版没有 `docs/RELEASE_V1.4.3.md` 与历史记录条目。这些由 v1.4.3 元数据同步候选修复，详见 `docs/ACCEPTANCE_V1.4.3_EVIDENCE.md`。

## 版本标识约束（本版起生效）

当前软件版本唯一声明点是 `pyproject.toml` 的 `[project] version`，由 `scripts/agentos_version.py` 读取；生成器不得内联版本字面量。发布目录不写死在正文，由发布索引与 `verify-release` 解析。四层版本与冻结兼容名见 `docs/VERSIONING.md`。

是否已完成提升及真实验收以活动 release index、`RELEASE.json`、运行指纹和验收数据库记录为准。本文件是版本方案，不能单独作为验收证据。
