# v1.6.13 运维

```sh
XIAOBA="$HOME/.agents/skills/xiaoba-agentos/scripts/xiaoba"
"$XIAOBA" doctor --json
"$XIAOBA" status --json --limit 10
"$XIAOBA" session-start
"$XIAOBA" review-list --stage all
```

上班不发真实探针，只做本地检查：节点是否开启、测试资格是否有效、入口是否还在。节点要真实连通时，由用户在页面手动测试运行。只读检查不创建任务。Worker 身份为小八 AgentOS Runner；沙箱禁止 kill(pid,0) 时可能误报 alive=false，需授权只读进程确认，不盲目重启。

v1.6.9（卡 51）已移除 `recovery.probe_on_session_start` 及 doctor 的
`requires_session_start_probe` 字段。旧配置残留该键也不触发模型调用；上班只做上述本地检查。

### 干活分与价值分（卡 51，2026-11-08 用户补定）

干活分在任务结算时自动记，不等待用户说“交付”，也不调用
`experience_loop.record_delivery()`。宿主绑定回执结算、普通 Worker、审计结算、
终态失败与用户放弃/取消共用事务内记分入口：被接收的节点回执且任务完成记 1，
失败、超时、拒收或放弃记 0；宿主自身、健康探针和测试任务不记分。
账本按任务＋节点＋实际执行角色去重，使用派单时的配置模型；`both` 是可用角色范围，
不会成为第三种计分角色。用户事后要求修改不扣分。

周分按 Asia/Shanghai 周一至周日汇总，只记本周分、任务样本数和成功率，
不设 Agent 永久总分、不计资源投入。原手动接口可按当前修订号更正自动评分，
多角色任务须明确 `review` 或 `collaborate`，更正不产生第二个样本或改换周桶。
旧外部工作回执的手动核验接口保持兼容。

自动选择两位同角色节点时，先按用户授权、开启、测试资格、作者/执行者排除和入口＋模型
独立性筛选。第一位按该角色本周分择优；第二位优先给近四个自然周（本周及前三周）
记分少于三次的节点，按最近一次同角色派单时间从早到晚轮换；没有这样的独立可用节点时
也按周分选。旧 `dsh`/`peer` 仅兼容读取，不限定厂商，不补固定默认审查员。

新经验的价值分与干活分分开。审查员提供 0–10 分和重复经验标识，系统按均分 ≥6
进入初审；均分 <6 丢弃，仅留候选摘要与各方分数；最高最低差 ≥4 或已有经验重复时
直接进入用户初审，并携带分歧/重复依据。该判断不批准任何经验，不改变用户初审、
终审及真实隔离验证门禁；已初审通过的复验仍走原验证流程。历史未分类任务沿用旧协议。

### 正式经验复用分（v1.6.9 卡 52）

复用分与新经验两轮复验、Agent干活分分别记账。只检索或阅读不计分；用户明确
“交付”后，本任务结论 `tasks.result` 确实引用源经验ID或笔记名，才结算。
宿主本地任务也须有对应任务行与结论；缺失时只记跳过事件，交付照常。

`memory-application-record --receipt FILE` 也可登记当前 `published` 正式经验。
原有 `real_task`、`artifact`、应用位置、步骤、偏离与执行证据字段仍必填；
`candidate_id` 为源经验ID，`candidate_sha256` 为**发布笔记当前字节的SHA-256**
（检索命中的 `source_sha256`），正式复用不要求 `delivery_review_id`，不走初审候选
或试验轮次。应用登记校验版本与证据，不计分、不修改源状态。
正式笔记登记和检索均以1,000,000字节为体积上限（不是1 MiB），超限明确拒绝登记。
交付分类只采信登记时服务端写入的 `formal_reuse`；登记时是新候选的应用，后来即使
已发布，仍回写原候选的 `memory.delivery_attached`，不在交付时按当前状态重新归类。
正式应用从新候选验证包、试验绑定、终审绑定中排除；直接拿正式应用挂接新候选验证
会明确拒绝，复用改动终审仍留给卡52b。

应用记录须明确填写，系统不会从自由文本猜测是否“原样”：

```json
"reuse_assessment": {
  "outcome": "unchanged",
  "changed": "",
  "added": "",
  "discarded": ""
}
```

`outcome` 取 `unchanged`（原样好用）、`modified`（改过）、`inapplicable`
（不适用）。后两种必须在 changed／added／discarded 至少一项说明改动、补充或
摒弃，其余可空；unchanged 三项须空。缺项或相互矛盾时不猜分，记跳过事件。
`memory-delivery-record --receipt FILE` 仍绑定该应用的任务、成品版本和用户交付记录。

