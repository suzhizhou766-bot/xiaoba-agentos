# 故障与维护

先记录实际命令、退出码、结构化错误的 `code`（错误码）、`details`（详细证据）与局限，不记录完整环境变量或凭据。所有路径都用引号。

| 现象 | 处理 |
|---|---|
| `doctor`（环境诊断）退出 1 | 读 scope（检查范围）；本机未全局注册或缺少原生引擎会使全审计不健康。使用实际静态分析和 `mcp doctor` 区分可用能力 |
| `provider_unavailable`（引擎不可用） | 确认目标是否确需原生引擎；仅配置合法且存在的安装路径，再运行 `doctor --provider ID --json`，不伪造可用状态 |
| `ambiguous`（引擎选择不唯一） | 从实际返回候选中选定 `--provider`（引擎）或 MCP 的 `provider_id` |
| Swift 所有权辅助程序 SIGTRAP（异常退出） | 本机已实测：TMPDIR 指向中文部署目录时 `xcrun swiftc --version` 退出 -5；继承 macOS 默认 TMPDIR 时正常。包装器因此不覆盖 TMPDIR。若外部会话已覆盖它，先说明并恢复该会话原本的系统临时路径；不关闭 REA 进程所有权检查，不修改上游源码绕过 |
| 源码解析失败/超时 | 保存 partial_observation（部分观察）；缩小 `--source-path`（选择源文件）与目标范围，合理调整已公开的预算参数，不能把部分图当完整结果 |
| MCP 列出工具却调用失败 | tools/list（工具列表）是清单，不是依赖验收；运行具体工具，用错误与健康证据定位。个人客户端未加载配置时先报告未连接 |
| Frida/PTY（动态插桩/伪终端）功能失败 | 本次省略可选依赖并禁用安装脚本；这些动态工作流未验证。仅在任务确有需要且授权覆盖时另做依赖安装和验证 |
| 后台审核被认证/健康门禁阻断 | 保留同一任务及失败证据，按小八现行恢复规则处理；不手改正式技能索引，不导出密钥，不替用户批准 |

复验：`python3 /Users/suzhizhou/Documents/私人助理/工具/REA逆向/tests/smoke.py`。它只分析本部署自建样本，重写部署目录内的验证记录；不会执行样本程序。

本地维护不用 `rea update`（全局更新器），因为官方此命令仅维护拥有当前 CLI 的全局安装。本部署维护方式：先核对官方 npm 最新版本、仓库和审计，明确选定新版本后修改项目依赖并用 `配置/node/bin/npm` 配套 Node 环境执行项目内安装，保留精确版本和锁文件，再复跑 smoke test、更新 `deployment.json` 和模块清单。技能有变动还需重新走双后台审查登记。用户未要求时不创建备份、快照、全局安装、定时更新或自动发布，也不自动删除已有文件。

重装同一版本可在部署根用现有锁文件执行（会重建此处 node_modules（依赖目录））：

```sh
cd "/Users/suzhizhou/Documents/私人助理/工具/REA逆向"
PATH="/Users/suzhizhou/Documents/私人助理/配置/node/bin:/usr/bin:/bin" \
  /Users/suzhizhou/Documents/私人助理/配置/node/bin/npm ci \
  --ignore-scripts --omit=optional --no-fund --registry=https://registry.npmjs.org --cache=./cache/npm
python3 tests/smoke.py
```

日志/原始证据可能包含私有代码和路径，外传前按实际任务脱敏。本部署源码归属与安装审计在 `evidence/`，运行结果不是上游对正确性或合法性的保证。
