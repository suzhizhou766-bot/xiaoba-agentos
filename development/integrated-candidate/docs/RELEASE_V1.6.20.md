# v1.6.20 热修：修复思考强度节点执行开放审查/价值评分任务卡误报配置不符

> 说明：本版承接线上 v1.6.19（活动源码清单 `adafe3b2f2d9804164d7b06947ea56f4e83640657faff0cb0dc42a238e0c3fd8`）。前置发布绑定已按线上 v1.6.19 完整值写入 `docs/DEPLOYMENT.json` 的 `from_source_manifest_sha256`；软件版本唯一声明点 `pyproject.toml` 已设为 `1.6.20`。最终构包、切换与线上采用均以正式独立双审回执及用户终端授权为准。

## 问题与根因

- v1.6.13（卡 66）为 Agent 池节点引入了可选字段 `reasoning_effort`（思考强度），并将其纳入 `node_runtime.spec()` 的规格核对。
- 但 `xiaoba_review/agent_pool.py` 中的 `resource_plan()` 在为开放审查与待价值评分生成冻结任务卡时遗漏了 `reasoning_effort` 字段。
- 导致凡是配置了思考强度的池节点（例如 `CodeBuddy=high`、`Qoder=xhigh`），即使刚生成的全新任务卡，在 `backends.execute()` 执行时都会因 `card['resource_plan'].get('reasoning_effort')`（`None`）与 `spec(source)['reasoning_effort']`（`'high'`/`'xhigh'`）不符，报错“冻结任务卡与当前节点配置不符，不能改用当前入口执行旧任务”。

## 本版变更范围

1. **`development/review-ingress/xiaoba_review/agent_pool.py`**：`resource_plan()` 补齐 `reasoning_effort`（与 `node_runtime.spec()` 保持完全一致的条件：`if 'reasoning_effort' in state`）。
2. **`development/review-ingress/xiaoba_review/backends.py`**：`execute()` 的冻结任务卡与当前节点核对补充反向检查 `frozen_plan.get('reasoning_effort') != current_spec.get('reasoning_effort')`，确保不仅能放行配置一致的新卡，也能在冻结后清空或修改思考强度时继续严格拒绝执行旧卡。
3. **`development/review-ingress/tests/test_card66_reasoning.py`**：新增 2 项回归测试（`test_resource_plan_preserves_reasoning_effort_matching_spec` 与 `test_frozen_card_allows_matching_effort_and_blocks_modified_or_cleared_effort`），覆盖设置/未设置思考强度与冻结后修改/清空思考强度的正反例。

## 数据与发布边界

- 数据库结构版本保持 schema 7，不新增表、列、索引或迁移步骤。
- 沿用现有 4 条已执行 `DEPLOYMENT.json` 步骤（逐字节不变）。
- 本版不改动任何监看前端或包外 UI 资产（12 项 UI 清单哈希与线上 v1.6.19 完全一致，无需再次同步 UI）。
- 验收与回退细则见 [ACCEPTANCE_V1.6.20.md](ACCEPTANCE_V1.6.20.md)。
