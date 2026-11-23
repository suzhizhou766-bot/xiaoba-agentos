# v1.6.18 验收与回退门槛

> 说明：本文件界定 v1.6.18 的必要验收项与回退条件，不替代正式双审、构包、切换与线上回验。

承接线上 v1.6.17（活动源码清单 `8979e53a51b6f082499c92b2445b6d170580d0e5a0b2fad66cf7bebec8304f4a`）。

## 必要验收项

1. **静态预检（`release_preflight.py`）**：裸版本 `1.6.18`、前驱哈希 `8979e53a51b6f082499c92b2445b6d170580d0e5a0b2fad66cf7bebec8304f4a`、UI 正反向哈希、语法与零 `.pyc`、禁词检查及 Agent 池节点只读核验全部 `ok: true`。
2. **并行四套基线回归（`run_parallel_suites.py`）**：`core`、`ingress`、`architecture`、`monitor` 四套全部 `ok: true`（0 failures / 0 errors），包含 `test_w2_g_monitor_security_ui.py`、`test_w1_bugs_receipt_and_cli.py`、`test_w2_h_value_queue_and_cli.py`、`test_w2_i_j_release_pipeline.py`。
3. **5 路径前端冒烟（`browser_smoke_v1618.py`）**：30 秒内完成，总览 CSP + 旧表折叠、池抽屉清 DOM 令牌 + 真实写请求、重启 403 `Command+R` 提示 + 保留非令牌 403、审核队列清 meta 令牌 + 正常加载、9 项 UI 资产正向前像校验全绿。
4. **独立双审与构包**：使用 Agent 池至少 2 个独立审查节点（或按既有规则 1 池节点 + 1 独立未参与宿主）完成独立验收并通过 `build_release.py` 构建不可变发布包。
5. **用户终端切换与包外 UI 同步**：用户执行 `switch_release.py --apply` 与 `xiaoba review-ui-install --apply`，随后运行 `verify-release` 与 `xiaoba doctor --json` 确认 `ok: true` 且 `系统当前态.md` 已剥离旧 `2026-10-10 上线确认` 尾段。

## 回退条件

- `switch_release.py --apply` 内置原子校验与事务回滚；如切换后 `verify-release`、`doctor` 或监看中心出现异常，可按既定发版流程回指 `.runtime/releases/v1.6.17` 并在 `私人助理/工具/小八监看` 依据 `manifest.json` 的 `before_sha256` 还原 `xiaoba-monitor.html` 与 `monitor_core/web.py`。
