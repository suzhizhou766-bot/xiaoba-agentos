# v1.6.6 候选：Agent 池收口与宿主回执

基线为已上线 v1.6.5。来源：`私人助理/工作区/小八架构修复-20261030/发版步骤-v1.6.6.md`，卡 32、33、35、36、37 的实现与卡 38 的发版前收尾。

本版没有新增数据库结构（结构版本仍为 7），没有新增切换步骤，历史发布记录一字未动。

## 一、本版做了三件事

### 1. Agent 池节点可修改

过去节点登记错了只能删掉重建。现在页面上可以改节点资料，接口是 `/api/agent-pool/edit`，实现见 `xiaoba_review/agent_pool.py` 的 `AgentPool.edit`：

- 可修改：名称、Agent、入口、模型；tmux 节点另有会话名、工作目录；`agent_type` 在已有时可改。
- 不能修改：节点 ID、角色、调用方式。要换角色或换调用方式，请另建节点。
- 改了入口、模型或 Agent 类型，节点的调用摘要（`spec`）就变了，上一次测试结果自动标为 `invalidated`（原因 `node_edited`），必须重新测试运行才恢复派活资格。这不是清掉摘要伪造的：资格本来就绑在摘要上，摘要一动资格自己就没了。**只改名字不作废资格**，工作目录和已有任务记录也不搬（`workspace_name` 保留原名）。
- 并发写用修订号挡：请求要带 `expected_revision`，节点已变化时报“请刷新后重试”；同一次修改的重复提交按原样返回，不算第二次修改。
- 有进行中的调用、未结算的审查指派或等待中的在场任务时不许改，报明确的冲突原因。已移除的节点要先恢复；旧四字段来源没有节点资料，不能修改，只能重新登记。
- 每次修改写 `agent_pool.edited` 事件，保留 `before`、`after` 和两个 `source_ref`；被作废的测试结果仍留在 `agent_pool.test_finished` 里，不删。

### 2. 接入口不锁版本

节点的入口按类型认，不按版本号认：`xiaoba_review/cli_identity.py` 的 `TYPES` 是 AGY、DSH、Qoder、WorkBuddy、CodeBuddy Code、Codex、Claude 与其它。WorkBuddy 的 CLI 就是 CodeBuddy，`source_family` 把两种类型算作同一个来源，所以独立性判定时它们不互相顶替（用户 2026-11-02 定）。

- 不记录、不要求、不比对任何 CLI 版本号。身份摘要 `implementation_sha256` 算的是小八这一侧适配器文件的哈希，CLI 自己升级不会改它。
- 入口路径里带版本目录或版本号（`versions/` 或 `x.y.z`）只给一条提醒：更新后可能失效，建议改用不带版本的稳定入口或软链；DSH 建议用 `current` 下的 CLI。**提醒不拦保存，也不拦测试**——`tests/test_cli_pool.py` 有一项就是断言带版本号的入口既给出提醒、探测仍然通过。
- 模型可以留空：`validate_node` 只在 `interface` 为 `cli` 时放行空模型（此时用该 CLI 自己选定的模型），其余字段仍不许为空。
- 入口不存在或不可执行时报 `entry_unavailable`，要求改入口路径后重新测试；不自动重试、不换模型、不换后台。

### 3. Worker 迁到 Agent 池，不再有默认审查节点

谁审查、谁协作由用户在池里建节点，角色是审查、协作、两者。**没有默认审查节点，发布包的配置模板 `agents` 是空数组，也不带任何默认池节点。**

- Worker 每次轮询问 `pool_dispatch.worker_roster`，它只看池节点；函数自己的注释就是“Historical config agents are not it.”
- 池里节点不足两份独立结论时，`roster` 的 `mode` 是 `blocked`，消息是“当前开启并通过测试的独立审查节点不足 2 个，这次不派审查”。同一入口程序加同一模型不算两份独立。
- 池审查员全部不可用时 `mode` 是 `ask_user`，等用户决定；只有用户明确选择才走宿主兜底，结果标“无独立审查”。不会静默回退到任何预设后台名字。
- 旧配置里的固定后台名字（`agy_review`、`dsh_review`、`qoder_review`）仍能加载，只作历史资料：`LEGACY_AGENT_NAMES` 里的名字既不被派活，也不能再作为发版验收的计票来源（`release_acceptance` 直接判“验收不再接受固定后台名字”）。
- 配置里的 `agents` 只有一种情况还参与派活：全部启用的都是 `type: mock` 的单元测试夹具（`_mock_agents`）。真实线路一律不参与。`health.execution_blockers` 的注释同样写明：历史配置后台不被调用，也不需要探测。

## 二、切换后，要审查的任务会被挡住，直到用户建好节点

这是本版最需要注意的行为变化：**切换成功不等于马上能干活。**

