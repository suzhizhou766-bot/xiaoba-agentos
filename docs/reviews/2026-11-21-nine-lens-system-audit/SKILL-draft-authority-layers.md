---
name: authority-layers
description: 改小八 AgentOS 任何文件前先确认自己在改哪一层权威。用于避免改快照、改错监看树、把候选当成线上、或把审查结论写进 release/second-brain。
---

# 先认层，再动手

## 触发条件

准备修改、删除、或把结论“写入第二大脑/技能”时，若目标路径属于本仓库或本机小八相关目录，先停下来认层。

## 权威顺序（高 → 低）

1. **用户最新确认**（对话原话、监看裁决、拓扑里标明的用户原话）
2. **在役构型**：本机运行根 `current_release` 指针（不在本 Git 仓库）
3. **意图底座**：`docs/architecture-topology/`（AGENT_READER、DECISIONS、CONTRACTS）
4. **候选实现**：`development/integrated-candidate/`、`development/review-ingress/`
5. **活动监看源码**：`architecture-package-20261027/shared/工具/小八监看/`（目录名像冻结，但是活动树）
6. **历史对照（默认只读）**：`release/`、`monitor/`、`implementation/active-v1.6.4`、`candidate-v1.6.5`

冲突时：报告差异，**不要用低层去改高层，也不要用快照冒充现行**。

## 检查步骤

1. 写出本次要改的路径属于上表哪一层。
2. 若路径在第 6 层：默认停止。只有用户明确要求改快照时才动，并在说明里写“破坏定版哈希”。
3. 若题目是“监看”：先确认不是 `monitor/` 快照；活动树在架构包 `shared/工具/小八监看/`，本机还有一份不在仓库里。
4. 若题目是“第二大脑”：`release/second-brain/` 是 v1.6.1 发布快照；现行 Vault 在用户本机。技能草稿放 `docs/reviews/...` 或本机 `skills/`，禁止写入 `release/second-brain/`。
5. 若 README / CURRENT-DEVELOPMENT / 入口 SKILL 互斥：以拓扑裁决顺序 + 运行根为准，并开文档修复，不要静默选一份旧的继续干。

## 证据要求

- 引用具体路径与（若有）DECISIONS 权威顺序。
- 写明本轮有没有读过运行根；没读过就不要断言线上版本号。

## 边界

- 本技能不授权删除历史树。
- 本技能不替代用户裁决经验是否入库。
- 与 `liveness-is-not-identity` 正交：那条管进程判定，这条管改哪里。
