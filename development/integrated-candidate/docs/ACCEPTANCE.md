# v1.6.14 发布验收

- 审查节点来自 Agent 池，成功门槛 2。v1.6.9 起构包接受“1 份池节点结论 ＋ 1 份宿主结论”，但至少 1 份必须来自已开启且测试通过的池节点，不能两票都是宿主，宿主也不能是这次的作者或执行者；宿主票只从 `task-host-complete` 留下的结算记录取，手写的 JSON 不算。构包从验收数据库取证。“干过活的节点不能计票”只查得到任务记录里有的执行者：验收任务必须用 `--work-task` 链上来源工作任务，确实没有时用 `--no-work-task` 明确写明，两者都不写构包拒收；工作如果不是通过小八任务做的，门禁看不出来，要由发版的人选没参与过的节点。固定后台名字和备用补位组合不再验收。登录材料与节点上的模型选择不在任何自动改动范围。
- 全回归、Skill结构验证、8技能和16知识来源哈希、源码与发布清单校验通过。
- 候选在隔离runtime与数据库验收，发布能力关闭；真实双后台健康标记和生产审核任务均成功，每侧输出一个独立 FINAL_ACCEPTANCE: PASS。
- 构建器从SQLite读取同源候选快照/运行/裁判/健康证据，不能用手写JSON或旧任务冒充。
- Vault只有一个活动根，无断链/环/重复父；历史系统说明和失效资产卡退出活动检索。保留业务知识，不把软件发布当经验批准。
- 活动审核候选及两阶段哈希批准记录完整保留，已发布经验才进正式经验索引；不存在候选自动批准或批量发布。
- 新skill入口真实可执行，坏配置/篡改发布fail closed。Worker启动后代码和manifest指纹一致。
- 健康是带时间的证据，不保证上游永久可用；AGY plan+sandbox不等于完整内部工具审计。
- 本节是验收门槛，不是已达成结论。v1.4.10 各门槛的实际达成情况、未执行项与证据不足项见 `docs/RELEASE_V1.4.8.md`；v1.4.3 的同类记录保留为 `docs/ACCEPTANCE_V1.4.3_EVIDENCE.md`，其证据绑定 v1.4.3 源码清单，不得继承；版本标识分层与冻结兼容名见 `docs/VERSIONING.md`。

## 经验晋升状态机（provisional → active）

- **不新增平行状态**：现有候选状态已经覆盖 provisional/active 语义。
  - provisional（未发布候选）：`pending_delivery_review`、`delivery_approved`、`revision_requested`、`delivery_rejected`、`pending_final_review`、`final_rejected`、`publication_failed`。
  - active（可被检索）：仅 `published`。`revalidation_required` 为隔离复验态，`retired` 为终态归档。
- **显式转移表**：`src/xiaoba_agentos/store.py` 的 `MEMORY_CANDIDATE_TRANSITIONS` 与 `MEMORY_REVIEW_STATUS_MAP` 是唯一权威定义，所有状态变更都经过 `validate_memory_transition()`；`retired` 为终态，任何审核都不能把已退休经验重新拉回晋升流程。
- **发布门禁**：`mark_memory_published` 只接受 `pending_final_review`/`publication_failed`，且必须存在与当前候选哈希完全一致的 final approve 记录；`memory.publish_enabled=false` 时发布在文件写入前即失败，不存在绕过人工审核的路径。批量发布 `publish()` 保持关闭。
- **检索双重门禁**：KnowLP 只负责定位；命中必须同时满足 (1) 回读 Vault 原文的 frontmatter `status: "active"`，(2) 绑定 `source_task_id` 的经验在数据库中为 `published`。数据库不可读或记录缺失时一律不放行（fail closed）。未过终审的候选不会出现在 `retrieve_hits()`/agent context 中。
- **审核回执**：`agentos memory-receipt <task_id>` 输出晋升回执（`status`/`active_retrieval`/`fully_approved`/`publish_ready`/两次批准计数/事件列表）；`agentos review` 的输出同样带 `review_id`、`previous_status`、`reviewed_status`。
- **异常路径**：审核与发布在同一事务内提交，事件写入失败则整体回滚（不留下半个状态）；发布失败写 `publication_failed` 并给出 `recovery_command`，可用 `publish-memory` 重试；文件已生效但数据库未结算时该经验会退出活动检索，等待重试结算。
- **无 schema 变更**：本次补强不新增表、列或状态，`memory_candidates` 的 CHECK 约束未改动；旧库（user_version=7）可直接初始化并通过 `schema_status()` 检查，无需迁移或回滚。