- 提交入口先挡：`intake --submit` 走 `execution_blockers` → `intake_blockers`，池里凑不出两份独立审查结论时返回 `status: blocked_local_health`、`submission: not_submitted_execution_blocked`，任务**没有入队**，退出码 1，并在 `hint` 里写明可以修好后重提，或加 `--queue-when-blocked` 明确要求先入队等待。
- 已入队的任务也不会被硬跑：Worker 扫描到期任务时跳过当前节点不足或正在等宿主的任务，不领租约、不耗 `attempts`，跳过原因写进 `last_error` 和去重审计事件，Agent 池管理页的“待处理任务”能看到。
- 所以切换后的第一件事是在池里建节点并测试运行，凑够两个彼此独立（入口程序或模型不同）且开启、测试通过的审查节点。`doctor` 的 `ok` 只看本地完整性，审查节点不足写在单独的 `review_nodes` 里，不会把 `ok` 打成假——别拿 `ok: true` 当作可以派审查的依据。
- 协作任务同理：没有可用的协作节点时消息是“没有协作节点，这次不排队”。

## 三、切换前不需要改配置

- `docs/DEPLOYMENT.json` 的 `from_source_manifest_sha256` 本版改为已上线 v1.6.5 的值 `317505b7b1199de16167d8adb8e11c097e96cc3d750214b5a714c9118c750195`（用 `shasum -a 256` 对 `.runtime/releases/v1.6.5/agentos/SOURCE_MANIFEST.json` 只读核对过）。改之前它指的是 v1.6.4 的清单哈希——v1.6.5 上线时这一行没有跟着往前走，切换预检会因“前置绑定不符”直接拒绝。
- 步骤 0002（技能根目录）、0003（技能索引）、0004（价值文件索引）、0005（AGY 路由迁到 `configured_model`）都已在线上执行过，`.runtime/release-steps.json` 里有四条记录。已执行的步骤内容必须逐字不变：切换脚本按 `digest(encoded(step))` 和历史记录里的 `sha256` 逐个比对，对不上就报“executed step has been modified”；历史发布包也会重新校验，内容变了报“previously executed step changed”。本版这四步一字未改，也**没有新增步骤**。
- 步骤 0005 已在历史里，因此不再拿它和线上配置的当前值比对，不会把用户以后改的模型名钉死。
- **不要提前去改线上配置的 `release.expected_version`。** 切换预检要求它仍等于旧版本 `v1.6.5`（`switch_release.py` 的“active config is not previous formal release”），是切换过程自己把它写成新版本号的。发布包里的 `config/agentos.json` 写的才是 `v1.6.6`，那是模板，不是线上生效配置。

新增 `tests/test_deployment_predecessor_binding.py` 四项，直接拿仓库里这份声明和 v1.6.5 的真实清单哈希跑切换脚本的声明校验：前置绑定通过、四个历史步骤不报变化；把绑定改回 v1.6.4 的哈希要被拒；改了某个已执行步骤即使前置哈希正确也要被拒；本版没有新增步骤。

## 四、宿主回执：没通过的结论也留下来

宿主的 FAIL、NEEDS_INFO 过去会被读成“任务通过了”——任务照样 `completed`，来源记录里没有 verdict，只有翻回执原文才看得出。

现在（`host_completion.py`）：

- **照样结算为 `completed`，不阻止结算。** 宿主的结论必须留下来，合格的 FAIL、NEEDS_INFO 保留原结论，不伪造成 PASS。
- verdict 写进任务来源记录：`provenance_json` 新增 `verdict` 字段（无需审查的任务走 `host_manual`，`verdict` 是 `null`）。
- verdict 也写进结果第一行：`宿主审查结论：PASS`、`宿主审查结论：FAIL（未通过）`、`宿主审查结论：NEEDS_INFO（未通过，需补资料）`。
- 因为 `xiaoba status --json` 打印的就是任务行（`result` 和 `provenance_json` 都在里面），CLI 和 Agent 池页面都看得出这一票没通过。`task-host-complete` 命令自己的即时返回仍只有 `status`、`task_id`、`label`，要看 verdict 请查 `status` 或任务详情。
- 其余校验一律不变：身份、冻结任务卡与快照哈希、B19 正文、证据引用只认本任务或其关联工作任务、评分 0–10、适用条件、时间窗、池节点执行收据。校验不通过不结算。同一回执可重放、不重复结算，不同回执拒绝覆盖。

新增 `tests/test_card37.py::test_host_fail_and_needs_info_still_settle_but_show_the_verdict`：三种 verdict 逐个跑，修复前在 `provenance_json['verdict']` 上就 `KeyError`，修复后三种都 `completed`、verdict 对得上、结果第一行对得上、重放不重复结算。

## 五、宿主补票不再丢经验候选

**这一版之前会丢。** 要进初审的新经验任务，一旦走到宿主补票，候选就没了：

- 正常路径是 `orchestrator` 先 `stage_experience` 写出候选正文，再 `store.complete_with_memory_candidate` 在同一事务里把任务标完成、往 `memory_candidates` 插一行 `pending_delivery_review`、写 `task.completed` 和 `memory.candidate_staged` 两个事件。
- 但 `orchestrator` 在派完池节点后如果发现这一轮要等宿主，会**在裁判之前、在暂存候选之前**就返回 `awaiting_host`；而 `host_completion.complete_host` 自己结算任务，四个 `memory_candidates` 插入点一个都不经过。CLI 的 `task-host-complete` 也只调 `complete_host`。
- 需要宿主补票的时刻，恰好就是池节点凑不够的时刻，所以这条路是常规路，不是边角。
- 影响范围要说准：审计任务（`experience_kind='audit'`）在两条路径下本来都不产候选，所以不受影响；受影响的是 `rule`、`method`、`legacy` 这些会产候选的任务。已有的宿主测试全部用审计任务，也没有一项断言过 `memory_candidates`，所以这个洞一直没被测出来。

