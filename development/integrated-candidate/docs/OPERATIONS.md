# v1.6.6 运维

```sh
XIAOBA="$HOME/.agents/skills/xiaoba-agentos/scripts/xiaoba"
"$XIAOBA" doctor --json
"$XIAOBA" status --json --limit 10
"$XIAOBA" session-start
"$XIAOBA" review-list --stage all
```

上班不发真实探针，只做本地检查：节点是否开启、测试资格是否有效、入口是否还在。节点要真实连通时，由用户在页面手动测试运行。只读检查不创建任务。Worker 身份为小八 AgentOS Runner；沙箱禁止 kill(pid,0) 时可能误报 alive=false，需授权只读进程确认，不盲目重启。

正式变更：冻结候选源码 → 重建技能/知识/源码清单 → 单元测试、单根Vault和KnowLP检查 → 临时 runtime/index 的生产模式双Agent验收 → 验证验收与同源哈希绑定 → 暂停Worker → 发布及入口切换 → 重载Worker → doctor/status/索引复验。不跨版本迁移授权结论。备份仅在用户直接、明确要求时执行；发布授权不包含备份授权，未经明确授权应跳过内置备份步骤。详见 `docs/USER_RULES.md`。

回滚使用已存在且经验证的发布内容，或用户明确要求创建的可用备份，恢复运行源码、release index、Vault、KnowLP及skill入口，再重启Worker、验证指纹；不得为回滚自行新建备份。任务数据库不回退到旧快照，避免丢失新审计记录。历史目录不参与当前检索。

`session-close` 不是批准。`review-list`/`review` 保留同哈希双审；未批准候选继续私有保留。禁止无任务ID批量发布。

## 现行运维要点

- Worker 服务运行的是不可变发布内的代码。提升新版本后必须重装或重启 Worker 服务，否则进程仍在旧发布上运行。
- 切换成功后会把新发布目录封成只读（文件 0444，可执行文件与目录 0555），结果记在切换返回的 `release_sealed` 里；封不上只报告，不退回已成功的切换。`doctor` 的 `release_read_only` 只报告当前发布目录是否仍是只读，不影响 `ok`。不要手工在发布目录里以会写字节码缓存的方式运行或导入 Python：多出一个字节码缓存文件就会让入口的发布校验失败，小八拒绝运行（v1.6.5 起把发布目录封成只读，正是为了防这一件事）。Worker 服务照常运行发布内的代码，它以不写字节码的方式启动，不受影响。
- 裁决后端由小八监看中心（`http://127.0.0.1:8799/`，「裁决中心」标签）按需拉起并看护：监看中心开启时运行，关闭时一并停止，没有独立网页，原 `127.0.0.1:8797` 地址与 `com.xiaoba.review-center` LaunchAgent 已停用。监看中心没开时不能在浏览器里裁决，待审队列也不会同步到第二大脑；重新开启后补同步。CLI 的 `review-list`/`review` 不受影响。
- 裁决后端必须与 Worker 使用同一份 `XIAOBA_CONFIG`。监看中心默认读取 Worker LaunchAgent 里的配置路径；裁决后端运行时把自己实际使用的配置记录在 `<runtime_dir>/review-center/backend-state.json`，`doctor` 的 `config_consistency` 据此把它列为 `review_backend` 一并比对：内容不一致判失败，路径不同但内容相同给警告。后端没在运行（或记录已过期）时跳过，不算失败。旧的 `com.xiaoba.review-center` LaunchAgent 若被重新启用，仍按原规则比对。
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

### 工作区、资格与队列

- Agent 工作区根目录沿用 run.py 的 `--agent-workspace-root`，通过同一数据库的 `agent_pool.workspace_bound` 审计事件绑定；Worker、ReviewStore 与页面适配器共享该路径，不增加配置字段。重启沿用原绑定；传入不同根目录拒绝启动，不复制或搬迁既有材料。未绑定且没有有效测试资格的真实节点不派活。
- 页面和 Worker 共用 `node_runtime.readiness`，比较 spec_digest、runner_digest、证据摘要与测试完成事件。适配器更新后须重测，不发模型探针。RELEASE.json 的节点证据只保留 node_id、enabled、test_passed、independence。
- Worker 扫描全部到期任务，跳过等宿主或当前节点不足的任务，不领租约、不耗次数；跳过原因写入 `last_error` 和去重审计事件，Agent 池管理页的“待处理任务”显示这些记录。

## 冻结任务恢复操作

- 自动修复通道退役后不再有后台自修：`status`/`doctor` 里出现的历史修复记录只供审计，Worker 不租它们，也不计为可运行任务。探测失败后不得**自动**再探测，也不得让同伴去诊断失败方；探测失败只记健康状态。
- 重探通过只把后台健康改为合格，不等于任务解冻。看到 `frozen_paused` 时向用户报告并等待决定，不代替用户执行恢复，也不静默更换模型或上游。
- 恢复命令 `task-resume TASK_ID --freeze-hash HASH`；遗留的 `resume_ready` 任务同样只有用户显式恢复才推进。恢复前复核冻结哈希、成功侧执行回执与失败侧新鲜真实健康，缺任一项就停止并说明证据缺口。两种入口都只补跑失败侧。这里的“失败侧新鲜真实健康”取自用户重新“上班”（`session-start`）时跑的真实探针记录，不是自动补探的结果；缺这条记录时请用户重新上班，不要自己再探一次。
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
