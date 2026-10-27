# v1.6.4 上线完成

2026-10-20。用户指示“剩下的所有内容均由你亲自完成”，正式切换由主力（Claude Code）执行：预检 ok → apply `ok: true`、`activated: true`、写入 301、读回 v1.6.4、`restart_verified: true`，Worker 3889 → 85429。没有创建备份。

## 主力独立只读回验

- 两处入口 `verify-release` 读回 v1.6.4，FILES `b961be41…`；首行仍为固定 3.14。
- Worker 85429 命令行为固定 3.14 解释器；`status`：代码与清单指纹和磁盘一致，`active_strategy` 已不在输出里。
- `doctor`：ok、可接单；数据库结构 7，无问题；`interpreter.running_is_pinned: true`；无警告。
- 线上库 6 张策略表原样保留：strategies 3、evaluation_cases 24、evaluations 48、evaluation_runs 96、promotion_gates 4 行，`user_version` 7。
- `xiaoba evolve` 打印退役提示，退出码 2。
- `content-notes-sync`：已同步，没有要写的。第二大脑 `10_治理与裁决/故障自愈与任务恢复.md` 已是新措辞（认证失败不可重试），检索命中。

## 验收经过（详见 README.md 与 `审查轮次/`）

- 第 1 轮：说明里贴的测试数据含答案格式样例句，被调度器当成本题约束，未出有效结论。
- 第 2 轮：rules_docs 两个后台都不通过（认证失败措辞漏改），修。
- 第 3 轮：四份代码范围双过；演练一次通过；集成审查双过；正式包新现场复核通过；线上只读计划通过。

演练现场 2 个已移到废纸篓 `小八演练现场-v1.6.4-20261020/`，可恢复。
