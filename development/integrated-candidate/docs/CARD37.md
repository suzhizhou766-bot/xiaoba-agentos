# 卡 37：生产入口与宿主回执

本卡只修改开发源码；没有部署、修改 monitor.json 或批准经验。

## 工作记录与送审

原生 CLI 的 `submit --no-review` 创建工作任务。宿主身份可用 `--host-id / --host-entry / --host-model` 指定；这三项只标识宿主，不能声明“没干活”。

实际参与者通过 `task-work-record TASK --actor-id ID --role author|executor|collaborator` 追加记录，不能撤销。页面对已有原生任务调用 `/api/agent-pool/work` 时自动记协作参与者和 agent_runs。Worker 的协作任务则从 workflow 与 agent_runs 取参与者。

产物送审使用 `submit "审查说明" --work-task WORK_TASK --memory-kind audit`。指定补票宿主时加上述三个宿主参数。Worker 从来源任务、作者记录和实际执行记录推导排除名单；提交方不能传一个空名单覆盖它们。已有 collaborate-review 的工作关联也使用同一排除规则。页面送审读取候选自己的作者和来源任务记录。

## 宿主提交

一个池节点完成后，任务保持 pending，last_error 标明 awaiting_host。后续轮询不重跑该节点、不增加 attempts。宿主需要提交实际回执：

```sh
xiaoba task-host-complete TASK_ID --receipt /absolute/path/host-receipt.json
```

旧任务没有宿主身份时，先用 `task-host-bind TASK_ID --host-id ID --host-entry ENTRY --host-model MODEL` 追加不可覆盖的身份审计记录，不改冻结题面。身份绑定不声明“没干活”，资格仍按实际参与记录判断。

审查回执示例（TASK、HASH、HOST、时间戳按实际填写；内容必须由指定宿主实际审查产生）：

```json
{
  "receipt_id": "stable-receipt-id",
  "task_id": "TASK",
  "snapshot_sha256": "HASH",
  "reviewer_id": "HOST",
  "verdict": "PASS",
  "findings": "实际审查结论与理由",
  "evidence_refs": ["task:TASK"],
  "review_type": "new_experience",
  "score": 8,
  "applicability": "适用条件和限制",
  "duplicate_of": null,
  "started_at": 1793500000,
  "finished_at": 1793500010
}
```

引用只接受当前冻结任务或其明确关联工作任务的 `task:ID`。`review_type` 可取 new_experience、revalidation、reuse_change。校验身份、任务摘要、正文 B19、证据、评分、适用条件、时间和池节点执行收据；校验不通过不结算。合格的 FAIL、NEEDS_INFO 也保留原结论，不伪造成 PASS。宿主是作者或执行者时拒绝审查；全部池审查员不可用时，只有用户明确选择的 `--user-chose-host` 才走宿主兜底，结果标“无独立审查”。普通补票不加这个标记。

无需审查的任务使用同一入口，JSON 只需 task_id、snapshot_sha256、reviewer_id、result；result 是宿主实际结果。接收回执、最终结果和完成事件在同一事务写入。相同回执可重放，不重复结算；不同回执拒绝覆盖。这里不批准或发布经验，不调用确定性裁判。

## 工作区、资格与队列

沿用 run.py 的 `--agent-workspace-root`，通过同一数据库的 `agent_pool.workspace_bound` 审计事件绑定；Worker、ReviewStore 与页面适配器共享该路径，不增加配置字段。重启沿用原绑定；传入不同根目录拒绝启动，不复制或搬迁既有材料。未绑定且没有有效测试资格的真实节点不派活。

页面和 Worker 共用 node_runtime.readiness，比较 spec_digest、runner_digest、证据摘要与测试完成事件。适配器更新后须重测，不发模型探针。RELEASE.json 的节点证据只保留 node_id、enabled、test_passed、independence。

Worker 扫描全部到期任务，跳过等宿主或当前节点不足的任务，不领租约、不耗次数。跳过原因写入 last_error 和去重审计事件；Agent 池管理页的“待处理任务”显示这些记录。

本卡验收使用临时数据库、用户自选目录形状的临时工作区、假 CLI，以及 loopback 随机端口的 HTTP 服务。未使用正式 8799 或真实模型。
