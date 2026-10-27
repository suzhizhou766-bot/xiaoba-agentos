---
id: "experience-88292e83-e5d5-4b67-b5f5-871cacf584a0"
type: "experience"
parent: "experiences"
status: "active"
authority: false
source_task_id: "88292e83-e5d5-4b67-b5f5-871cacf584a0"
last_verified: "2026-10-07"
---

# 辅助模型特性档案（第三版，新增 Qwen 3.8 Max）

**本版取代初版候选 cff221aa-6ddc-47e5-8758-e3035b6b4e0a 和第二版候选 5a886524-e06c-42de-a20f-403333826334**（内容是第二版加上 Qwen 3.8 Max，前两版不必采用）。

用途：主力 Agent 给辅助模型派工前先读本档案，按“任务内容与模型特性的匹配度”分工。配套规则见候选《大工程拆分与按模型特性派工》（ecfaa454-8860-4acb-b054-6a437eea1251）。

当前辅助模型：DeepSeek V4.1 Flash、Qwen 3.8 Flash、Gemini 3.8 Flash、Qwen 3.8 Max（2026-10-07 用户新增）。

**阅读须知：本版没有任何本机实测评分。** 第一节是第三方独立测试机构的跑分，第二节是厂商自报成绩，两者都是 2026-10-06 联网检索所得，整理者没有复现；“分工倾向”是据此做的推断。本机实测记录出现后以实测为准。

## 一、第三方独立跑分（未经本机复现）

主要来自 Artificial Analysis（下称 AA）的同条件对比，DeepSeek 用最高推理档，Gemini 用高推理档，Qwen 测的是开放权重版 Qwen3.8-Flash-Next。**用户实际使用的若是托管版 Qwen3.8-Flash，表现可能与此不同。**

| 项目 | DeepSeek V4.1 Flash | Qwen3.8-Flash-Next | Gemini 3.8 Flash | 说明 |
|---|---|---|---|---|
| AA 综合智能指数 | 39 | 40 | 41 | 三者基本同档 |
| LiveBench 总分 | 81.1% | 76.2% | 75.8% | 榜首 83.4%；DeepSeek 明显领先另两个 |
| 自动化流程 AutomationBench | 69% | 56% | 60% | 按步骤操作软件完成任务 |
| 终端编码 Terminal-Bench 4.0 | 27% | 25% | 20% | 最难一档，三者都低 |
| 终端编码 Terminal-Bench 2.1（Vals 独立测） | 74.5% | 未查到 | 未查到 | 厂商自报 90.6，独立测低 16 分 |
| 科学计算编码 SciCode | 52% | 51% | 57% | |
| 知识型办公任务 Briefcase（Elo） | 1420 | 1583 | 1203 | 产出报告、表格等交付物 |
| 真实职业任务 GDPval（Elo） | 1600 | 1633 | 1435 | |
| 专业文档处理 GDP.pdf | 13% | 16% | 21% | |
| 长上下文推理 AA-LCR | 84% | 80% | 81% | |
| 高难知识推理 HLE | 39% | 38% | 48% | |
| **知识可靠性 Omniscience**（越高越好，可为负） | **−5** | **−10** | **30** | 负分表示答错多于答对 |
| 输出速度（字符/秒，token） | 227 | 55 | 242–332 | |
| 首个答案出现前的等待 | 约 10 秒 | 约 39 秒 | 约 20 秒 | 含推理时间 |
| 每百万输入/输出价格（美元） | 0.30/1.20 | 0.15/0.47 | 0.75/3.75 | |
| 跑完整套测试的花费（美元） | 477 | 363 | 1623 | Qwen 单价最低但单任务用的 token 最多 |
| 上下文 | 1M | 256k（开放权重版） | 1M | 托管版 Qwen3.8-Flash 为 1M |

另：DeepSeek V4.1 Flash 在 AA-Omniscience 上的“幻觉率”为 96.5%——**不知道答案时几乎总是照样作答，而不是说不知道**。

从这些数字读出的特点：

- **DeepSeek V4.1 Flash**：综合最强（LiveBench 领先约 5 分）、响应最快、按步骤自动化和长上下文推理最好、价格居中。最大的短板是知识可靠性：凭记忆回答事实、接口用法时容易自信地答错。厂商自报成绩明显高于独立测试。
- **Gemini 3.8 Flash**：知识可靠性远好于另两个，高难知识推理、科学计算编码、专业文档处理最好，输入类型最全。短板是自动化流程和办公交付类任务偏弱，价格是 DeepSeek 的 2.5–3 倍，开始作答前等待较久。
- **Qwen 3.8 Flash**：知识型办公交付（报告、表格类）最强，单价最低。短板是很慢（速度约为另两个的四分之一，开始作答前约等 39 秒）、知识可靠性最差、自动化流程最弱。

