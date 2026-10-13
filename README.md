# 小八 AgentOS v1.6.1（定版）

2026-10-13 上线的定版快照。私有仓库，仅作留存，不是开发仓库。

## 内容

| 目录 | 是什么 |
|---|---|
| `release/` | v1.6.1 不可变发布包的原样拷贝（232 个文件加 `FILES.sha256` 清单）。包含 `agentos/`（源码、脚本、配置、测试、文档）、`interface/`（已安装入口）、`second-brain/`（发布生成的 30 篇规则与发布状态笔记）、`RELEASE.json`。可用 `FILES.sha256` 逐文件核对 |
| `content/skills/` | 上线时的 8 个技能，连同 `catalog.json` 与 `index.json`。自 v1.6.1 起技能不在发布包内，线上位置是 `私人助理/skills/` |
| `content/价值文件索引/` | 价值文件的目录与索引。原文不在这里（它们是项目材料） |

## 这一版的状态

- 发布分三档（补丁、常规、迁移），档位由上一版包内的工具按改动路径算出。
- 第二大脑里的内容笔记、技能、价值文件都不随发布：改它们不用发版。
- 改技能走“草稿、审查、发布”：`xiaoba skill-review-prompt`、`xiaoba submit`、`xiaoba skill-publish`。
- 详见 `release/agentos/docs/RELEASE_V1.6.1.md`、`RELEASE_V1.6.0.md`、`RELEASE_V1.5.6.md`。

## 没有放进来的

运行数据库、凭据、配置里的模型线路覆盖文件、第二大脑里的经验与内容笔记、项目素材。发布包本身不含这些。

文件里有本机的目录路径（`/Users/…`），所以这个仓库保持私有。测试文件里形如 `sk-…`、`AKIA…EXAMPLE` 的字符串是测试用的假凭据，不是真的。