现在接上了同一条候选路径、同一套门禁：

- `store.py` 把门禁和入库各抽成一个事务内helper：`_candidate_gates_tx`（约束保留未释放 → 不放行；冻结处置是审计 → 报错要求走审计专用结算；候选生命周期已终结 → 拒绝再次登记）和 `_insert_candidate_tx`（插 `pending_delivery_review` 行并写状态 sidecar）。`_complete_with_memory_candidate_tx` 改成调这两个，行为、事件顺序和返回值都没变；`complete_host` 调的是同两个函数，所以哪条路结算都不会改变候选能不能进初审。
- 宿主结算时：审计任务不产候选，和正常路径一致；非审计任务先过门禁，再用 `stage_experience` 写候选正文（含 `status: pending_review`、`type: "experience-candidate"`、`authority: false`），核对正文哈希与证据摘要一致，然后在**同一个事务**里写 `host.receipt_accepted`、更新任务、写 `task.completed`、入候选行、写 `memory.candidate_staged`。回执、结果、完成事件和候选仍然是一次原子写入。
- 来源任务沿用冻结快照里的 `memory_hits`，和正常路径同一个来源。
- 事务回滚时不留没有数据库行的候选文件：提交与否由数据库决定，不由异常决定；读不出来就不删，宁可留下待人工核对。
- **宿主不代批经验。** 候选只到 `pending_delivery_review`（待初审），初审和终审仍只能由用户做；宿主结算不调用确定性裁判，也不发布任何经验。

新增两项测试：

- `test_host_second_vote_queues_the_same_experience_candidate_as_the_worker`：`method` 类任务走宿主补票。修复前断言在“宿主补票把要进初审的经验候选丢了”上失败（`memory_candidates` 没有行）；修复后候选行是 `pending_delivery_review`、正文哈希对得上、正文里带宿主结论、sidecar 在、`memory.candidate_staged` 恰好一条，重放同一回执不重复排队。
- `test_host_settlement_keeps_the_audit_and_hold_gates_of_the_normal_candidate_path`：审计任务走宿主结算后没有候选行（与正常路径一致）；约束保留未释放时宿主**不能**结算——报错、任务状态不变、`result` 仍是空、没写 `host.receipt_accepted`、`memory-outbox` 里不留残余文件。这一项守住的是“不因接上候选路径就绕过人工放行”。

**受控验证任务不走宿主结算：** 验证轮次由 `complete_audit` 推进，宿主结算不走这条线，放行会让轮次卡住，所以 `task-host-complete` 直接拒绝，提示等池节点凑够结论。

## 六、文档与测试收尾

- `docs/CARD37.md` 的运维内容并进 `docs/OPERATIONS.md` 新的“生产入口与宿主回执”一节（宿主结算、工作区与资格、队列跳过规则），原卡文件按删除规则移出发布包。并进时去掉了两句只对当次开发有用的话（“本卡只修改开发源码……”“本卡验收使用临时数据库……”），运维规则一条没少。
- 两个被批量改名弄坏的测试名改回：`tests/test_release_recovery.py` 的 `test_undo_short_write_leaves_installed_file_complete`、`tests/test_curated_skills.py` 的 `test_project_storyboard_accepts_complete_and_rejects_incomplete`。原名里的 `complete` 是“完整/写完”的意思，被误当成“手动”加了 `_manually` 后缀。
- 版本升到 v1.6.6：`pyproject.toml`（唯一声明点）、`config/agentos.json` 的 `release.expected_version`、README 与 ACCEPTANCE、ARCHITECTURE、COLLABORATION、OPERATIONS 的版本头、两份入口 SKILL.md（包内 `.agents/skills/xiaoba-agentos/SKILL.md` 与包外 `development/interface/SKILL.md`，逐字节相同）、`docs/VERSIONING.md`，以及本文件和历史版本记录里追加的 v1.6.6 一条。历史发布记录（v1.4.2 至 v1.6.5）一字未动。
- `tests/test_card37.py` 的配置夹具现在把技能索引指到临时目录下的空索引。原先它沿用配置模板里的线上技能索引路径，审计类任务不读索引所以一直没暴露；本版第一次提交非审计任务，就会去读线上技能索引并因其审查任务核对不过而报错。测试只用隔离临时目录和夹具。

## 七、本版没做

- **不退役内置 WorkBuddy 引擎**，排到 v1.6.7。
- 不加新功能，不改线上、发布目录、`monitor.json` 和正式 8799，不调用真实模型，不新建备份。
- WorkBuddy 的 CLI 就是 CodeBuddy：新建节点只有“WorkBuddy CLI（CodeBuddy）”一个选项，判独立时两种类型算同一来源。
