# v1.6.4：架构第 4、5 步并入

启动：2026-10-20，用户同意“把第 4、5 步并进 v1.6.3，走冻结、双审、构包、演练，做完给切换命令”。主力：Claude Code（小克）。基线：线上 v1.6.3。

## 位置

- `candidate/`：候选源码根。由 `work/merge/`（git，只给主力看来历）合成：v1.6.2 → v1.6.3 → 合入 DeepSeek（第 4 步返修版）→ 合入 Qwen（第 5 步），两次合并都无冲突。
- `interface/SKILL.md`、`artifacts/`：构包与测试需要的旁置材料。
- `work/`：冻结、审查、演练、构包脚本（由 v1.6.3 的同名脚本改来）。
- `reports/`、`审查轮次/`：测试日志、审查与演练证据；每轮作废的材料归档在 `审查轮次/`。

## 集成时主力补的

- `tests/test_guardrail_marks.py` 的无测试底线集合改为只剩 `B07`（Qwen 回执 §8.3）。
- 第二大脑治理笔记模板 `docs/second_brain/v152_extra_nodes.json` 的矛盾 1、2 措辞（Qwen 回执 §9.2）。
- 版本号推进到 v1.6.4，发布说明 `docs/RELEASE_V1.6.4.md`。

## 状态

| 步 | 状态 |
|---|---|
| 候选与全量测试 | 928 项，3.14.7 与 3.9.6 冻结前各一遍均通过 |
| 分档 | 常规档 |
| 冻结 | 第 1、2 轮源码清单 `4dd0b292…`，第 3 轮 `d27e2c38…`；审查拆成四份代码范围（retirement_code、retirement_tests、rules_docs、guardrail_marks） |
| 第 1 轮双审 | 未出有效结论：说明里贴的测试数据含答案格式样例句，被调度器当成本题约束，见 `审查轮次/第1轮/结论.md`；已给样例句加〈〉并在生成时用同一检测函数校验 |
| 第 2 轮双审 | 三份双过；rules_docs 两个后台都不通过（认证失败的措辞漏改），已修，见 `审查轮次/第2轮/结论.md` |
| 第 3 轮双审 | 源码清单 `d27e2c38…`，四份 AGY、DSH 各自通过 |
| 演练 | 一次通过：11 阶段故障全部已退回；切换后结构 7、6 张策略表行数不变；9 张技能卡逐张只改路径 |
| 集成审查 | AGY、DSH 各自通过 |
| 正式包 | FILES `b961be41…`，新现场复核通过 |
| 线上切换 | 2026-10-20 主力按用户指示执行，回验通过，见 `RELEASE_COMPLETE.md` |

## 下一版待办

- `test_peer_repair_retirement.test_mixed_budget_and_environment_failure_is_not_let_through` 负载下偶发失败（10 遍 1 次），既有问题，待查根因（建议加负载复跑并记录分类分布）。
- `config/agentos.json` 的 `evolution` 死配置段与 `config.py` 对应属性（DeepSeek 登记）。
- `scripts/build_second_brain.py` 约 158–166 行一段不生效的旧恢复策略正文（Qwen 登记）。
- 底线标记覆盖率偏低；`test_memory_publication_gate.py` 还有 5 项未标；哈希不符测试有两处重复（Qwen 登记）。
- 沿用 v1.6.3 遗留：沙箱不拒读文件元数据、测试写死 3.14 版本号、VERSIONING 历史行停在 v1.5.1、盘点工具技能误报。

## 记下的事

- 3.14 全量连跑 10 遍有 1 遍偶发 `test_peer_repair_retirement.test_mixed_budget_and_environment_failure_is_not_let_through` 失败；单项加压 64 次、与线上 v1.6.3 并排全量 4 遍都没复现，涉及的代码本版未改，判为既有的负载敏感测试，登记后续，已在审查说明里披露。
- 主力排查时写的一个计数脚本缺 `__main__` 保护，测试里的子进程把整套测试层层重跑，已全部终止；只在临时目录建库，线上与候选都核实未受影响。教训：给测试套件包壳的脚本必须加 `if __name__ == "__main__"`。