同任务同经验永久只结算一次，重新发送消息、交付新成品版本、撤回再交付均不重复加分。
原样好用记1；改过或不适用记0；同任务同成品对同一来源有多份应用记录时，必须全部
有效，任一记录改过或不适用即记0。没有交付不计分、不扣分。

`memory.reuse_scored` 在交付事务中记录源ID／发布版本、真实任务、应用ID、交付事件ID、
0或1及 `used_at`。`reuse_scores.projection_tx` 从SQLite专用结算事件投影永久总分与
`last_used_at`，0分的有效应用也更新最后使用时间；不新增表／列，不从Markdown台账
反查，不把旧 `reuse_count`、`successful_reuse_count`、`last_validated_at` 冒充新账。
交付先无条件检查事件链；accept 时所有应用（包括正式复用）均重新校验成品及执行
证据。坏链或交付证据漂移拒绝交付，且不写交付、计分或跳过事件；消息重放也不能绕过
链门禁。用户来源、应用／成品版本、交付记录及新候选验证门禁继续保留。
交付本身有效后，每个来源用保存点包住记分及改动事件：记分前提缺失、源正式笔记版本
漂移、记分阶段复验发现漂移等记 `memory.reuse_scoring_skipped`，回滚该来源部分记分，
交付继续，其它有效来源照常计分。该容忍仅属于记分环节，不豁免交付门禁。
例外：历史 `memory.reuse_scored` 有畸形事件（字段缺失、类型异常或重复键）时，投影
采用全局熔断，本轮所有正式来源均跳过记分；有效交付照常记录。doctor 的
`reuse_scores` 返回 ok=false 和 problems，warnings 明示“复用分”“全局熔断”；
该报告不代替原有事件链检查，也不另行改变 doctor 的就绪判定。

0分同时追加一次 `memory.reuse_revision_requested`，保留源ID／版本、任务、应用ID与
改动／补充／摒弃。**本卡不送终审队列，不改正式经验状态**；卡52b再实现独立复用
改动终审、审查证据和新版本发布。历史待复验／退役来源保持原状，不因其它任务成功
而恢复。正式经验不再参与旧成功计数、自动隔离、失败降级或阈值退役；新候选自身的
驳回、两轮验证以及用户对历史待复验经验的显式裁决不变。
正式来源的旧 `memory.reuse_uncited` 留痕已停用；命中追踪行仍可保持 pending，
不代表实际使用。引用判断与记分／跳过结论看交付后的专用复用事件；非正式来源的
旧 uncited 路径保留。

检索先由KnowLP按相关性提供有限匹配池，再对校验通过的匹配按永久复用分降序排序；
默认主名额6条（沿用 `knowlp_limit`），另追加最多1条匹配的低分／新正式经验，不替换
主名额。同分沿用相关性顺序；留位从主名额以外选分数最低者，同分也沿用相关性。
默认最多读取相关池24条（主名额的4倍），不逐条读全库，重复来源不占多个名额。
新分账不可用时保留检索可用性，按0分及相关性返回并在 `score_problems` 中说明。
命中附带 `reuse_score`、`last_used_at`；检索本身不修改分数或最后使用时间。
14天未用计时及提交不在本卡范围。
复查发现5（检索热路径全量校验事件链、扫描计分事件）的实现本轮不改；索引或物化账本
等性能处置留给卡52b／恢复发版时评估，本轮不加索引或其它结构。

正式变更：冻结候选源码 → 重建技能/知识/源码清单 → 单元测试、单根Vault和KnowLP检查 → 临时 runtime/index 的生产模式双Agent验收 → 验证验收与同源哈希绑定 → 暂停Worker → 发布及入口切换 → 重载Worker → doctor/status/索引复验。不跨版本迁移授权结论。备份仅在用户直接、明确要求时执行；发布授权不包含备份授权，未经明确授权应跳过内置备份步骤。详见 `docs/USER_RULES.md`。

回滚使用已存在且经验证的发布内容，或用户明确要求创建的可用备份，恢复运行源码、release index、Vault、KnowLP及skill入口，再重启Worker、验证指纹；不得为回滚自行新建备份。任务数据库不回退到旧快照，避免丢失新审计记录。历史目录不参与当前检索。

`session-close` 不是批准。`review-list`/`review` 保留同哈希双审；未批准候选继续私有保留。禁止无任务ID批量发布。

## 现行运维要点

