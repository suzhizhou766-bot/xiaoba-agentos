# 版本标识分层

小八 AgentOS 同时存在多种“版本号”，含义不同，不得互相替代，也不得一起批量替换。

## 四层版本

| 层次 | 当前值 | 唯一声明点 | 校验位置 |
| --- | --- | --- | --- |
| 软件版本 | `v1.6.1`（候选） | `pyproject.toml` 的 `[project] version` | `scripts/agentos_version.py` 的 `software_version()` |
| 发布标识 | `v1.6.1`（目标；活动索引仍为 `v1.5.6`） | `.runtime/releases/index.json` 的 `current_release` | `src/xiaoba_agentos/release.py` |
| 知识协议 / 树格式族 | `v1.4` | `scripts/build_second_brain.py` 生成的树结构 | `build_second_brain.py --verify` |
| 历史版本记录 | `v1.4.2`、`v1.4.3`、`v1.4.6`、`v1.4.9`、`v1.4.10`、`v1.5.0`、`v1.5.1` | `docs/HISTORY_VERSIONS.json`（只追加） | `tests/test_release_version_contract.py` |

软件版本是唯一可以随发布递增的层次。其余三层只有在存在独立格式迁移证据时才变化，本仓库当前没有这类迁移。

## 索引与 schema 格式版本（不随软件版本变化）

| 字段 | 当前值 | 强制位置 |
| --- | --- | --- |
| `config/agentos.json` `schema_version` | `2` | `src/xiaoba_agentos/config.py`：只支持 2 |
| `.runtime/releases/index.json` `format_version` | `2` | 发布索引读取方 |
| 技能索引 `schema_version`（`私人助理/skills/index.json`，内容，不在包内） | `1` | `src/xiaoba_agentos/skills.py`：技能索引必须为 1 |
| 价值文件索引 `schema_version`（`私人助理/系统/价值文件索引/index.json`，内容，不在包内） | `1` | `src/xiaoba_agentos/knowledge.py`：价值文件索引必须为 1 |
| 数据库 `PRAGMA user_version` | `7` | `src/xiaoba_agentos/store.py` `DATABASE_SCHEMA_VERSION` |

自 v1.6.1 起两份内容索引不在发布包内，也不再有 `release` 字段：内容不因发版而变。

## 冻结的兼容标识（禁止随版本改名）

改名会破坏 Obsidian 绑定、KnowLP 图或已安装入口，必须有独立迁移方案才能动。

- Vault 物理目录：`/Users/suzhizhou/Documents/私人助理/第二大脑`
- KnowLP 图目录：`…/.runtime/knowlp`
- Obsidian 根笔记文件名：`00_小八AgentOS.md`
- 已安装入口目录：`~/.agents/skills/xiaoba-agentos`、`~/.codex/skills/xiaoba-agentos`

这些名字不含版本号，不随软件版本变化；判断运行版本只看 `verify-release` 回读的发布索引与 `RELEASE.json`。

## 生成器约束

当前软件版本只允许写在 `pyproject.toml`。生成器（`create_skill_index.py`、`create_knowledge_index.py`、`build_second_brain.py`、`build_release.py`）一律通过 `scripts/agentos_version.py` 读取，不得内联版本字面量。第二大脑正文用 `{version}` 占位符，在 `_render()` 中替换。

发布目录路径不得写死在正文或笔记里：`.runtime/releases/index.json` 的当前指针决定活动发布，`verify-release` 返回实际 `release_dir`。

## 候选版本标识

验收中的候选使用 `<软件版本>-<用途>` 形式的发布标识（例如 `v1.5.2-candidate`），`RELEASE.json` 的 `status` 为 `release_candidate`。`scripts/build_release.py` 强制：发布标识必须等于软件版本或以其为前缀；只有两者完全相等时才允许写 `verified_immutable_release`。最终补丁号由发布审查决定，候选不得冒充正式版。

## 发布分档（自 v1.5.6）

每一版的验收强度由工具按改动路径算出：补丁、常规、迁移三档，规则和算法见 `docs/RELEASE_V1.5.6.md`。查看某个候选的档位：

```sh
/usr/bin/python3 scripts/release_tier.py <上一版发布包>/agentos <候选源码根>
```

正式构包必须带 `--previous-release <上一版发布包>`。补丁档不需要 `--acceptance-database` 和 `--acceptance-task-id`，构包会自己实跑全量测试，`--test-count` 必须等于实际运行的数量。其余档位仍需两个后台的验收证据。

### 第 2 步乙结项（分块验收不做）

目标架构第 2 步乙到发布分档为止。原计划的分块验收（验收证据按块绑定，改一块只重审一块）正式不做：v1.5.6 实测在真实包上省不了审；v1.6.1 技能和价值文件索引出包后，常规档改动基本只剩运行代码，分块能省的审更少。

分档规则 `config/release_policy.json` 里保留 `skills`、`knowledge` 两个顶层目录和相关条目，不要当死规则删掉：包里若再出现这两个目录，顶层结构有增减，档位直接算迁移。这和发布校验的受管目录清单是同一道防线。
