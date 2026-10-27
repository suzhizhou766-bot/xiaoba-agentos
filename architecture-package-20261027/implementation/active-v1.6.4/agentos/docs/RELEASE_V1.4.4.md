# 小八 AgentOS v1.4.4

正式方案仍为 AGY+DSH 双后台，两份独立合格结果才进入本地确定性裁判；Codex/Claude 后台保持禁用，桌面 Headroom 独立保留。统一 skill 入口先校验发布与源码哈希再执行。

本版实质内容是**版本标识与验收元数据同步**，不是后台拓扑变化。

下列改动以活动发布 v1.4.3 的受管清单为基线（`SOURCE_MANIFEST.json` sha256 `a60f38c0f51adb4a33efb7fcda797b8115a8930112919b03ccd6e12c52574ce9`，122 个受管文件）；本候选受管清单 sha256 `c751b7f56b1f902b0ff8febb92157767920735fd0db68bdc41bc522619478431`，128 个受管文件，新增 6 个、修改 17 个、删除 0 个。

- 软件版本唯一声明点落到 `pyproject.toml` 的 `[project] version`（活动包内仍是 `1.4.2`，本版改为 `1.4.4`）；新增 `scripts/agentos_version.py` 统一读取该声明点，`create_skill_index.py`、`create_knowledge_index.py`、`build_second_brain.py`、`build_release.py` 四个生成器改为从该模块取版本，不再内联版本字面量。
- 修复 v1.4.3 提升时遗留的版本标识缺陷。活动 v1.4.3 包内 `pyproject.toml` 仍写 `version = "1.4.2"`，`config/agentos.json` 的 `release.expected_version`、`skills/index.json` 与 `knowledge/index.json` 的 `release` 字段、`README.md`、入口 `SKILL.md`、`docs/ARCHITECTURE.md`、`docs/OPERATIONS.md`、`docs/COLLABORATION.md`、`docs/ACCEPTANCE.md` 与 `skills/collaboration-core/SKILL.md` 的正文版本标识本版统一到 v1.4.4。索引 `schema_version` 保持 1、数据库 schema 保持 7、知识协议族保持 v1.4：这三者是格式与协议版本，不随软件版本变化。
- 新增 `docs/VERSIONING.md`，明确软件版本、发布标识、知识协议/树格式族、历史版本记录四层版本模型，并列出冻结兼容名清单。
- 补入活动 v1.4.3 包内缺失的两份文件：`docs/RELEASE_V1.4.3.md`（版本方案）与 `docs/ACCEPTANCE_V1.4.3_EVIDENCE.md`（验收证据）。
- 扩充 `tests/test_current_release.py`，由 5 项增至 9 项，新增四项版本契约断言：当前文档声明同一软件版本、冻结兼容名永不跟升、系统当前态不写死带版本号的发布目录、生成器单点读取版本且不内联完整版本号。离线套件相应由 360 项增至 364 项。版本漂移从此是可失败断言，不是人工检查项。
- 追加 `docs/HISTORY_VERSIONS.json` 的 v1.4.4 记录，只追加不重排，v1.4.2 与 v1.4.3 记录逐字保留。

## 本版保留的冻结兼容标识

以下标识是外部集成的兼容面，刻意不随软件版本升版：

- Vault 物理目录名 `第二大脑_小八AgentOS_v1.4.1`：保留以维持 Obsidian 库路径绑定，目录名不代表运行版本。
- Vault 根笔记文件名 `00_小八AgentOS_v1.4.md`：属 v1.4 树格式族，正文声明当前软件版本。
- KnowLP 图目录名族 `knowlp-v1.4`：知识协议版本独立于软件补丁号。
- 已安装 skill 目录 `~/.agents/skills/xiaoba-agentos` 与 `~/.codex/skills/xiaoba-agentos`：保持全局入口路径稳定。

`tests/test_current_release.py::test_compatibility_identifiers_are_never_version_bumped` 把这些名字断言为冻结项。

## 版本标识约束

当前软件版本唯一声明点是 `pyproject.toml` 的 `[project] version`，由 `scripts/agentos_version.py` 读取；生成器不得内联版本字面量。发布目录不写死在正文，由发布索引与 `verify-release` 解析。四层版本与冻结兼容名见 `docs/VERSIONING.md`。

## 本版正文的起草与审计方式

本文件正文与 `docs/HISTORY_VERSIONS.json` 的 v1.4.4 记录字段由 `agy_review` 在隔离验收运行时的 `collaborate` 工作阶段起草；`docs/ACCEPTANCE_V1.4.4_EVIDENCE.md` 由 `dsh_review` 在同一工作阶段起草，并对本次版本同步独立审计有无漏改、有无误升冻结兼容名、有无改写历史记录。宿主对两份产出逐条回读磁盘校验后才集成，后台文本未被当作已执行补丁；集成时修正的两处事实错误记录在 `docs/ACCEPTANCE_V1.4.4_EVIDENCE.md` 的工作阶段小节。

是否已完成提升及真实验收以活动 release index、`RELEASE.json`、运行指纹和验收数据库记录为准。本文件是版本方案，不能单独作为验收证据。