- Worker 服务运行的是不可变发布内的代码。提升新版本后必须重装或重启 Worker 服务，否则进程仍在旧发布上运行。
- 切换成功后会把新发布目录封成只读（文件 0444，可执行文件与目录 0555），结果记在切换返回的 `release_sealed` 里；封不上只报告，不退回已成功的切换。`doctor` 的 `release_read_only` 只报告当前发布目录是否仍是只读，不影响 `ok`。不要手工在发布目录里以会写字节码缓存的方式运行或导入 Python：多出一个字节码缓存文件就会让入口的发布校验失败，小八拒绝运行（v1.6.5 起把发布目录封成只读，正是为了防这一件事）。Worker 服务照常运行发布内的代码，它以不写字节码的方式启动，不受影响。
- 裁决后端由小八监看中心（`http://127.0.0.1:8799/`，「裁决中心」标签）按需拉起并看护：监看中心开启时运行，关闭时一并停止，没有独立网页，原 `127.0.0.1:8797` 地址与 `com.xiaoba.review-center` LaunchAgent 已停用。监看中心没开时不能在浏览器里裁决，待审队列也不会同步到第二大脑；重新开启后补同步。CLI 的 `review-list`/`review` 不受影响。
- 裁决后端必须与 Worker 使用同一份 `XIAOBA_CONFIG`。监看中心默认读取 Worker LaunchAgent 里的配置路径；裁决后端运行时把自己实际使用的配置记录在 `<runtime_dir>/review-center/backend-state.json`，`doctor` 的 `config_consistency` 据此把它列为 `review_backend` 一并比对：内容不一致判失败，路径不同但内容相同给警告。后端没在运行（或记录已过期）时跳过，不算失败。旧的 `com.xiaoba.review-center` LaunchAgent 若被重新启用，仍按原规则比对。
- 裁决中心列表的序号角标（v1.6.9 卡 47）：序号是该条在**全部待审队列**里的位置，后端按登记时间排序；页面搜索或过滤时不重排、序号不变；某条被处理离开队列后，剩下的条目按新队列重新编号。序号只取完整队列快照，不是搜索结果的下标。
- **已退回的条目不能重复退回**：`revision_requested` 的条目仍留在待审队列里，列表和详情显示「已退回，等作者修改」及上次退回时间、上次意见；「需要修改」按钮置灰并写明「正文未改动，不能重复退回」，**驳回仍可点**，批准路径不变。后端在该条候选正文哈希与上次退回相同时，再提交「需要修改」返回 `already_revision_requested`（带上一次退回时间和意见），不新增裁决、不新增审计记录；作者改过正文（候选正文哈希变化）后再退回照常写入。
- `intake --submit` 返回 `blocked_local_health` 时任务未入队，按 `blockers` 处理后重新提交。
- `install-service` 不会自行备份（v1.6.5 起）：服务配置的原内容只留在内存里，安装失败时写回并重启原服务（按 launchctl 的实际结果报告，重启失败会写明）；数据库已是当前结构时不碰；只有数据库确实需要迁移时，命令会停下说明，用户明确同意备份后再加 `--backup-database` 重跑。安装中途失败时，错误信息会写明是否创建过数据库备份，没有创建就明说“本次未创建数据库备份”，不会误报“已恢复备份”；回滚留下的失败副本位置、以及旧 Runner 留在了哪里，也会写进错误信息。重建 Runner 时被替换下来的旧 Runner 移进废纸篓（可恢复），不再留在 `.runtime/backups/runner/`；移不进废纸篓时（比如废纸篓目录不可用），旧 Runner 会留在运行目录里，命令结果会写明它的位置并提示用户处理，绝不会悄悄删掉。`agent-enable` / `agent-disable` 也不再复制配置文件，新配置先写临时文件再整体替换。日志轮转与安装失败的新配置文件仍移到 `.runtime/backups/` 下（是移动现有文件，不是复制备份）。
- 策略进化已于 2026-10-11 退役（v1.6.4 起代码移除）：`evolve`、`eval`、`replay`、`regression`、`judge-baseline`、`promote`、`rollback` 七个命令只打印“已退役”并以 2 退出，什么都不做；`status` 不再有 `active_strategy`。原 24 个评估用例改为离线测试 `tests/test_offline_judge_cases.py`，每次全量测试都跑。数据库里的 6 张策略表只读保留，不要手动删除，删了旧版就无法退回。
- 宿主撰写候选使用 `memory-candidate-create --title … --body-file …`，不要直接写数据库。
- AGY 路由采用 `selection=configured_model`，并写明 `model`（用户 2026-11-01 定：配置里的模型就是实际使用的模型）；不读取 agy CLI 设置文件，也不读取或启动 Antigravity 桌面。旧写法仍可加载，但新版调用 AGY 时被拒。线上指定的 `local_account` 旧路由由切换编号步骤 `0005-agy-configured-model` 迁为 `Gemini 3.8 Flash (High)` 新写法，切换后不用再改路由；现场与步骤声明不同则预检拒绝。切换前由用户将 `integrations.agy_command` 和 `paths.agy_command` 两处都改为 `/Users/suzhizhou/.local/bin/agy`，详见发布说明第五十八节。
- 详见 `docs/AGY_DESKTOP_FOLLOW.md`（后台模型选择边界）与 `docs/RELEASE_V1.6.5.md` 第十四节。
- 切换（`switch_release.py`）前的两项检查（v1.6.5 起，见发布说明第三十八、四十、四十一节）：旧版本 doctor 只允许因“后台线路就绪”（`local_cli_routes`）不通过——报告须明确 `ok` 为假、`paths` 非空且全部为真、线路检查（`local_cli_routes`）明确不通过、其它各判定检查都在且明确通过（`ok` 为 true）；doctor 通过时票据也记下不参与判定的失败项——失败原文写进切换票据，其它任何情况照旧拒绝；退回后读回旧版用同一条规则；切换后读回时新版本 doctor 必须完全通过，否则自动退回。队列里从没被领过的 pending 任务（attempts=0、没有租约）可以留到切换后由新版本处理；运行中、重试中、冻结、可恢复的任务和任何未释放的租约照旧挡住切换。

