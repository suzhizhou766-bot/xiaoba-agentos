# v1.6.7 候选：冻结任务能恢复、能放弃

基线为已上线 v1.6.6（2026-11-03）。这一版只修 v1.6.6 上线后真实使用中发现的问题，不加别的改动。

本版没有新增数据库结构（结构版本仍为 7），没有新增切换步骤。`docs/DEPLOYMENT.json` 的前置源码清单哈希改为已上线 v1.6.6 的值 `3790c0be…`。

## 一、冻结任务恢复后，失败那一侧能重跑

**v1.6.6 的问题。** 用户在 Agent 池重新测试失败节点、再 `task-resume` 之后，任务回到队列，但失败那一侧马上又被拒绝：“原任务步骤已失败并留有记录，不自动重跑”，任务再次冻结。原因是节点工作区按“任务编号＋步骤码”认记录，恢复后还是同一条记录，而失败过的记录按规定不自动重跑。

**改法。** 任务被恢复过时，Worker 把恢复次数带给节点调用，节点工作区为这一次恢复开一条新记录（`任务编号-步骤码-resume次数`）。派工卡里的任务编号和步骤码不变，回执校验照旧。没有恢复过的任务，调用方式和记录名都不变。

实现：`orchestrator.py` 的 `_run_agent` 写入 `resume_generation`；`adapters.py` 的 `PoolCliAdapter` 在恢复过时传 `attempt`；`xiaoba_review/resource_cli.py` 和 `resource_agy.py` 的 `run_task` 用它区分记录。反例：`tests/test_resume_reruns_failed_node.py`、`review-ingress/tests/test_card41_materials.py` 的 `test_resumed_task_gets_a_fresh_record_for_the_failed_step`。

## 二、冻结任务可以放弃，排队任务可以取消

- **放弃冻结任务：** `task-abandon 任务编号 --freeze-hash 冻结哈希`。任务结束为 `failed`，冻结记录、已有结论和回执都保留，事件记 `task.abandoned`。冻结哈希必须和当前记录一致。
- **取消排队任务：** `task-cancel 任务编号`。只取消还在排队、没有开始的任务（`pending`、`retry`），正在跑的不取消，事件记 `task.cancelled`。
- **页面：** 监看中心的冻结任务卡上，“恢复”旁边多了“放弃”按钮，确认框里核对任务编号和冻结哈希，一次只放弃一个。接口是 `/api/review/frozen/abandon`，在监看中心的 `monitor_core/modules/frozen.py`。

反例：`tests/test_task_abandon.py`；监看中心 `tests/test_frozen.py` 新增两条。

## 三、页面文字跟上 v1.6.6 的做法

- 审查材料面板的标题从“DSH＋开放第二方 · 独立审查材料”改为“Agent 池 · 独立审查材料”。
- 冻结任务卡不再说“这一侧现在没有新鲜的合格探测”。池节点要在冻结之后重新测试通过才能恢复，卡片和命令行拒绝时都这样写。

## 四、怎么上线

三处要一起更新，缺一处“放弃”按钮就会报错：

1. 小八本体：按 `docs/RELEASE_SWITCH.md` 构包、演练、切换。
2. 监看中心后台：更新 `私人助理/工具/小八监看/monitor_core/modules/frozen.py`，重启监看中心。
3. 页面：切换后运行 `review-ui-install` 安装新页面。

## 五、本版没做

- 发布验收接受宿主补票。
- 退役内置 WorkBuddy 引擎（它的事件流上限仍是 1 MiB）。
- v1.6.6 发布说明的文字问题：它是历史记录，不改；本版说明避开同样的写法。
- 审查员在 v1.6.6 验收中提的其它非阻断意见，原文在 `私人助理/工作区/小八AgentOS-v1.6.6-发版/reports/round11～13`。
