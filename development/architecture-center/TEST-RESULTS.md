# 实现与验证记录

日期：2026-10-27。范围是云端开发扩展，不是本机生产发版验收。

| 检查 | 结果 |
|---|---|
| 升级中心单元、真实 HTTP 和 Chromium 按钮流程 | 28 通过 |
| 既有开放审查接入口测试 | 20 通过 |
| 原生 switch_release 隔离回归 | 40 通过 |
| 既有运行监看 test_monitor.py | 31 通过、2 失败（下述 Mac 路径依赖） |
| HTML 结构核对 | 8 视图、69 节点说明；引用、边界及两轮核验路径通过 |
| JavaScript / Python 语法 | 通过 |
| 原始材料包 | 3,831 文件、50,819,807 字节，清单与完整性复核通过 |

新增测试覆盖：提案不写活动源码；文件差异与权限接续记录；模块范围、路径穿越、软链接与未登记文件；源码/候选/正式包漂移；执行权互斥和过期接续；检查点交接；语法检查的范围；缺正式包/执行器拒绝；版本不符；预检过期；一次性确认；发布幂等与全工作区单一发布；实际失败不记成功；审计链损坏停止写入；固定原生入口漂移、无 JSON 或无真实回读不报告成功。

真实 HTTP 验证了页面注入令牌、跨源和无令牌拒绝、无预检不能发布。真实 Chromium 使用隔离夹具执行“查看差异 → 正式包预检 → 按钮启用 → 显示具体目标 → 用户确认 → 收取回读 → 版本记录”，执行器只调用一次。该执行器明确是测试替身，没有生产切换。

另在集成运行器启动的独立空运行目录中，实际操作了八个拓扑视图、节点编辑规范、保存待发布、显示差异、候选检查、未接执行器时按钮禁用、原监看新增标签和刷新恢复、390px 手机界面。页面 JavaScript 错误为 0。截图和隔离 TEST 任务留在云端测试工作区，不进入仓库或生产库。

既有监看两项失败：

- `ReviewLifecycleTests.test_prefers_own_previous_backend_then_legacy_service`：期望监听 PID 为测试进程，实际为 None。
- `LocalToolsTests.test_takes_over_existing_controller_and_stops_services_on_close`：无法接管测试控制器，健康未达到 ok。

只读核查显示这两条既有路径都固定调用 `/usr/sbin/lsof`，而当前 Linux 只有 `/usr/bin/lsof`，前者不存在。未改动冻结快照、替换路径或削弱接管判断来制造通过；需在目标 Mac 环境补测。这两项不属于新增升级模块，新增模块测试和原生切换隔离测试均通过。

复跑：

```bash
python3 -B -m unittest discover -s development/architecture-center/tests -v
python3 -B -m unittest discover -s development/review-ingress/tests -v
```

原生隔离回归从 `architecture-package-20261027/implementation/candidate-v1.6.5` 执行 `python3 -B -m unittest discover -s tests -p test_switch_release.py -v`。既有监看回归从包内 `shared/工具/小八监看` 执行同样命令，文件名为 `test_monitor.py`。

本云端未配置用户 Mac 发布执行器，未运行真实 DSH、未执行正式构包或生产切换，未批准经验。在线托管页明确只读；真正保存与发版需同源本机监看。模块规范分类已建立，旧核心的大文件尚未物理迁移到新的模块目录。两轮核验、任务综合评分、自动择优及交付自动回流仍待各自实现。