## Agent 池

谁审查、谁协作由用户在池里建节点，角色是审查、协作、两者。没有默认审查节点。配置模板不带默认节点。旧配置里的线路名字仍能加载，只作历史资料，不参与派活，切换时也不会自动建成节点。doctor 的 ok 只表示本地完整性；审查节点不足单独报告，不让 ok 变假。要审查的任务在节点够两份独立结论之前不入队。被冻结的任务仍须用户显式恢复。
- 节点的掉线时限写在节点资料里，缺省 300 秒，是连续无进展窗口。人工转交和 tmux 等待不按掉线计。历史任务里的 `standby.substituted` 事件仍能读出，新任务不再按固定后台补位。
- `doctor` 的 ok 只看本地完整性。审查节点不足写在单独的 `review_nodes` 里，不把 ok 打成假。

## 生产入口与宿主回执

原生 CLI 的 `submit --no-review` 创建工作任务。宿主身份用 `--host-id / --host-entry / --host-model` 指定；这三项只标识宿主，不能声明“没干活”。

实际参与者通过 `task-work-record TASK --actor-id ID --role author|executor|collaborator` 追加记录，不能撤销。页面对已有原生任务调用 `/api/agent-pool/work` 时自动记协作参与者和 agent_runs；Worker 的协作任务则从 workflow 与 agent_runs 取参与者。

产物送审使用 `submit "审查说明" --work-task WORK_TASK --memory-kind audit`，指定补票宿主时加上述三个宿主参数。Worker 从来源任务、作者记录和实际执行记录推导排除名单，提交方不能传一个空名单覆盖它们；已有 collaborate-review 的工作关联使用同一排除规则，页面送审读取候选自己的作者和来源任务记录。

### 任务材料

无人值守节点读不到工作区外的文件，**任务材料必须显式声明**：`submit ... --material /absolute/path`（可重复；文件或目录都行），语义与 `collaborate-review --artifact` 一致——提交时冻结绝对路径和 SHA-256，派活前把材料复制进节点工作区的 `任务记录/<记录>/材料/`（副本只读），派工卡列出本地路径并写明“只读这里列出的材料”，节点只读这些副本。接入口任务卡 `evidence_refs` 里的绝对路径引用同理自动当文件材料；`test://`、`task:` 这类标签引用不是文件，不进材料。**未声明的工作区外路径会被拒绝**：权限系统拒绝读文件或跑命令时归为 `permission_denied`（提示：把文件作为材料提交，或走 tmux 在场通道），不返回原始输出。材料在冻结后被改动、含符号链接、超过上限（10000 个文件、200 MB）或属于凭据类文件时，拒绝派活并写明原因，不静默跳过、不替换。凭据检查分三层：私钥文件、环境变量文件、文件名含“凭据/密码/密钥/口令”等凭据文件一律拒绝；文档、配置等文本全文按 `_sensitive_reason` 检查；代码和差异文件（`.py`、`.diff` 等）常含假密钥夹具，内容只拦私钥，所以候选源码目录能当材料，**但代码文件里真写了密钥不会被这里拦住**。被小八自己结束的进程（SIGTERM、SIGKILL）单独归类为“调用被结束”，不显示成“被系统拒绝执行”。

### 宿主结算

一个池节点完成后，任务保持 pending，`last_error` 标明 `awaiting_host`；后续轮询不重跑该节点、不增加 attempts。宿主提交实际回执：

```sh
xiaoba task-host-complete TASK_ID --receipt /absolute/path/host-receipt.json
```

