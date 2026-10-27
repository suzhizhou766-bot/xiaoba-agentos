# 底线测试标记

`tests/guardrail_marks.json` 是测试标记清单。每条 B01–B24 各有一个条目，`tests` 内写 `test_文件.py::test_函数`；同一测试可以守护多条底线。现有测试采用 `unittest`，因此标记保存在独立清单中，不改变测试执行方式和断言。

首版映射沿 `小八规则清单.md` 的底线 → 支撑规则 → 原始测试出处生成，并按当前候选的实际函数名校验。`tests/test_guardrail_marks.py` 阻断无效编号、悬空函数和状态不一致。清单是需要持续复核的证据索引；函数存在本身不证明断言充分，改动底线测试时必须审查实际行为。

`guarded` 只表示至少一个支撑规则找到了已标记测试，并不表示整条底线已被自动化完整保护。B23 剩下一项测试，只对应 MG-009 的评分可复算（策略进化退役后不再有按候选调权的入口，权重恒为 1.0，可复算这条不变）；MG-002 的用户转交和 MG-010 的主力自留高权限工作没有自动测试，已在清单中显式标为 `coverage: partial`。这一映射依据规则清单的 B23 原始出处，不能把评分测试解释成任务转交的验证。

B11 新增 `test_memory_approval_binding.py` 的三项测试：初审批准内容与终审绑定、共用审核入口的初审绑定拒绝、终审批准内容与数据库发布绑定。两次独立 mutation 已验证删除对应门禁会使测试失败。第三项覆盖 store 的发布结算，不证明 CLI 在数据库拒绝前不会写入 Vault；发布入口写入顺序的独立发现见工作区主力复核记录，不能将底层测试通过扩大为该入口的完整保护。

架构第 5 步又给 B11 补了 `test_memory_publication_gate.py` 的两项标记：终审批准没有覆盖已登记证据时 CLI 拒绝、终审之后证据被改过时共用审核入口拒绝。这两项此前完全没有被标记过。删掉 `store.py` 里“memory publication is not bound to the final approved candidate”这道检查，全量测试里真实失败的是这两项加上 `test_memory_approval_binding.py::test_publication_refuses_content_the_final_review_never_approved`；删掉“final review is not bound to the approved delivery candidate”那道检查，真实失败的是 `test_memory_approval_binding.py` 里初审绑定的两项。两道检查都确有测试看守，mutation 之后 `src/` 已按不可变基线逐字节恢复。同一批把 `test_store.py::test_candidate_sha256_binding_still_holds` 改名为 `test_registration_refuses_staged_evidence_that_does_not_match_its_declared_hash`：函数体测的是登记时证据哈希对不上就被拒绝，与同列在 B11 下的那两道“批准绑定”检查不是一件事，旧名字里的 binding 容易被读成同一件事。断言一字未改。

B07 目前仍为 `no_automated_test`，不得宣称受到自动化保护：架构第 5 步在 `src/` 里找过“独立判断、不迎合”的具体守卫，没有找到可测的实现，MZ-001 只由文档与人工遵守。任务卡建议把状态写成 `by_discipline`，但 `config/release_policy.json` 的 `allowed_statuses` 只允许 `guarded` 与 `no_automated_test`，写进去会让发布门禁直接失败，所以状态不变，实情记在清单该条的 `note` 里。B02 已与 B22 交叉标记 `test_routing.py::test_high_impact_request_cannot_be_forced_to_direct`（高风险请求不得强制直答）。将来补测试时，必须同时更新对应标记。

架构第 5 步把 B01、B04、B24 从 `no_automated_test` 改为 `guarded`。三条都只覆盖一部分支撑规则，逐条写明 `coverage: partial` 与没覆盖到的规则编号，不宣称整条底线已被自动化保护：

