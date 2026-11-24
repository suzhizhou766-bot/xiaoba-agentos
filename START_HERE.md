# 小八 AgentOS 源码导航（START_HERE）

> 唯一代码地图（Wave 3 单源收敛版）。线上运行哪个版本，以本机稳定入口与发布指针为准，**不看目录猜测**。

## 1. 线上运行链路（只读区，禁止热改）
- 稳定命令入口：`~/.agents/skills/xiaoba-agentos/scripts/xiaoba` 与 `verify-release`
- 当前线上版本指针：`~/Documents/小八AgentOS/.runtime/releases/index.json`（只通过 `switch_release.py` 原子切换，严禁手改 `.runtime/releases/`）
- 线上监看部署副本：`~/Documents/私人助理/工具/小八监看/`（通过 `xiaoba review-ui-install --apply` 从已发版包同步）

## 2. 本仓库权威目录（单源真相）
| 目录 | 职责 | 说明 |
|---|---|---|
| `development/integrated-candidate/` | AgentOS 核心引擎、CLI、状态机、发版脚本与核心测试 | 发版构包主入口；`review_ui_assets/` 仅保留 3 个裁决中心私有文件与 `manifest.json` |
| `monitor/` | 小八监看中心**唯一权威源码**（含 `monitor_core/`、`xiaoba-monitor.html`、`monitor-keypool.js`、`monitor-review-center.js`、`tests/`） | 构包时自动投影进 `review_ui_assets/` 与 `xiaoba_monitor_shared/`；`monitor_core/lifecycle.py` 现阶段显式冻结 |
| `development/review-ingress/` | Agent 池、开放审查接入口与前端脚本（`agent-pool*.js`） | 与核心同库事务边界 |
| `development/architecture-center/` | 架构中心与模块规格（`MODULES.json`、`modules/`） | 升级提案与只读规格 |
| `scripts/release_pipeline/` | 收编的 `75P`/`75Q`/`75R` 发版预检、彩排与部署工具链 | 统一指向顶层 `monitor/`，不再散落各版本工作区 |

## 3. 八条常用命令
1. **查线上健康**：`"$HOME/.agents/skills/xiaoba-agentos/scripts/xiaoba" doctor --json`
2. **验线上版本**：`"$HOME/.agents/skills/xiaoba-agentos/scripts/verify-release"`
3. **查待审队列**：`"$HOME/.agents/skills/xiaoba-agentos/scripts/xiaoba" memory-pending-index`
4. **重建源码清单**：`<pinned-python3.14> -B development/integrated-candidate/scripts/create_manifest.py`
5. **发版静态预检**：`<pinned-python3.14> -B development/integrated-candidate/scripts/release_preflight.py --agentos-root development/integrated-candidate --expected-version <X.Y.Z> --predecessor-sha256 <SHA>`
6. **四套测试并行回归**：`<pinned-python3.14> -B development/integrated-candidate/scripts/run_parallel_suites.py .`
7. **监看前端冒烟**：`<pinned-python3.14> -B development/integrated-candidate/scripts/browser_smoke_v1618.py`
8. **同步监看 UI（仅用户终端）**：`"$HOME/.agents/skills/xiaoba-agentos/scripts/xiaoba" review-ui-install --apply`

> 术语消歧：经验初审推荐 `review --stage initial`（兼容旧名 `delivery`），与用户成品交付 `task-delivery` 无关；正式经验复用总分为 `formal_reuse_score`（事件账本投影），区别于历史列 `legacy_reuse_count`。详细发版SOP与安全红线见：`~/Documents/私人助理/Playbook/AgentOS发版流程.md` 与本仓 `AGENTS.md`。
