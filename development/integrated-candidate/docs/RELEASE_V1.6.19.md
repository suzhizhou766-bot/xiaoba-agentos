# v1.6.19 单源架构收敛、历史兼容收口与核心前后端模块化重构

> 说明：本版承接线上 v1.6.18（活动源码清单 `5bbc6c9be717c962554b71d504a2fb66c3e0aa477f74a9fe7f89198966fcfd70`）。前置发布绑定已按线上 v1.6.18 完整值写入 `docs/DEPLOYMENT.json` 的 `from_source_manifest_sha256`；软件版本唯一声明点 `pyproject.toml` 已设为 `1.6.19`。最终构包、切换、UI 同步与线上采用均以正式独立双审回执及用户终端授权为准。

## 本版变更范围

1. **单源架构收敛与仓库治理（Wave 3：W3-M1 ~ M5、W3-N1）**
   - 确立仓库顶层 `monitor/` 为小八监看唯一权威源码目录；删除开发工作树中重复副本，`create_manifest.py`、`build_release.py` 与 `review_ui_install.py` 在构包及清单生成时自动将监看资产投影到 `review_ui_assets/` 与 `xiaoba_monitor_shared/`。
   - 清理旧快照目录与硬编码历史路径，将发版编排与恢复脚本（75P / 75Q / 75R）统一收编至 `scripts/release_pipeline/`。
   - 新增仓库根目录导航文档 `START_HERE.md`，汇总权威目录、术语边界与 8 条高频运维命令。

2. **术语清晰化与 CLI 分组（Wave 4 O 类：W4-O1 ~ O3）**
   - `review` / `review-list` / `review_flow` / `bridge` 新增 `initial` 作为经验初审阶段标准别名，100% 兼容旧名 `delivery`（数据库 canonical 值不变，Schema 7 无变更）；在 `OPERATIONS.md` 与 `START_HERE.md` 置顶消歧，避免与用户成品交付 `task-delivery` 混淆。
   - 显式区分历史计数字段 `legacy_reuse_count` 与事件账本正式复用分 `formal_reuse_score`（保留原有字段契约不变）。
   - `xiaoba --help` 将全部 81 条子命令按「日常使用 / 经验裁决与记忆管理 / 内部维护与发版 / 已退役占位」四类清晰分组，不修改任何子命令名。

3. **历史只读兼容收口与退役定案（Wave 4 Q 类：W4-Q1 ~ Q3）**
   - 新增 `xiaoba_agentos/legacy_compat.py`，统一收拢历史事件与历史具名审查节点的只读兼容逻辑与全量历史事件链只读回放自检（线上实盘 2,933 条事件 0 写入完整回放通过）。
   - 统一登记 `RETIRED_COMPONENTS`（`WorkBuddy` 内置引擎、`Headroom` 自动重启、旧 `8797` 独立端口与同伴自动修复），保持全部防复活底线测试（B01–B24）完好。
   - 明确 Schema 7 六张策略进化退役表维持零读写占位，不做高风险删表迁移。

4. **核心前后端模块化拆分与 `lifecycle.py` 冻结（Wave 4 P 类：W4-P1 ~ P4）**
   - `cli.py` 拆分为 `cli_groups/{parser_spec,daily_ops,memory_ops,maintenance_ops}.py`，保留全部 monkeypatch 兼容与 81 条命令行为不变。
   - `orchestrator.py` 的 `run_once()` 拆分为 `_stage_preflight_and_claim`、`_stage_prepare_and_execute`、`_stage_cover_and_validate`、`_stage_judge_and_settle` 四个显式阶段方法。
   - `store.py` 拆分为 `store_common.py`、`store_events.py`、`store_tasks.py`、`store_memory.py` 四个职责清晰的模块，同时在 `store.py` 保持 `DATABASE_SCHEMA_VERSION = 7` 供静态发版检查；接入层将 `xiaoba_review/review_store.py` 设为单一 SQLite 写边界权威模块，`xiaoba_review/store.py` 保持同对象模块别名。
   - `monitor/xiaoba-monitor.html` 从 4,447 行收敛至 2,187 行，将 Key 池实时拓扑与裁决中心脚本原样拆出为同源外链 `monitor/monitor-keypool.js`（1,065 行）与 `monitor/monitor-review-center.js`（1,193 行）；`monitor_core/modules/system.py` 新增精确白名单路由与同源 CSP 放行；UI 同步清单扩展至 12 项并与线上 `v1.6.18` 实盘哈希逐项衔接。
   - `monitor/monitor_core/lifecycle.py` 显式冻结（零改动），由自动化哈希测试看守。

## 新增回归与护栏测试

- `development/integrated-candidate/tests/test_w3_single_source.py`：单源资产投影、零旧快照路径、发版脚本 AST 有效性与 `START_HERE.md` 约束。
- `development/integrated-candidate/tests/test_w4_category_o.py`：初审别名 `initial`/`delivery`、复用分双口径注解与 81 条命令四类分组。
- `development/integrated-candidate/tests/test_w4_category_q.py`：历史只读兼容出口、事件链零写入回放与退役组件防复活边界。
- `development/integrated-candidate/tests/test_w4_category_p.py`：核心 Python 拆分结构断言、监看拆出脚本语法与严格 CSP 放行、`lifecycle.py` 冻结指纹看守。

## 数据与发布边界

- 数据库结构版本保持 schema 7，不新增表、列、索引或迁移步骤。
- 沿用现有 4 条已执行 `DEPLOYMENT.json` 步骤（逐字节不变）。
- 切换与监看 UI 同步（本次实际变更 4 项：`xiaoba-monitor.html`、`monitor_core/modules/system.py` 及新增 `monitor-keypool.js`、`monitor-review-center.js`）由用户在终端按授权执行，不创建未授权备份。
- 验收与回退细则见 [ACCEPTANCE_V1.6.19.md](ACCEPTANCE_V1.6.19.md)。