旧任务没有宿主身份时，先用 `task-host-bind TASK_ID --host-id ID --host-entry ENTRY --host-model MODEL` 追加不可覆盖的身份审计记录，不改冻结题面。身份绑定不声明“没干活”，资格仍按实际参与记录判断。

审查回执的字段如下，TASK、HASH、HOST、时间戳按实际填写，内容必须由指定宿主实际审查产生：

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

证据引用只接受当前冻结任务或其明确关联工作任务的 `task:ID`。`review_type` 可取 new_experience、revalidation、reuse_change。系统校验身份、任务摘要、正文 B19、证据、评分、适用条件、时间和池节点执行收据，校验不通过不结算。

合格的 FAIL、NEEDS_INFO 也照样结算为 completed：宿主的结论要留下来，不因为没通过就丢掉，也不伪造成 PASS。verdict 同时写进任务来源记录（`provenance_json` 的 `verdict`）和结果第一行（`宿主审查结论：FAIL（未通过）`、`宿主审查结论：NEEDS_INFO（未通过，需补资料）`），所以 `status` 和 Agent 池页面都看得出这一票没通过。宿主是作者或执行者时拒绝审查；全部池审查员不可用时，只有用户明确选择的 `--user-chose-host` 才走宿主兜底，结果标“无独立审查”，普通补票不加这个标记。

无需审查的任务使用同一入口，JSON 只需 task_id、snapshot_sha256、reviewer_id、result，result 是宿主实际结果，不加结论抬头。接收回执、最终结果和完成事件在同一事务写入；相同回执可重放、不重复结算，不同回执拒绝覆盖。这里不批准或发布经验，也不调用确定性裁判。

### 发布验收的宿主补票

软件发版门禁至少要 2 份合格结论，可以是“1 份池节点 ＋ 1 份宿主”。门禁函数 `release_acceptance` 一直认这种组合，v1.6.9 起构包从验收数据库取证也照这个收（此前只认两个池节点，宿主结算的任务直接报错）。

**什么时候可以补。** 验收任务由 `task-host-complete` 结算（`provenance_json.kind` 是 `host_review`）、宿主这一票结论是 PASS、任务里至少有 1 份池节点结论时，才谈得上补票。至少 1 份来自池节点、入口程序与模型彼此独立、不恢复固定后台名字和备用补位这几条一条没放宽；两票都是宿主直接拒收。

**怎么补。** 提交验收任务时就声明来源工作任务：

```sh
xiaoba submit "验收说明" --memory-kind audit --work-task WORK_TASK \
  --host-id ID --host-entry ENTRY --host-model MODEL
```

这次发版确实没有来源工作任务时，用 `--no-work-task` 明确写明没有。两个都不写，构包看不出谁干过活，拒收。工作做完按上一节的回执格式 `task-host-complete TASK_ID --receipt /absolute/path/host-receipt.json` 结算。

宿主这一票的回执正文、回执哈希、`host_id`、`verdict`、`pool_run_ids` 只从 `host.receipt_accepted` 结算记录读；模型取自冻结题面里登记的宿主信息，冻结题面由回执里的 `snapshot_sha256` 绑定。手写进证据 JSON 的结论不算。

**什么情况会被拒（原因各不相同）。**

- 两票都是宿主：`至少 1 份结论来自池节点，不能两票都是宿主`。
- 宿主是这次的作者或执行者（作者和执行者从声明的来源工作任务的参与记录查，不看宿主自述）：`宿主是作者或执行者，不能计票`。
- 宿主票没绑同一份冻结题面和源码清单：`宿主回执没有绑定同一份冻结题面与源码清单`。
- 宿主结论是 FAIL 或 NEEDS_INFO：`宿主结论是 FAIL，不能算通过，构包拒收`（结论照实留在库里，不改写成 PASS）。
- 池节点那一票不是恰好一处 `FINAL_ACCEPTANCE: PASS`（写的是 FAIL、NEEDS_INFO，或 PASS 和 FAIL 都出现）：`池节点结论 … 不是 FINAL_ACCEPTANCE: PASS，宿主补票不能替它算通过，构包拒收`。规则和两个池节点的路径相同。
- 验收任务没有声明来源工作任务：`验收任务没有声明来源工作任务…`。
- 池节点那一票的完整回执（页眉和正文）缺审查结论字段、结论自相矛盾、不在这个任务的运行记录里、或节点没开启／没测试通过：分别拒收并写明是哪一份。

文件回执的审查结论、证据引用、审查类型、评分、适用条件和重复经验随正文一起保留在运行的 `output` 中，宿主补票、干活分和构包读取同一份内容。结论只写在派工卡规定的页眉即可；页眉与正文的结论矛盾时拒收。构包仍要求正文恰好一处 `FINAL_ACCEPTANCE: PASS`，不会用页眉 PASS 自动补写。旧运行中已丢失的页眉不会自动回填。