- B01（重大决定归用户）标记 `test_switch_release.py` 的两项：plan/preflight/readback 全程不写盘也不停服务，票据过期、被改或现场变了时一律不写。看守的是 MF-048（切换类命令必须逐条交给用户在自己的终端确认执行）。MD2-44、MD2-13、MD2-42、MZ-022 在代码里没有找到可测的守卫，仍靠自觉。
- B04（删除前看清对象，能恢复）标记 `test_memory_failure_recovery.py` 的七项，看守 MC-33：`memory-retained-resolve --trash` 必须按 dev/ino 核对到同一个对象之后真的移进系统废纸篓（同一 inode，不是复制后删除），身份缺失、只给一半身份、路径上站着软链接、对象已被换掉、对象还在时都拒绝，并且一次只动用户点名的那一个对象。MF-042 与 MD1-061 讲的是重复文件清理，`src/` 里没有这个功能，无从测起。
- B24（其它底线）新增 `tests/test_guardrails_step5.py` 的五项，只看守 MH-011 的“仅监听本机内部端口”这半边：裁决中心绑在 127.0.0.1，Host 指向别处或 `Sec-Fetch-Site: cross-site` 的请求都 403，同源请求正常服务，写操作还要求本机 Origin 加 CSRF 令牌。把 `safe_host()` 改成恒真会让其中两项失败，把入口绑定改成 `0.0.0.0` 会让另一项失败；写操作那项不会因为 `safe_host()` 失效而失败，因为 `do_POST` 自己另查 Origin 与令牌。MH-011 的另一半“验收结果呈现为通过/分歧/证据不足”在 `src/` 里没有实现，MC-41 也没有代码守卫，两者都只靠自觉。

`tests/test_guardrail_marks.py` 把“没有测试的底线集合”写成字面量 `{B01, B04, B07, B24}`，补上标记后它必然报不一致，必须同批改成 `{B07}`。该文件不在架构第 5 步的可改清单里，这处改动由主力在集成时执行。

目标架构第 2 步的发布分档应读取本清单：改动或删除任一带标记的测试函数，至少按常规档处理。不得只按“文件在 tests/ 下”归为补丁档。

B08 新增 `test_schema_fingerprint.py` 的两项：在临时库实际建表，表结构指纹必须与 `tests/fixtures/schema_fingerprint.json` 一致，且指纹文件的结构版本等于代码常量。改了表结构而不更新指纹文件，测试失败；更新指纹文件按路径即为迁移档。B10 新增 `test_release_tier.py` 的十项：档位由上一版发布包里的工具和规则算出，候选自带的规则不参与；常规档和迁移档仍要求两个后台的证据；包内档位记录与重算不一致、缺记录、上一版没有工具时自报补丁，均被拒绝；补丁档由构包实跑全量测试，且不得改动接口脚本。这些测试里的分档工具是测试替身，分档规则本身的测试在 `test_release_policy.py`。

B14 新增 `test_content_notes.py` 的四项：认不出格式的同名笔记不被内容卡片覆盖；包里少了未列入交还清单的笔记时切换被拒绝；索引自检不过时不生成笔记；列入清单的笔记经过一次真实切换后原样保留。同一版撤下 B14 的五项旧标记：模板往内容分支新增叶子的三项拒绝测试、审美手册与发版流程两篇的冻结元数据测试、三项冻结身份合并测试。原因是内容分支已归笔记库，模板不再能新增任何笔记。替换为四项：模板不能新增笔记；冻结的恢复策略仍被合并且内容分支不再生成；生成结果不含内容分支笔记且树有效；生成内容分支或丢掉根笔记对它的链接即无效。

v1.6.1 新增 `test_content_index.py` 的标记。B10 十项：没有与草稿逐字节对应且两个后台都通过的审查任务时发布不改动任何东西；审查之后草稿再变则不算数；一个技能的审查不能用来发布另一个技能；手写说明即使带着指纹行也不是证据；绕过命令手改索引收录未审内容时自检不通过；基线技能少一个或多一个文件不被信任；索引里记的审查任务必须正好对应磁盘上的内容；带发布清单的目录里基线不可读或缺失时按不通过处理；索引脚本只能校验不能写；协作技能必须带用户规则原文。B22 一项：配置步骤只能把三个内容路径指到固定位置。B08 两项：受管目录下重新出现文件会破坏发布绑定；新位置没有内容时拒绝切换。

策略进化退役（2026-10-11 用户裁决）撤下四项标记，理由都是「随功能一起退役」而不是放宽底线。B10 撤下 `test_evaluation.py::test_regression_gate_aggregates_all_cases` 与 `test_evaluation.py::test_empty_regression_dataset_fails_closed`：这两项看守的是策略晋升门 `promotion_gates` 的回归闸门，`promotion_gates` 表只读保留、不再有任何读写入口，闸门本身已不存在。B10 的实质是「正式发布要两个独立后台的真实结论」，那一条由 `test_release.py`、`test_agy.py`、`test_dsh.py`、`test_release_tier.py` 的其余标记看守，与策略晋升链无关。B21 撤下 `test_evaluation.py::test_replay_records_isolated_evaluation_run`：它看守的是评估面不写 `agent_runs`，评估面整体退役后没有可隔离的对象；B21 的「冻结隔离上下文」由 `test_task_context.py`、`test_user_rules.py` 等其余标记看守。B23 撤下 `test_evaluation.py::test_adjudication_baseline_skips_single_candidate_cases_and_touches_nothing_production`：它看守的 `adjudication_baseline` 是已退役命令 `judge-baseline` 的实现，「不触碰生产数据」这层保护由新增的 `tests/test_offline_judge_cases.py::test_the_cases_need_no_database_and_no_model_call` 接过来，且更严格（连库都不连）。

