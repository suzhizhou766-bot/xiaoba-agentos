# v1.6.21 验收与回退门槛

> 说明：本文件界定 v1.6.21 的必要验收项与回退条件，不替代正式双审、构包、切换与线上回验。

承接线上 v1.6.20（活动源码清单 `dd51a05c4a49503a6ca0a128cb0a37ac0c7ca8fc978806ac2453f6463a567ddf`）。

## 必要验收项

1. **静态预检（`release_preflight.py`）**：裸版本 `1.6.21`、前驱哈希 `dd51a05c4a49503a6ca0a128cb0a37ac0c7ca8fc978806ac2453f6463a567ddf`、12 项 UI 资产正反向哈希（11 项不变，仅 `monitor-review-center.js` 从 `94ab2cab...` 升级至 `eb7f2924...`）、语法与零 `.pyc`、禁词检查及无作者/审查者冲突全部 `ok: true`。
2. **并行四套基线回归（`run_parallel_suites.py`）**：`core`、`ingress`、`architecture`、`monitor` 四套全部 `ok: true`（0 failures / 0 errors），包含 `test_w1_bugs_receipt_and_cli.py` 新增 5 项回归测试。
3. **5 路径前端冒烟（`browser_smoke_v1618.py`）**：30 秒内全绿。
4. **独立双审与构包**：使用 Agent 池 2 个独立审查节点（CodeBuddy + Qoder）完成独立验收，双方给出唯一 `FINAL_ACCEPTANCE: PASS`，并通过 `build_release.py` 构建不可变发布包且完成复制现场演练（故障回滚与正式切换，含 UI 精确同步验证）。
5. **用户终端切换与线上回验**：用户执行 `switch_release.py preflight` + `apply`、`xiaoba review-ui-install --apply` 并重启一次监看中心（确保监看进程重新加载 v1.6.21 `xiaoba_review` 与更新后的 `monitor-review-center.js`），随后运行 `verify-release` 与 `xiaoba doctor --json` 确认 `ok: true`。

## 回退条件

- `switch_release.py apply` 内置原子校验与事务回滚；如切换后 `verify-release`、`doctor` 或监看中心出现异常，可按既定发版流程回指 `.runtime/releases/v1.6.20`，并将 `工具/小八监看/monitor-review-center.js` 恢复至 v1.6.20 对应文件（SHA-256 `94ab2cab27e5cf499d4587ad21dfd70b815dbded52d96757013c75dc4ec25844`）后重启监看中心。
