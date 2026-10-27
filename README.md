# 小八 AgentOS

## 当前云端开发

2026-10-27 用户授权在本仓库开展实现并提交。新增 [DSH＋开放第二方审查接入](development/review-ingress/README.md)：身份任务卡、外部/后台回执、来源核对、可靠归集与运行监看。验证记录见 [TEST-RESULTS](development/review-ingress/TEST-RESULTS.md)。

这是开发代码，尚未完成真实 DSH 模型联调或本机生产切换。活动 v1.6.4 与冻结 v1.6.5 的原始材料均保留在 architecture-package-20261027，未被本次扩展改写。

## 历史 v1.6.1 定版快照

2026-10-13 上线的定版快照。以下目录说明仅描述历史留存，不代表当前开发或生产版本。

## 内容

| 目录 | 是什么 |
|---|---|
| `release/` | v1.6.1 不可变发布包的原样拷贝（232 个文件加 `FILES.sha256` 清单）。包含 `agentos/`（源码、脚本、配置、测试、文档）、`interface/`（已安装入口）、`second-brain/`（发布生成的 30 篇规则与发布状态笔记）、`RELEASE.json`。可用 `FILES.sha256` 逐文件核对 |
| `content/skills/` | 上线时的 8 个技能，连同 `catalog.json` 与 `index.json`。自 v1.6.1 起技能不在发布包内，线上位置是 `私人助理/skills/` |
| `content/价值文件索引/` | 价值文件的目录与索引。原文不在这里（它们是项目材料） |
| `monitor/` | 小八监看中心与 Key 池代理的定版快照（本机仓库 `私人助理/工具/小八监看` 的提交 `24c6eb9`，2026-10-14），含冻结任务列表与单个恢复。监看中心不随 AgentOS 发布，在本机独立仓库开发，这里只是留存。`keypool.json`、`run/`、`logs/`、`monitor.log` 被 `.gitignore` 排除，不在这里 |

## 这一版的状态

- 发布分三档（补丁、常规、迁移），档位由上一版包内的工具按改动路径算出。
- 第二大脑里的内容笔记、技能、价值文件都不随发布：改它们不用发版。
- 改技能走“草稿、审查、发布”：`xiaoba skill-review-prompt`、`xiaoba submit`、`xiaoba skill-publish`。
- 详见 `release/agentos/docs/RELEASE_V1.6.1.md`、`RELEASE_V1.6.0.md`、`RELEASE_V1.5.6.md`。

## 没有放进来的

运行数据库、凭据、配置里的模型线路覆盖文件、第二大脑里的经验与内容笔记、项目素材。发布包本身不含这些。

文件里有本机的目录路径（`/Users/…`），所以这个仓库保持私有。测试文件里形如 `sk-…`、`AKIA…EXAMPLE` 的字符串是测试用的假凭据，不是真的。

