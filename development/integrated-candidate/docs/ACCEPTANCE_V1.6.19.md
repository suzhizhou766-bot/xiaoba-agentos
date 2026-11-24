# v1.6.19 验收与回退门槛

> 说明：本文件界定 v1.6.19 的必要验收项与回退条件，不替代正式双审、构包、切换与线上回验。

承接线上 v1.6.18（活动源码清单 `5bbc6c9be717c962554b71d504a2fb66c3e0aa477f74a9fe7f89198966fcfd70`）。

## 必要验收项

1. **静态预检（`release_preflight.py`）**：裸版本 `1.6.19`、前驱哈希 `5bbc6c9be717c962554b71d504a2fb66c3e0aa477f74a9fe7f89198966fcfd70`、12 项 UI 资产正反向哈希、语法与零 `.pyc`、禁词检查及 Agent 池节点只读核验全部 `ok: true`。
2. **并行四套基线回归（`run_parallel_suites.py`）**：`core`、`ingress`、`architecture`、`monitor` 四套全部 `ok: true`（0 failures / 0 errors），包含 `test_w3_single_source.py`、`test_w4_category_o.py`、`test_w4_category_q.py`、`test_w4_category_p.py` 及线上 SQLite 历史事件链零写入只读回放验证。
3. **5 路径前端冒烟（`browser_smoke_v1618.py`）**：30 秒内完成，总览 CSP + 同源加载 `monitor-keypool.js` 与 `monitor-review-center.js`（均通过 `node --check` 且 HTTP 200）+ 旧表折叠、池抽屉清 DOM 令牌 + 真实写请求、重启 403 `Command+R` 提示 + 保留非令牌 403、审核队列清 meta 令牌 + 正常加载、12 项 UI 资产正向前像校验全绿。
4. **独立双审与构包**：使用 Agent 池至少 2 个独立审查节点（与作者/执行者无冲突）完成独立验收，双方给出唯一 `FINAL_ACCEPTANCE: PASS`，并通过 `build_release.py` 构建不可变发布包且完成三阶段复制现场演练（故障回滚、正式切换、UI 同步）。
5. **用户终端切换与包外 UI 同步**：用户执行 `switch_release.py preflight` + `apply` 与 `xiaoba review-ui-install --apply` 并重启监看中心，随后运行 `verify-release` 与 `xiaoba doctor --json` 确认 `ok: true`。

## 回退条件

- `switch_release.py apply` 内置原子校验与事务回滚；如切换后 `verify-release`、`doctor` 或监看中心出现异常，可按既定发版流程回指 `.runtime/releases/v1.6.18` 并在 `私人助理/工具/小八监看` 依据 `manifest.json` 还原 `xiaoba-monitor.html`、`monitor_core/modules/system.py` 并移走新增的 `monitor-keypool.js`、`monitor-review-center.js`。
