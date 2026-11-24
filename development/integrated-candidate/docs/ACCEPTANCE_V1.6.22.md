# v1.6.22 验收与回退门槛

> 说明：本文件界定 v1.6.22 的必要验收项与回退条件，不替代正式双审、构包、切换与线上回验。

承接线上 v1.6.21（活动源码清单 `6053b27de7543217dd806018a634b4ea51841d1eb73f66aa0f792731006b419c`）。

## 必要验收项

1. **静态预检（`release_preflight.py`）**：裸版本 `1.6.22`、前驱哈希 `6053b27de7543217dd806018a634b4ea51841d1eb73f66aa0f792731006b419c`、12 项 UI 资产正反向哈希（全与线上 v1.6.21 一致）、语法与零 `.pyc`、禁词检查及无作者/审查者冲突全部 `ok: true`。
2. **并行四套基线回归（`run_parallel_suites.py`）**：`core`、`ingress`、`architecture`、`monitor` 四套全部 `ok: true`（0 failures / 0 errors），包含 `test_w1_bugs_receipt_and_cli.py` 新增 3 项回归测试。
3. **5 路径前端冒烟（`browser_smoke_v1618.py`）**：30 秒内全绿。
4. **独立双审与构包**：使用 Agent 池 2 个独立审查节点（CodeBuddy + Qoder）完成独立验收，双方给出唯一 `FINAL_ACCEPTANCE: PASS`，并通过 `build_release.py` 构建不可变发布包且完成复制现场演练（故障回滚与正式切换）。
5. **用户终端切换与线上回验**：用户执行 `switch_release.py preflight` + `apply` 并重启一次监看中心（确保监看进程重新加载 v1.6.22 `workday.py`），随后运行 `verify-release` 与 `xiaoba doctor --json` 确认 `ok: true`。

## 回退条件

- `switch_release.py apply` 内置原子校验与事务回滚；如切换后 `verify-release`、`doctor` 或监看中心出现异常，可按既定发版流程回指 `.runtime/releases/v1.6.21` 并重启监看中心（本版无 UI 资产改动，无需回退监看静态文件）。
