---
id: "rea-local-deployment"
type: "asset-card"
parent: "assets"
status: "active"
authority: false
source_path: "/Users/suzhizhou/Documents/私人助理/工具/REA逆向/deployment.json"
last_verified: "2026-10-17"
---

# REA 本地部署索引

本卡只登记用户本次明确要求部署的工具事实及入口，不是逆向分析结论或经验发布。详细状态回读原始文件。

| 项目 | 入口与状态 |
|---|---|
| REA 工具 | `/Users/suzhizhou/Documents/私人助理/工具/REA逆向`，官方 npm（软件包）`rea-agents 6.5.0`，项目内隔离安装 |
| CLI（命令行） | 工具目录 `bin/rea`，已验证可执行；复用 `私人助理/配置/node/bin/node` 24.19.0 |
| 使用说明 | 工具目录 `使用说明.md` |
| 模块清单 | `私人助理/系统/小八工作体系/modules/rea.json`，已加入 registry（模块注册表），`implementation_status=partial`（部分可用） |
| 通用技能 | `私人助理/skills/rea-reverse-engineering/SKILL.md` 及 `references/`，本地已安装并正式登记，active/callable（活动/可调用）；技能卡在 `20_能力与技能/rea-reverse-engineering.md` |
| 正式技能审查 | 最新更新任务 `35d57b79-99c8-4078-844e-101972a08ed7` 已完成，AGY/DSH 各自 FINAL_ACCEPTANCE: PASS（独立验收通过）；经 skill-publish（审核登记入口）更新索引和技能卡。初始登记任务 `73245aa1-7496-4450-93c8-7729bed8380e` 仍作为历史证据保留 |
| 调用后的引导 | 2026-10-17 新增：用户调用 REA 时，主动以选项澄清分析对象、重点与授权，再补应用名/路径；已知信息不重复问，授权未确认不读目标；详见技能 references/guided-intake.md |
| MCP（模型上下文协议） | `bin/rea mcp`，stdio（标准输入输出）；配置片段在工具目录 `config/`，当前个人客户端配置未修改 |
| 依赖与版本 | 工具目录 `deployment.json`、`package.json`、`package-lock.json`（锁文件），统一运行环境见 `私人助理/配置/环境索引.md` |
| 工具适配 | 技能 `references/agentos-workflow.md`；REA 提供证据，宿主实施，AGY/DSH 独立审查；不替换既有工具/路由 |
| 验证 | 工具目录 `evidence/verification/smoke-summary.json`：CLI 和实际 MCP 的 JS 分析、macOS 清单及 Mach-O（原生二进制格式）通过 |
| 来源与安全 | 工具目录 `evidence/source-verification.json`、`evidence/upstream/`、`evidence/verification/npm-signatures.txt`、`npm-audit.json` |
| 限制 | Hopper/Ghidra/IDA 未配置，原生反编译未验证；Frida（动态插桩）未安装；全局 doctor（环境检查）可能因此不健康 |
| 发布 | 模块 `publish_enabled=false`（关闭自动发布），无自启、定时更新或 release（发布包）变更；未更改小八全局记忆策略 |

查询词：REA、逆向工程、授权软件分析、Electron、ASAR、Mach-O、软件结构、模块关系。KnowLP（知识定位）命中后回读本卡及 deployment.json；正式 `skill-match`（技能匹配）已准入，具体能力仍按依赖和验证记录判断。该工具不在现有价值文件允许的内容根目录内，因此不向价值文件 catalog（目录清单）越界登记。