等宿主补票期间对池节点点一次“测试运行”不影响已经跑完的这一票：复用按本任务审查用途的那次运行（run_id）取，测试运行和其他用途的运行不参与，也不会让节点重新派活、不增加 `agent_runs` 预算。

### 工作区、资格与队列

- Agent 工作区根目录沿用 run.py 的 `--agent-workspace-root`，通过同一数据库的 `agent_pool.workspace_bound` 审计事件绑定；Worker、ReviewStore 与页面适配器共享该路径，不增加配置字段。重启沿用原绑定；传入不同根目录拒绝启动，不复制或搬迁既有材料。未绑定且没有有效测试资格的真实节点不派活；tmux 在场通道是例外，它只协作，不进审查和独立性计票。
- 页面和 Worker 共用 `node_runtime.readiness`，比较 spec_digest、runner_digest、证据摘要与测试完成事件。适配器更新后须重测，不发模型探针。RELEASE.json 的节点证据只保留 node_id、enabled、test_passed、independence。
- Worker 扫描全部到期任务，跳过等宿主或当前节点不足的任务，不领租约、不耗次数；跳过原因写入 `last_error` 和去重审计事件，Agent 池管理页的“待处理任务”显示这些记录。

## 冻结任务恢复操作

- 自动修复通道退役后不再有后台自修：`status`/`doctor` 里出现的历史修复记录只供审计，Worker 不租它们，也不计为可运行任务。探测失败后不得**自动**再探测，也不得让同伴去诊断失败方；探测失败只记健康状态。
- 重探通过只把后台健康改为合格，不等于任务解冻。看到 `frozen_paused` 时向用户报告并等待决定，不代替用户执行恢复，也不静默更换模型或上游。
- 恢复命令 `task-resume TASK_ID --freeze-hash HASH`；遗留的 `resume_ready` 任务同样只有用户显式恢复才推进。恢复前复核冻结哈希、成功侧执行回执，以及失败的节点在冻结之后是否重新测试通过，缺任一项就停止并说明证据缺口。两种入口都只补跑失败侧。“重新测试通过”指用户在 Agent 池给这个节点点“测试运行”并通过；上班（`session-start`）不再发真实探针，不能代替它，系统也不会自动补测。
- 放弃命令 `task-abandon TASK_ID --freeze-hash HASH`：冻结任务不打算再跑时用，任务结束为 `failed`，冻结记录、已有结论和回执都保留，事件记 `task.abandoned`；监看中心的冻结任务卡上有同样的“放弃”按钮。取消命令 `task-cancel TASK_ID` 只取消还在排队、没有开始的任务（`pending`、`retry`），正在跑的不取消，事件记 `task.cancelled`。
- 外部故障与单轮输出上限是两件事，分开处理：账户额度、限流、超时、网络、容量属外部故障，按后台级阻断处理，允许有限次数有界退避重试；认证失败同属外部故障但不可重试，只报告、等用户处理；单轮输出预算耗尽的任务属任务级失败，手动恢复返回拆分提醒，按提醒把任务拆小后重新提交，不原样重发（重发只会再撞同一个上限）。
- 以上操作都不创建备份；需要备份仍按本文的备份授权规则，由用户直接、明确要求。

## 失败恢复保留物（v1.6.2）

失败恢复只保留已认领原对象，不复制为备份；不自动清理、过期删除或进入检索。doctor与session-start显示待人工核对提醒，普通待核对不阻断接单、版本切换或新经验发布；真实发布门禁及索引错误仍按原规则拒绝。

用 xiaoba memory-retained-list 列出位置、身份与异常，重启后仍可查。用户亲自核对后逐个运行 xiaoba memory-retained-resolve ABSOLUTE_PATH --trash 移入废纸篓，或确已手动删除后运行 --confirm-deleted。两选一，无批量/自动清理。无身份的旧记录须先从清单核对并给出 --expect-dev DEV --expect-ino INO，不凭文件名盲目处理。失败按返回的实际位置、原因与重试条件处理；误捕获对象按清单人工核对。详见 docs/MEMORY_RECOVERY.md。

## 第二大脑里的内容卡片

技能卡与价值文件索引这 9 篇笔记不随发布生成。技能索引或价值文件索引变化后运行 `xiaoba content-notes-sync`；只想检查用 `--verify`。方法与知识、工作流、资产与索引、能力与技能四个分支下的其它笔记归笔记库，直接修改即可，改完按 AGENTS.md 的命令重建并校验笔记索引。详见 `docs/RELEASE_V1.6.0.md`。