### Qwen 3.8 Max（2026-10-07 检索，未经本机复现）

它和前三个不是一档：闭源旗舰模型，综合智能更高，但慢得多、贵得多。测的是 0902 版本。

| 项目 | Qwen 3.8 Max | 对照 |
|---|---|---|
| AA 综合智能指数 | 45 | 三个 Flash 为 39–41 |
| 输出速度（token/秒） | 约 38 | 四个里最慢；DeepSeek 227 |
| 首个字出现前的等待 | 约 2.7 秒 | |
| 每百万输入/输出价格（美元） | 2.00/6.00 | DeepSeek 的 5–7 倍 |
| AA 测试中平均每个任务花费（美元） | 5.41 | DeepSeek 0.27，约 20 倍 |
| 上下文 | 约 98 万，单次输出上限约 13 万 | |
| 可接收的输入 | 文字、图片 | |
| 是否开放权重 | 否 | DeepSeek、Qwen Flash 开放 |
| 终端编码 Terminal-Bench 2.1（Vals 独立测） | 67.4% | 厂商自报 86.6，独立测低 19 分；DeepSeek 独立测 74.5% |
| SWE-bench（Vals 独立测） | 85.6% | |
| 函数级编程题 LiveCodeBench（Vals 独立测） | 87.9% | |
| 研究生水平科学题 GPQA Diamond（Vals 独立测） | 93.7% | |

厂商自报（仅供参考）：SWE-bench Pro 67.7、DeepSWE 1.1 为 56.6（低于 DeepSeek V4.1 Flash 自报的 74.2）、按指令办事 IFBench 82.8、文档解析 OmniDocBench 92.1、操作电脑界面 OSWorld-Verified 86.1、论文复现 PaperBench 93.0。厂商称它的强项是终端代理、文档理解、按指令办事和操作电脑界面。

AA 的评语：智能在同价位里领先，但“明显偏慢，而且非常啰嗦”（跑完整套测试输出了约 1.9 亿 token）。AA 页面上它的各分项成绩（含知识可靠性、长上下文、自动化流程）没有公开，无法与三个 Flash 逐项对比。

从这些数字读出的特点：综合能力和知识水平是四个里最高的，厂商强调的“按指令办事”和“读文档”对约束多、不能越界的任务有利；但真实仓库级的大改动并不比 DeepSeek V4.1 Flash 强（自报的 DeepSWE 反而更低，独立测的终端编码也更低），而且最慢、最贵、输出最长。

## 二、厂商自报成绩（仅供参考，普遍高于独立测试）

| | DeepSeek V4.1 Flash | Qwen 3.8 Flash | Gemini 3.8 Flash |
|---|---|---|---|
| 真实仓库修改 DeepSWE v1.1 | 74.2% | 58.7% | 74%（高推理档） |
| 终端多步任务 Terminal-Bench 2.1 | 90.6 | 未查到 | 90.8% |
| 函数级编程题 LiveCodeBench | 未查到 | 91.9 | 89.5% |
| SWE-bench Pro | 未查到 | 62.5 | 61.6% |
| 可接收的输入 | 文字、图片 | 文字、图片、视频 | 文字、图片、音频、视频、PDF |

第三方动手评测对 DeepSeek V4.1 Flash 的观察：它“很快搭出看起来合理的框架，但底层逻辑经常经不起检查”（魔方求解只是把打乱步骤倒放，状态跟踪出错）；SVG 图形质量明显不如同档对手。

**没有查到可靠数据的方面**：四者是否严格按约束办事（只有 Qwen 3.8 Max 的厂商自报 IFBench）、大范围跨文件重构、是否会多改无关代码、中文任务表现；Aider 多语言编码榜和 LMArena 网页开发榜上没有查到这三个模型的成绩。

## 三、本机既有记录（关于 DeepSeek Harness，不是关于模型本身）

