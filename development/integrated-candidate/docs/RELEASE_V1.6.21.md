# v1.6.21 热修：修复隔离只读副本证据引用误杀与待评分状态文案

> 说明：本版承接线上 v1.6.20（活动源码清单 `dd51a05c4a49503a6ca0a128cb0a37ac0c7ca8fc978806ac2453f6463a567ddf`）。前置发布绑定已按线上 v1.6.20 完整值写入 `docs/DEPLOYMENT.json` 的 `from_source_manifest_sha256`；软件版本唯一声明点 `pyproject.toml` 已设为 `1.6.21`。最终构包、切换、UI 同步与线上采用均以正式独立双审回执及用户终端授权为准。

## 问题与根因

1. **只读副本证据引用被误杀**：
   - `CliWorklogResource._stage_materials()` 在派发审查任务前，会把候选材料从原始出箱路径（如 `.runtime/memory-outbox/<id>.md`）复制进该节点隔离工作区的只读副本目录（`任务记录/<记录>/材料/<文件名>`），并逐字节校验 `SHA-256`。
   - 派工卡明文约束节点仅读取隔离区内的只读副本并如实填写实际检查的证据路径；但 `parse_review_receipt()`（`resource_agy.py`）与 `AgentWorkspace.workday_inputs()`（`agent_workspace.py`）仅核对原始出箱路径，未把本轮刚通过 `SHA-256` 核验的只读副本路径映射回任务卡原路径，导致严谨守规矩的节点（如 `AGY-Flash` 切到 `Gemini 4 Hydrogen (XHigh)`）产出合格回执仍被误判为「证据引用未匹配任务卡」。
2. **待评分进行中状态文案误导**：
   - `monitor/monitor-review-center.js` 将待价值评分的 `started` 状态统一显示为「派工中断或待续」，导致点击开始/重试评分后后台节点正在正常执行期间刷新页面时误像刚点即中断。

## 本版变更范围

1. **`development/review-ingress/xiaoba_review/workday.py`**：新增统一公共函数 `strip_ref_anchor()`、`is_dispatch_card_ref()`、`staged_evidence_map()`、`verified_staged_evidence_map()` 与 `normalize_review_evidence_refs()`，仅对本轮经 `SHA-256` 校验且非符号链接的 `材料/` 只读副本路径做规范路径映射（兼容绝对/相对路径、反引号与中英文行号后缀），同时保留防伪造边界（拒绝未列于 `card.evidence_refs`、篡改/符号链接及仅引用 `派工卡-*.md` 的回执）。
2. **`development/review-ingress/xiaoba_review/resource_cli.py`**：`CliWorklogResource._turn()` / `run()` / `run_task()` 将刚校验完成的只读副本映射表透传至回执校验与结果归一化。
3. **`development/review-ingress/xiaoba_review/resource_agy.py`**：`parse_review_receipt()` 接入公共 `normalize_review_evidence_refs()` 并接受可选 `staged_refs` 参数。
4. **`development/review-ingress/xiaoba_review/agent_workspace.py`**：`workday_inputs()` 复用同一套 `normalize_review_evidence_refs()` 与 `verified_staged_evidence_map()`，使每日台账合格判定与回执验收口径一致。
5. **`monitor/monitor-review-center.js` 与 `review_ui_assets/manifest.json`**：将 `VALUE_DISPATCH_LABEL.started` 由「派工中断或待续」改为「评分进行中或待续」，同步更新 `manifest.json` 中该单一前端文件的 `before_sha256`（绑定线上 v1.6.19/v1.6.20 实值 `94ab2cab27e5cf499d4587ad21dfd70b815dbded52d96757013c75dc4ec25844`）与 `after_sha256`（`eb7f2924cd877a2d3b6f32331fdfa4c5df70f7530ce41097d90e1b1fb14ebf87`）。
6. **回归测试**：`development/review-ingress/tests/test_w1_bugs_receipt_and_cli.py` 新增 5 项正反例与端到端闭环测试；同步更新 `test_review_ui_frontend_contract.py`、`test_card59.py` 与 `test_deployment_predecessor_binding.py`。

## 数据与发布边界

- 数据库结构版本保持 schema 7，不新增表、列、索引或迁移步骤。
- 沿用现有 4 条已执行 `DEPLOYMENT.json` 步骤（逐字节不变）。
- UI 资产仅变更 1 项：`monitor-review-center.js`（其余 11 项逐字节不变）；切版后需执行 `xiaoba review-ui-install --apply` 并重启一次监看中心，使新 `xiaoba_review` 模块与前端文案一并生效。
- 验收与回退细则见 [ACCEPTANCE_V1.6.21.md](ACCEPTANCE_V1.6.21.md)。