## 技能与价值文件

技能在 `私人助理/skills/`，价值文件索引在 `私人助理/系统/价值文件索引/`，都不随发布。改技能不就地改：把技能目录拷到别处当草稿，在草稿里改；`xiaoba skill-review-prompt <技能名> --draft <草稿目录>` 生成审查说明，`xiaoba submit` 提交，两个后台通过后 `xiaoba skill-publish <技能名> --draft <草稿目录> --review-task <任务编号>`。改价值文件：`xiaoba knowledge-index-rebuild`。手改索引没用，运行时会核对每个技能的来历（发布基线或审查任务）。详见 `docs/RELEASE_V1.6.1.md`。


新增经验分类与受控验证的运行契约及验收条件见 docs/EXPERIENCE_TRIALS.md；技术审计仅结算审计回执，方法必须真实新执行与独立双审后用户终审。历史候选不批量迁移。

## 节点测试期限与失败诊断（v1.6.9 卡 43）

- **测试期限怎么算**：节点测试（健康检查、上班初始化）的运行期限取该节点已设的掉线时限（`no_progress_seconds`），上限 300 秒；节点没设掉线时限时仍是 90 秒。所有 CLI 类型（含 AGY 交互与 AGY CLI 两种）走同一个函数取值，不再各自写死常量；工作调用不受影响（AGY 工作调用不向 CLI 传期限，仍为 0 秒）。掉线时限只在 Agent 池编辑节点处修改，不新增节点配置字段。
- **错误尾部在哪看**：节点测试失败时，测试记录新增 `error_tail`：stderr 最后 2 KB 经 `_redact_error` 脱敏后的文字；stderr 为空时取 stdout 最后 2 KB 中不是结构化事件（不能解析成 JSON 的行）的文字，同样脱敏。Agent 池节点卡片显示“错误尾部（已脱敏 · 仅末段）”；`doctor` 结果的 `review_node_error_tails` 列出同样的内容。原始 stdout 不保存、不落盘。
- **失败分类只看诊断文字**：分类匹配只读 stderr 全文，加上 stdout 最后 4 KB 里不是结构化事件的文字；读过的源码、工具结果都在结构化事件里，不参与匹配。`call_terminated`、`permission_denied` 的判定顺序不变，既有分类测试全部照旧。

## 内置 WorkBuddy 引擎退役（v1.6.9 卡 45）

- **退役了什么**：Agent 池的 `workbuddy` 类型（WorkBuddy 桌面附带的内置精简引擎，原生 `--serve` + ACP HTTP/SSE、事件流上限 1 MiB）不再维护。`resource_workbuddy.py`、`workbuddy_login.py` 已移出仓库（移入私人助理 `_待删/内置WorkBuddy引擎-20261106`，由用户清空）；`RESOURCES` 表、身份摘要名单、登录提示、类型推断里的对应分支一并清掉。CodeBuddy CLI（`codebuddy` 类型，入口 `~/.local/bin/codebuddy`）不受影响，调用方式、参数与行为与改动前一致。
- **新建节点**：Agent 池页面类型下拉和接口都不再提供 `workbuddy`，提交会被拒绝并给出迁移办法。`workbuddy` 仍留在类型清单里，只用于识别旧数据，不当作可选项。
- **已有旧节点**：数据不动、类型不自动改。测试运行与派活直接返回错误码 `engine_retired`（HTTP 400 的 `error` 文本带迁移提示），不起进程、不写测试记录、不动 `revision`；`doctor` 在 `warnings` 里按现有警告格式列出这些节点（不参与 `ok`），Agent 池节点卡片也标出退役。
- **迁移办法**：编辑该节点，把类型改成 WorkBuddy CLI（CodeBuddy），入口填本机 `codebuddy` 的可执行路径，模型按需填写，再点测试运行。旧节点改名称、掉线时限等其它字段仍可编辑。
- **入口名 `codebuddy` 的推断**：v1.6.9 起入口名 `codebuddy` 判成 `codebuddy` 类型（原来归到 `workbuddy`）；判独立仍把两类算同一个来源，`workbuddy` 只由显式类型或 `workbuddy` 入口名留给旧节点。线上 v1.6.7 池的 CodeBuddy 节点已显式存 `agent_type=codebuddy`，切换后行为不变；线上池只读盘点（2026-11-06）没有 `workbuddy` 类型节点，不需要迁移。
- **ACP**：Agent 池退役内置引擎后就没有 ACP 通道了，ACP 另走卡 48 实测。`implementation_sha256` 随本次改动变化，池里已有节点要重新测试运行才算通过。


## 正式经验裁决（v1.6.9 卡 52b）