- 经 AgentOS 后台调用（300 秒超时）：约 2 KiB 的工作任务约 50 秒完成；约 11 KiB 时一次超时、一次 189 秒完成；约 15 KiB 两次超时。出处：`70_复利规则/经验/6628a31c-a3de-41e2-84ed-1a588dba6f88`。这反映的是后台调用的时间限制，不代表模型读不了长上下文；用户在桌面端手动转交时是否同样受限，未实测。
- 作为审核方较严格：v1.4.10 验收第一轮指出三份文档漏写必需说明，经核实属实。出处：`70_复利规则/交付记录/监看中心-2026-10-05`。

## 四、分工倾向（推断，待本机实测验证）

- **DeepSeek V4.1 Flash —— 首选的编码与操作执行者。** 适合：有测试或类型检查兜底的编码、终端操作、批量机械修改、按步骤的自动化、需要读大量上下文的任务、对照清单逐条核对；需要快速来回迭代的工作。派工时的防护：要求它**读代码和文档而不是凭记忆**，凡涉及事实、版本、接口用法的结论都要给出处；它交回的逻辑要重点复核。不适合：需要真正推演状态的算法、图形与界面观感、任何无法自动验证的事实性内容。
- **Gemini 3.8 Flash —— 事实核查与看图读文档。** 适合：需要对照截图、视频、PDF 判断的工作；查资料、核对事实和接口用法；科学计算类代码；审阅另两个模型的产出里有没有事实错误。不适合：长串的自动化操作、需要频繁快速迭代的小任务（等待久、价格高）。
- **Qwen 3.8 Flash —— 成文的交付物。** 适合：报告、说明文档、表格、数据整理、测试用例这类自成一体、可以慢慢做的成品；预算敏感的大批量文字工作。不适合：需要快速迭代的工作（很慢）、跨文件代码改动、事实性要求高的内容（须另行核对）。
- **Qwen 3.8 Max —— 约束多、不能出错的精细活，以及复核。** 适合：必须严格按清单办、不能多改一处的定点修改；需要读懂长文档或截图后再动手的任务；对另外三个模型的产出做第二遍审阅；需要较强推理的单点难题。不适合：大批量或需要快速来回迭代的工作（最慢、单任务花费约为 DeepSeek 的 20 倍）、可以交给 Flash 模型并有自动验收兜底的常规编码。用它时任务卡要明确要求“只输出结果，不要长篇解释”。
- **四者都不派、由主力 Agent 完成**：需要整体理解和设计取舍的；跨多个文件须保持一致且没有自动检查兜底的；涉及凭据、权限、对外请求的；出错难以发现或难以回退的。

## 五、本机实测评分记录

（空。每项外派任务经主力 Agent 核实后在此追加一行。）

| 日期 | 模型 | 任务类型与规模 | 是否一次通过 | 出了什么问题 | 评分 1–5 及理由 | 证据位置 |
|---|---|---|---|---|---|---|

## 六、资料来源（2026-10-06 检索）

- https://artificialanalysis.ai/models/qwen3-8-max
- https://benchlm.ai/models/qwen3-8-max
- https://www.mindstudio.ai/blog/qwen-3-8-max-benchmarks-explained
- https://www.datacamp.com/blog/qwen3-8-max
- https://artificialanalysis.ai/models/comparisons/deepseek-v4-1-flash-vs-gemini-3-8-flash
- https://artificialanalysis.ai/models/comparisons/deepseek-v4-1-flash-vs-qwen3-8-flash-next
- https://benchlm.ai/benchmarks/livebench
- https://benchlm.ai/models/deepseek-v4-1-flash
- https://www.datacamp.com/blog/deepseek-v4-1-flash
- https://www.mindstudio.ai/blog/deepseek-v4-1-flash-benchmarks
- https://deepinfra.com/blog/deepseek-v4-1-flash-api-benchmarks
- https://www.datacamp.com/blog/deepseek-v4-1-flash-vs-gemini-3-8-flash
- https://www.respan.ai/articles/qwen-3.8-models-comparison
- https://www.cometapi.com/what-is-qwen3-8-flash/
- https://codersera.com/blog/qwen-3-8-flash-complete-guide-2026/
- https://benchlm.ai/models/gemini-3-8-flash
- https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/guides/gemini-3-8-flash
- https://www.buildfastwithai.com/blogs/gemini-3-8-flash-review

整理：主力 Agent（Claude Code），应用户 2026-10-06 的要求（“对于那三个模型的能力描述你可以联网搜索然后调查”，并同意将其整理入第二大脑供其他 Agent 派工时查阅）。上述网页为检索工具返回的摘要与页面内容，整理者未逐一核对原始基准报告。
