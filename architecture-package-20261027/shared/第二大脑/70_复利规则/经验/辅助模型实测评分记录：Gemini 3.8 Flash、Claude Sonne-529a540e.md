---
id: "experience-529a540e-137a-4e12-8967-472905c767fd"
type: "experience"
parent: "experiences"
status: "active"
authority: false
source_task_id: "529a540e-137a-4e12-8967-472905c767fd"
last_verified: "2026-10-07"
---

# 辅助模型实测评分记录：Gemini 3.8 Flash、Claude Sonnet 4.6、GPT-OSS 120B（2026-10-07，经 Antigravity 命令行）

用途：给《大工程拆分与按模型特性派工》（ecfaa454-8860-4acb-b054-6a437eea1251）第 7、8 条提供第一批实测评分，并补充《辅助模型特性档案（第三版）》（88292e83-e5d5-4b67-b5f5-871cacf584a0）里 Gemini 3.8 Flash 一栏。档案第三版的内容来自公开资料；本条是本机实测，两者冲突时以本条为准，但只在下面写明的任务范围内有效。

## 派工结论

| 任务类型 | 首选 | 说明 |
|---|---|---|
| 边界清楚的编码、修 bug、SQL、规则计算、结构化提取 | Gemini 3.8 Flash | 准确度与 Claude Sonnet 4.6 持平，额度耐用；每题要等 1.5–6 分钟 |
| 同上，但要快，且数量少 | Claude Sonnet 4.6（Antigravity 内） | 快 2–4 倍；与 GPT-OSS 合计约 25 次调用就耗尽额度 |
| 100 KB 以上日志或长文的检索、计数 | Gemini 3.8 Flash 或 Claude Sonnet 4.6 | GPT-OSS 120B 不要派 |
| 多文件改动 | Gemini 3.8 Flash 或 Claude Sonnet 4.6 | GPT-OSS 120B 两次里一次只改一个文件就停 |
| 短小、单文件、无长上下文的任务 | GPT-OSS 120B 可用 | 算法题、逻辑题、SQL 实测全对 |
| 需要运行命令的任务、真实大型代码库、设计判断 | 未测，留给主力 Agent | 见"未验证"一节 |

## 评分（1–5 分，依据是脚本对照标准答案的判分结果）

| 模型 | 评分 | 有效运行 | 结果 | 单题耗时 |
|---|---|---|---|---|
| Gemini 3.8 Flash (High) | 5 | 第一轮 10 题各 1 次；第二轮 6 题各 3 次 | 第一轮 10/10；第二轮 18 次里 17 次满分 | 55–390 秒 |
| Claude Sonnet 4.6 (Thinking) | 5 | 第二轮 13 次（额度耗尽，少跑 5 次） | 13 次全部满分 | 45–145 秒 |
| GPT-OSS 120B (Medium) | 3 | 第二轮 12 次（额度耗尽，少跑 6 次） | 平均 0.70 | 45–130 秒 |

第二轮分题得分（满分 1，括号内为有效次数）：

| 题目 | Gemini 3.8 Flash | Claude Sonnet 4.6 | GPT-OSS 120B |
|---|---|---|---|
| 多文件重构，4 个文件，14 项隐藏测试 | 1.00 (3) | 1.00 (3) | 0.50 (2) |
| cron 下次触发时间函数，16 个用例 | 1.00 (3) | 1.00 (2) | 1.00 (2) |
| 142 KB 日志多步推理加精确计数 | 0.93 (3) | 1.00 (2) | 0.20 (2) |
| 六人座位推理加容斥计数 | 1.00 (3) | 1.00 (2) | 1.00 (2) |
| 12 人工资表，6 条规则 | 1.00 (3) | 1.00 (2) | 0.50 (2) |
| SQLite 查询：并列第二高、中位数 | 1.00 (3) | 1.00 (2) | 1.00 (2) |

第一轮 10 题（仅 Gemini 3.8 Flash）：表达式解析器、4 个隐藏 bug 的修复、排名推理、概率与大数取模、中文聊天记录提取为 JSON、33 KB 日志检索、不存在的标准库函数（幻觉测试）、读文件改代码、带约束的中文写作、提示注入抵抗。全部通过。

## 出过的问题

- Gemini 3.8 Flash：142 KB 日志里有一次把 38 条错误数成 37 条，另两次数对。
- GPT-OSS 120B：142 KB 日志题版本号和回滚人留空；多文件重构有一次只改了一个文件，并声称其余"后续再适配"；两次无视"不许调用工具"去运行命令，输出为空，按 0 分计。
- Claude Sonnet 4.6：有效运行内没有失分。判分脚本曾因注释里出现 float 一词误扣 1 项，人工核对后改回满分。

## 调用方式与已核实的坑

- 调用：`/Users/suzhizhou/Documents/AGY/agy -p "<任务>" --model "Gemini 3.8 Flash (High)" --print-timeout 600s`；要改文件加 `--mode accept-edits`；`agy models` 列出可用模型。
- 地区：FlClash 选"美国 02"节点时，所有 Gemini 型号报 `Agent execution terminated due to error`，日志 `~/Library/Logs/Antigravity/language_server.log` 里的真实原因是 `FAILED_PRECONDITION (code 400): User location is not supported for the API use`。换成"日本 01"或"日本 02"后正常。Claude 与 GPT-OSS 不受这项检查影响。
- 无界面模式不能授权运行命令：模型一旦尝试运行命令，整次调用返回空输出。Gemini 第一轮 10 题里有 6 题因此为空。任务卡里要写明"不允许运行命令"，需要测试结果时把结果贴进任务卡。
- 每次调用有约 50 秒固定开销，只回两个字也要 56 秒。
- 额度：Claude Sonnet 4.6 与 GPT-OSS 120B 合计约 25 次有效调用后报 `RESOURCE_EXHAUSTED (code 429): Individual quota reached`，约 4 小时后恢复。Gemini 额度独立，当天 35 次调用未触顶。

## 未验证

- 需要实际运行命令的任务、真实大型代码库、设计判断类工作，三个模型都没测。
- 每题最多 3 次运行，Claude 与 GPT-OSS 多数题只有 2 次，样本小。
- 只测了 Gemini 3.8 Flash 的 High 档；Medium、Low 档未测。
- 额度共用是从两个模型同时报 429 推断的，没有官方说明。
- DeepSeek V4.1 Flash、Qwen 3.8 Flash、Qwen 3.8 Max 不在这次测试内。

## 证据位置

`/Users/suzhizhou/Documents/小八AgentOS/work/model-bench-20261007/`：`round1/`、`round2/` 各含题目（`p/`）、原始输出（`out/`）、出题与判分脚本（`gen.py`、`grade.py`）、标准答案（`round2/key/`）、隐藏测试（`round2/hidden_shop.py`、`round2/cron_tests.py`）和重构题的工作目录（`round2/ws/`）。在 `round2/` 下运行 `python3 grade.py -v` 可复算第二轮分数；脚本把因额度耗尽而为空的运行也记作 0 分，本文表格已把这些运行剔除。

## 来源

主力 Agent（Claude Code）2026-10-07 在本机出题、运行并判分；用户（苏之舟）当日要求"写进小八 agentos，让别的 Agent 也能看得到这条分配的记忆"。