状态只从 `memory.reuse_revision_requested` 与 `memory.formal_*` SQLite专用事件投影，不增加表、列、索引或数据库版本。源正式经验不进入新经验初审／两轮复验。请求ID是改动事件ID；源经验ID保持稳定，版本是发布笔记的SHA-256。

宿主提交完整修订稿：在 `reuse_assessment.draft` 传 `{path, sha256}`，或使用 `memory-revision-submit --receipt FILE` 补交 `{request_id, draft:{path,sha256}}`。修订稿为绝对路径的普通UTF-8文件，最多1,000,000字节；敏感内容、非答案与新增双方括号链接拒收。没正文的请求显示“待补正文”，不派审查，可由用户关闭。改正文后必须重新审查，不能沿用旧正文的回执；最终用户操作再次复核同一正文哈希。发布只生成规范生命周期元数据，已批准正文（包括标题、空白与分隔线）不改写。

现有接入口新增 `/api/open-review/revision-body` 与 `/formal-call`；仍由本机同源／CSRF内核保护。原 `/requests`、`/dispatch` 使用改动请求ID与 `review_type=reuse_change`，证据必须包含修订稿摘要。身份卡携带完整修订稿、源ID／版本、真实任务与全部改动说明；排除源作者和本次真实任务的执行者／协作者，继续按入口程序＋模型核验独立性。现有监看进程内单独派审查线程自动准备有正文的请求，不阻塞材料投递；不另建常驻进程。缺节点／节点故障时保留待审查，不自动回退或重复执行失败节点，补选／重试沿用已有接入口。

宿主补票通过 `/api/open-review/revision-host-plan`（`request_id, plan_id`）先生成单池节点身份卡，再由 `/revision-host-receipt` 提交 `request_id, receipt`；回执绑定宿主真实任务身份、请求ID、修订稿哈希，包含 `review_type=reuse_change`、结论／正文／评分／适用条件与 `evidence_refs:[{path,sha256}]`。复用现有池选择规则：宿主没执行且不是作者时才算一票；全池不可用须用户显式选择 `user_chose_host:true`，终审材料标“无独立审查”。池回执和宿主回执均不批准经验。审查全部到齐后，包含FAIL或分歧的原始结论仍提交用户终审，不由代码假装一致通过。

用户在裁决中心操作批准／驳回。批准后新笔记成为源经验的当前发布路径，旧版本原文件移至运行目录 `memory-versions/<源ID>-<旧SHA>.md`，精确保留旧字节；索引只链接新版本。其它绑定旧版本的请求显示“版本已过期”，只能关闭或重新提交新请求，不能自动合并。宿主重新登记绑定当前源版本、同一真实任务的应用并取得该应用的用户交付后，可用 `memory-revision-resubmit --receipt FILE` 或 `/api/open-review/revision-resubmit`（`request_id, application_id, draft`）重新提交；不重复结算复用分、不沿用旧审查。驳回仅关闭本次请求。新版本路径长度不随版本数增长。新经验队列、初审和试验门禁不变。

长期未用检查选在 Worker `run_once(preflight_when_idle=False)` 的空闲分支，不调用模型。首次检查写唯一启用事件；每条经验起点取启用时间与 `published_at` 较晚者，缺发布时间取启用时间。最近调用取历史 `memory_reuse.created_at`、检索出口的专用 `memory.formal_retrieved` 事件（仅记实际返回的正式来源ID／版本及时间，不记查询正文、不加分）、交付复用结算、显式 `memory-formal-call --receipt FILE`（`source_task_id,source_sha256`）中最新者，再与起点及用户保留时间取较晚者。首次严格超过14天到期，保留后依次30／90／90天，永久分不清零。每次检查将当前新到期条目合成一个批次；已有待决批次里的条目不重复生成，用户不处理就保持待决。

裁决中心一批展示名称、最后调用时间、计时起点、当前间隔与永久分。本批“保留”对应 approve：整批记为已撤回、重新计时并升档；“删除（退役归档）”对应 reject：整批源笔记移出Vault至 `memory-retired/<源ID>-<SHA>.md`，摘链接、重建校验索引，数据库记退役，原字节可找回，不硬删除。两种操作均为用户手动决定，不由计时检查触发。

浏览器显示、审查回执版本与最终操作绑定；旧快照、正文漂移、最新回执未核验、作者／执行者身份冲突均拒绝发布。普通文件／索引异常会回滚DB与文件，失败产生的新文件移至运行目录 `_待删/` 并记说明；不生成备份。跨SQLite与文件系统没有跨进程崩溃事务：进程被强制终止时可能留下待核对文件，重试遇到冲突会停止并要求人工核对，不覆盖或删除保留版本。不把此限制称为端到端崩溃恢复通过。
