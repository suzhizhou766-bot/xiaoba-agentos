# v1.6.18 监看安全、价值审查、实测缺陷修复与发版防复发收口

> 说明：本版承接线上 v1.6.17（活动源码清单 `8979e53a51b6f082499c92b2445b6d170580d0e5a0b2fad66cf7bebec8304f4a`）。前置发布绑定已按线上 v1.6.17 完整值写入 `docs/DEPLOYMENT.json` 的 `from_source_manifest_sha256`；软件版本唯一声明点 `pyproject.toml` 已设为 `1.6.18`。最终构包、切换、UI 同步与线上采用均以正式独立双审回执及用户终端授权为准。

## 本版变更范围

1. **监看前端安全与交互收口（G 类）**
   - `agent-pool.js` 与 `page.js` 启动读取页面令牌后立即从 DOM 删除 `data-page-token` 与 `<meta name="xiaoba-page-token">`，仅驻留闭包内存。
   - `monitor_core/web.py` 令牌失配 403 返回明确操作指引（`监看服务已重启或页面会话已过期，请按 Command+R 刷新页面后重试`），前后端同步友好改写且不误伤非令牌 403（如仅限本机访问）；`review_ui_install.py` 与 `review_ui_assets/manifest.json` 新增 `monitor_web.py` 同步资产。
   - Agent 池节点详情抽屉留空掉线时限文案对齐为 `任务默认 300 秒 / 测活默认 90 秒`。
   - `xiaoba-monitor.html` 旧 6 列历史线路表改为默认折叠 `<details id="legacyAgentsPanel">`，移除 `agent-pool-native.js` 中的旧标题运行时改名补丁。

2. **核心价值审查、CLI 对齐与 Wave 1 实测三缺陷修复（H 类 + Wave 1 试飞补丁）**
   - `cli.py` 将 `memory-value-list`、`memory-value-dispatch`、`repair-status`、`workday-read`、`review-packet` 纳入 `read_only_or_managed` 白名单，并为常用只读子命令补齐 `--json` 兼容参数；`SKILL.md` 明确 `verify-release` 为独立同级脚本。
   - `value_queue.py` 与 `orchestrator.py` 新增受控价值审查派审（CLI 显式 `memory-value-dispatch` + Worker 空闲轮询受 `worker.auto_dispatch_value_review` 默认关闭开关保护，每条候选同哈希至多派一次，不伤害普通任务结算）。
   - **回执引用锚点兼容**：`resource_agy.py` 兼容证据引用中的 `#L...` 行号锚点，并在已命中真实任务卡证据时放行补充 `派工卡-*.md`，报错直指具体缺失/非法条目。
   - **敏感词误杀收窄**：`memory.py` 的 `_sensitive_reason()` 收窄 `authorization`、`secret_field` 与 `environment_secret` 规则，不再误拦正常中文技术论述、函数调用与 `demo-/test-/wrong-` 合成夹具，同时继续拦截带前缀 YAML/配置凭据、含符号密码、JWT、云厂商访问键、Bearer 令牌与已知前缀密钥。
   - **CLI 失败退出码可观测**：`resource_agy_cli.py` 与 `resource_cli.py` 在 `execution_evidence` 记录整数 `exit_code`，不留原始 stdout/stderr。

3. **第二大脑当前态与发版流水线防复发加固（I / J 类）**
   - `build_second_brain.py` 全面采用插件化 Agent 池表述并按 `HISTORY_VERSIONS.json` 动态解析 `last_verified`；`switch_release.py` 合并 `系统当前态.md` 时自动剥离旧 `2026-10-10 上线确认` 残留尾段，同时逐字节保留真实用户补充。
   - `build_release.py` 去除 `RELEASE.json` 中硬编码旧双后台名称，改为按验收证据动态写入池节点，新增 `stamp_release_docs` 与 `--parallel-suites` 并行 4 套基线开关。
   - `release_preflight.py` 新增 Python AST / Shell 语法、零 `.pyc` 字节码残留、发布禁词、Agent 池只读就绪检查与推迟项台账对账；`build_release.build()` 构包前自动执行语法与字节码门禁。
   - 新增 `release_review_orchestrator.py`（硬配置错立即停、瞬时网络 EOF/TLS 退避 15 秒并在独立目录最多重试 2 次 + 后台心跳；第 2+ 轮增量差异包、终轮 P0-only 与 3 轮未收敛熔断）及 `browser_smoke_v1618.py`（30 秒内验证 5 条高频监看与安全路径）。

## 新增回归测试 ← 踩坑日期映射

- `development/review-ingress/tests/test_w2_g_monitor_security_ui.py` ← 2026-11-22 监看页面审查（DOM 令牌残留、重启 403 提示含糊、抽屉掉线时限双口径分叉、旧 6 列历史线路表未折叠）。
- `development/review-ingress/tests/test_w1_bugs_receipt_and_cli.py` ← 2026-11-23 Wave 1 实测（回执证据 `#L` 行号锚点与补充派工卡被误拒、`cli_failed` 丢失退出码）。
- `development/integrated-candidate/tests/test_w2_h_value_queue_and_cli.py` ← 2026-11-22/23 实测（敏感词误杀技术文章、只读子命令缺白名单与 `--json`、价值待审队列无显式派审入口）。
- `development/integrated-candidate/tests/test_w2_i_j_release_pipeline.py` ← 2026-11-22 发版复盘（第二大脑当前态与 `RELEASE.json` 残留旧双后台、发版静态预检与字节码拦截、审查失败分级重试与 3 轮熔断、30 秒内 5 路径前端冒烟）。

## 数据与发布边界

- 数据库结构版本保持 schema 7，不新增表、列、索引或迁移步骤。
- 沿用现有 4 条已执行 `DEPLOYMENT.json` 步骤（逐字节不变）。
- 切换与监看 UI 同步（含 `xiaoba-monitor.html` 与 `monitor_web.py`）由用户在终端按授权执行，不创建未授权备份。
- 验收与回退细则见 [ACCEPTANCE_V1.6.18.md](ACCEPTANCE_V1.6.18.md)。
