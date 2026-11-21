# 小八 AgentOS · 九透镜系统复盘（2026-11-21）

审查开始：2026-11-21（Asia/Shanghai）。基线：`origin/main` = `c846381`（合并 PR #59）。

## 与 PR #60 的关系

同日另有 [PR #60](https://github.com/suzhizhou766-bot/xiaoba-agentos/pull/60) 按同一九项框架做了**可复现缺陷收口**：监管进程把“存活 PID”当成身份，并已给出最小修复与技能草稿 `liveness-is-not-identity`。

本文件是**系统层复盘**：不重复修那个洞，而是回答——若把九项框架对准整个 AgentOS，真正反复付钱的根因是什么、用户真正想要什么、什么该简化、什么该写进预防技能。

PR #60 自己也写明：30 天里反复被改的是结算和评分（`store.py`），监看启停只是当前 HEAD 上云端能直接复现的一条边。本复盘从那句话继续往下挖。

## 一句话结论

小八不是“缺功能的 Agent 操作系统”，而是**用户主权下的多 Agent 经验复利系统**。当前最大的本质问题不是再加节点，而是：

1. **多份事实源并存**（拓扑 / 候选代码 / 发布快照 / 本机运行根 / 第二大脑），Agent 经常修错层；
2. **外部审查轮次在扮演 CI**，导致同一意图连修多轮；
3. **经验状态机与评分门过宽**，把日常任务拖进高仪式链路。

最小有价值的下一步不是再扩拓扑，而是：**单一权威入口 + 契约测试替掉多轮口头验收 + 按任务风险分层走经验链路**。

## 本目录文件

| 文件 | 用途 |
|---|---|
| [九透镜系统复盘.md](./九透镜系统复盘.md) | 九项全文 |
| [metrics.json](./metrics.json) | 可核对数字 |
| [SKILL-draft-authority-layers.md](./SKILL-draft-authority-layers.md) | 技能草稿：先认层 |
| [SKILL-draft-gate-scope.md](./SKILL-draft-gate-scope.md) | 技能草稿：新门最小范围 |
| [SKILL-draft-acceptance-is-not-ci.md](./SKILL-draft-acceptance-is-not-ci.md) | 技能草稿：验收≠CI |
| [第二大脑同步草案-防复发.md](./第二大脑同步草案-防复发.md) | 待用户初审后拷入本机 Vault |

监管身份修复与 `liveness-is-not-identity` 草稿见 PR #60 目录 `docs/reviews/2026-11-21-nine-point-audit/`。
