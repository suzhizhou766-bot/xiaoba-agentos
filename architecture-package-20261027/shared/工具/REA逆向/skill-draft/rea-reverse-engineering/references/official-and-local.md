# 官方来源与本地能力

核对日期：2026-10-16（北京时间）。官方仓库 https://github.com/morluto/rea ，网站 https://rea.tools ，npm（软件包仓库）包 `rea-agents`，仓库 README 明确链接此包并提醒仿冒来源风险。不能因为仓库同名或复制 README 就安装。

本次 npm 最新版本为 6.5.0；README 的 main（主分支）核对提交为 `2c1dee16df7c3839eb1bad115e10d102d6641ea2`。主分支与已发布包有差异（例如依赖清单），不能把主分支全部功能等同于安装版本。实际命令参数以本机 6.5.0 `--help`（帮助）和返回证据为准。

官方文档：

- 安装：https://github.com/morluto/rea/blob/main/docs/installation.md
- CLI：https://github.com/morluto/rea/blob/main/docs/cli.md
- MCP：https://github.com/morluto/rea/blob/main/docs/mcp-contracts.md
- 客户端：https://github.com/morluto/rea/blob/main/docs/client-configuration.md
- 安全：https://github.com/morluto/rea/blob/main/SECURITY.md
- 责任和许可边界：https://github.com/morluto/rea/blob/main/DISCLAIMER.md

官方入门是 `npx rea-agents setup`，全局 CLI 安装是 `npm install --global rea-agents`，项目内安装也受支持。官方持久 MCP 注册固定精确版本，例如 `npx -y rea-agents@6.5.0 mcp`。本机改用已安装的绝对路径包装器，避免每次从网络解析版本。

支持 Node.js 22.x >=22.19、24.x >=24.11、26+；不支持 23、25 或预发布版本。本机复用 `私人助理/配置/node/bin/node` 24.19.0，不新增解释器、不改系统 Python。`package-lock.json` 锁定依赖，安装禁用生命周期脚本并省略可选 Frida（动态插桩）。这会限制 Frida/可能依赖原生终端组件的工作流，未测试的动态能力不声明可用。

| 能力 | 本地状态/依赖 |
|---|---|
| CLI 与 MCP | 已安装；MCP 需客户端显式加载，当前会话未全局注册 |
| JS / Electron 静态分析 | 自建双文件样本经 CLI 和实际 MCP 工具调用验证；Node 与 macOS Swift 所有权辅助程序 |
| macOS `.app` 清单、Mach-O | 自建应用包静态验证；部分命令使用 Xcode Command Line Tools（苹果命令行开发工具） |
| 原生反编译 | Hopper / Ghidra / IDA 未配置，不声明通过 |
| 网页、运行时、网络捕获 | 视操作需要 Chrome/CDP（浏览器调试协议）、Frida、捕获工具及额外授权；本次未测试 |
| Android / .NET / WASM | REA 有对应工具；依赖随操作变化，本次未测试，不因 tools/list（工具列表）存在就声明可运行 |

macOS 是官方支持平台之一；Hopper 官方说明要求 macOS 12+，Ghidra 另需匹配的 JDK（Java 开发工具包）。商业引擎须自备合法许可。REA 的桥接令牌和私有临时目录降低意外风险，但官方明确这不是沙箱，同一系统用户下的恶意进程不在防护边界内。

安装包校验、仓库信息、npm 元数据和官方文档原始内容保存在 `工具/REA逆向/evidence/`。本地 `source-verification.json`、`npm-signatures.txt` 和 `npm-audit.json` 分别说明完整性、已验证签名与当时已知漏洞审计，不能据此保证没有未知漏洞。