B12 新增 `test_store.py::RetiredStrategyTablesReadOnlyTest` 的三项，守「退役的不复活」。退役的 6 张表没有删，只读保留，结构版本仍是 7：`test_a_full_round_of_work_leaves_the_six_tables_row_for_row_unchanged` 建一个结构 7 的库、6 张表各放样例行（`strategies` 含一条 active 行），跑完 `initialize()`、提交任务、裁判、`status`、7 个退役命令后，断言 6 张表逐行不变、`user_version` 仍为 7、事件链通过，同时断言这一轮确实完成了任务（否则「没变」没有意义）；`test_a_database_this_build_touched_still_passes_the_v162_self_check` 断言用新代码操作过的库仍是结构 7、6 张表都在且列定义与基线建表语句逐字一致，退回 v1.6.2 时旧版的结构自检还能过；`test_source_never_reads_or_writes_the_six_tables` 用 `tokenize` 取源码里的字符串字面量、按分号切成 SQL 语句静态扫描，6 张表名只允许出现在 `CREATE TABLE` / `CREATE INDEX` 里（注释里提到表名不算），任何 `SELECT`/`INSERT`/`UPDATE`/`DELETE` 碰到它们都失败。

B23 保留的 `test_evaluation.py::test_explain_decomposition_sums_to_choose_score` 被收窄：它原来还断言「候选权重来自策略表、缺省取最小值」，权重入口随退役删除，这两条断言已无对象；保留的断言是分数必须能由分项与权重精确复算、`choose` 与 `explain` 给出同一个分数，并新增一条「所有候选权重恒为 1.0」。任何隐藏的调权因子都会让复算对不上，所以这条底线没有变松。

## v1.6.5 补标

- B03：`test_install_service_no_backup.py` 五项（含 Runner 重建与 agent-enable/disable）。`install-service` 过去会自动备份数据库与服务配置；v1.6.5 起数据库已是当前结构时不碰、需要迁移时须用户加 `--backup-database` 明确授权，服务配置原内容只留在内存里用于本次回滚。
- B08：`test_switch_release.py::test_successful_switch_seals_the_activated_release_read_only`。切换成功后发布目录设为只读，任何进程都不能往里写文件（多一个文件即让入口发布校验失败）。
- B10：备用补位的构包门禁（备用+备用、无补位证据、被替换者非主后台、中途补位的类别不是外部故障，一律拒绝；门禁还会去数据库独立核对：派发前补位看被替换方补位前最近一条健康记录——真实失败的探针或已记录的外部故障（后者不需要探针标记），类别与事件一致；中途补位时它在本任务里确实有同类别的失败运行）与调度器（备用不作第三个审查者、已给结论的一侧不重审、两主都失败不补）；配置里 `standby` 必须是布尔值。
- B16：两个主后台都不健康、或启用的主后台少于两个时，即使备用健康也挡住接单；上班探针同一口径。
- B17：任务中途的内部故障与输出预算耗尽不补位（健康探针真实失败则不论原因都补，用户 2026-10-21 定）；Headroom 就绪问题即使提到 AGY 也归内部；单独的 EOF 归外部；5xx 状态码要带上下文才算，两层判定一致；Qoder 的登录词表不再影响 AGY/DSH，回答正文里的认证字样也不算认证故障；数据库附属文件在检查中途消失不再被当成内部故障（确定性回归测试：第一次检查时文件还在、第二次已消失；v1.6.4 起就有的竞态，v1.6.5 修复）。
- B18：改名为“模型用用户自己的选择”：DSH 跟随桌面配置，AGY 与 Qoder 用用户在各自 CLI 里上次选定的模型；原先守 AGY 读桌面、标到 B18 的两项测试已随读桌面代码删除（读桌面代码的全部用例共 6 个，含 `test_agy_desktop_route.cjs` 与 `test_desktop_diagnosis.py` 里的，一并删除，见发布说明第十四节），换成读 CLI 设置、旧写法线路不能借 settings_path 指定任意文件、Qoder 模型类环境变量被清除、多个模型键判失败等测试。
- B20：失去执行权（租约被收回）后不补位、不结算，两处都有测试。