## 恢复机制验收边界

- 自动修复退役：须证明健康重探不改变任何任务的冻结状态，Worker 不再租历史修复任务、不把修复记录计为可运行；任务冻结与用户显式恢复保持可用。
- 兼容遗留数据：遗留 `resume_ready` 任务与修复记录须在模拟遗留库上验证，退役不升数据库版本、不删除历史行与审计事件。
- 恢复必须带证据：`task-resume --freeze-hash` 需完成冻结哈希、成功侧执行回执与失败侧新鲜真实健康复核；缺任一项判未通过，不能以“健康已恢复”代替，仍只补跑失败侧。
- 故障分级：单轮输出预算耗尽按任务级失败验收（不自动重发、手动恢复返回拆分提醒、不把后台标为不健康）；真实认证/网络/容量故障按后台级阻断验收。
- 不新增备份：退役与恢复路径都不得创建备份文件、备份目录或快照。
- 第二大脑固定模板：`docs/second_brain/v152_extra_nodes.json` 的两篇叶子正文须与线上原文逐字节一致，第三项只改 `10_治理与裁决/故障自愈与任务恢复.md` 的 body；生成树门槛（唯一父链接、无断链/环）不等于用户批准经验。
- 本节是验收门槛，不是已达成结论；正式独立审核仍由 Agent 池里合格的审查节点执行，辅助模型的报告与预审不替代它。


新增经验分类与受控验证的运行契约及验收条件见 docs/EXPERIENCE_TRIALS.md；技术审计仅结算审计回执，方法必须真实新执行与独立双审后用户终审。历史候选不批量迁移。

## v1.6.11 候选：显式发布验收声明

发版工具用 `scripts/submit_release_acceptance.py --config CONFIG PROMPT --memory-kind audit --work-task TASK_ID` 提交；确实没有来源任务时改用 `--no-work-task`。工具强制传入 `submit --release-acceptance`，快照冻结 `review_context.release_acceptance: true`。普通 submit 冻结 false，不会因为题面引用两个验收结论标记而被识别成发布验收。宿主审查、宿主补票仍按自己的身份与上下文要求结论。

兼容口径：仅缺少该字段的历史快照保留原题面双标记判据；显式 false 不回退，显式 true 不要求题面标记。历史快照及哈希不重写。

未评分事件的 reason 使用 `no_scoring_source`（无评分来源）、`kind_not_method_rule`（分类不适用）、`mock_or_non_pool`（mock 或旧非池协议）、`host_draft`（宿主草稿）、`no_review_receipts`（尚无审查回执）。原因由价值评分函数同次求值返回，候选插入不重复判断 requires_review_vote。

页面安装清单的 before 基准为上一已发布 v1.6.10 的 after。测试夹具 `tests/fixtures/review-ui-v1.6.10.json` 来自已上线不可变包 `工作区/小八AgentOS-v1.6.10-热修/releases/v1610-r3/v1.6.10/agentos/src/xiaoba_agentos/review_ui_assets/manifest.json`；该版页面与 v1.6.9 相同。它是发布契约的固定测试输入，不从当前开发清单推导。

干活分更正接口接受 `outcome: "void"`、非空 `void_reason` 和 `evidence_refs`，仅作废已结算自动样本。保留冻结身份与回执哈希，points/tasks/success_rate 归零；事件 delta 为原分数取负，void 为 true。相同评价ID、相同内容可幂等重读，不能恢复计分或再次更正作废行。统计与近四周择优样本均排除 void。

已完成但缺候选的修复在价值分丢弃时不插候选、不发 candidate_staged；保留原已完成 result/provenance 与 task.completed 哈希，另记价值评分决策。放弃或取消后的恢复通知采用读取过滤：保留通知及原事件，不自动伪造用户确认；doctor 和待处理计数使用相同过滤。

复用改动派审按原请求版本、原指派记录尝试；仅未出合格且已确认来源的回执算软失败。下个空闲周期每张未合格卡最多再派一次，合格卡不重跑；仍失败显示“待人工补派”。硬异常、在途或中断的不确定尝试停止自动补派，不自动换节点。尝试及可见状态写入既有事件链，不新增数据库表、列或索引。

仓库级 Python 测试入口顺序检查覆盖所有 test*.py / *_test.py，没有排除目录（卡 60 已改正监看中心仓库副本的 12 处）。已有测试只把主入口移到文件末尾，避免直接运行时漏收后面的定义。

构包要求验收任务快照里 `review_context.release_acceptance` 为 true；题面里的结论标记不能代替这项声明。
