# v1.6.5 运维

```sh
XIAOBA="$HOME/.agents/skills/xiaoba-agentos/scripts/xiaoba"
"$XIAOBA" doctor --json
"$XIAOBA" status --json --limit 10
"$XIAOBA" session-start
"$XIAOBA" review-list --stage all
```

上班先验证 AGY+DSH 两个真实探针。只读检查不创建任务；session-start 更新健康会话状态。不要从旧探针推出此刻远程可用。Worker 身份为小八 AgentOS Runner；沙箱禁止 kill(pid,0) 时可能误报 alive=false，需授权只读进程确认，不盲目重启。

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
- AGY 路由采用 `agy_cli_settings`，绑定 `settings_path=/Users/suzhizhou/.gemini/antigravity-cli/settings.json`，用用户在 agy CLI 里上次选定的模型；不再读取或启动 Antigravity 桌面。线上旧写法（`local_account` 加 `desktop_debug_port` 等桌面字段）仍可加载，按同样方式读 CLI 设置，桌面字段被忽略；切换后的登记步骤会把它改成新写法。
- 详见 `docs/AGY_DESKTOP_FOLLOW.md`（后台模型选择边界）与 `docs/RELEASE_V1.6.5.md` 第十四节。
- 切换（`switch_release.py`）前的两项检查（v1.6.5 起，见发布说明第三十八、四十、四十一节）：旧版本 doctor 只允许因“后台线路就绪”（`local_cli_routes`）不通过——报告须明确 `ok` 为假、`paths` 非空且全部为真、线路检查（`local_cli_routes`）明确不通过、其它各判定检查都在且明确通过（`ok` 为 true）；doctor 通过时票据也记下不参与判定的失败项——失败原文写进切换票据，其它任何情况照旧拒绝；退回后读回旧版用同一条规则；切换后读回时新版本 doctor 必须完全通过，否则自动退回。队列里从没被领过的 pending 任务（attempts=0、没有租约）可以留到切换后由新版本处理；运行中、重试中、冻结、可恢复的任务和任何未释放的租约照旧挡住切换。

## 备用后台 qodercli（v1.6.5 起）

分派前补位须有可验证来源：真实失败探针，或已记录的外部故障（`external_upstream`、`external_auth`，这一种不需要探针标记）。只有内部或未知类别的降级、又没有失败探针证据时，不能借此启用备用；启用的主后台少于两个时，领任务前无条件挡住，备用不补，不会只用一个主后台加备用接单（备用只顶替两个主后台中不可用的那一个）；补位事件的 `health_evidence` 由闸门、派发与构包共用判定。被冻结的任务仍须用户显式恢复。

- 发布包配置模板带 `qoder_review`（`standby: true`）。线上生效配置要由用户同意后另行登记（切换命令按设计不改后台线路）：在生效配置的 `cli_routes` 里把 `local_agy` 改成 `agy_cli_settings` 新写法、加 `local_qoder`，`agents` 里加 `qoder_review`，`integrations` 里加 `qoder_command`，内容与包里模板一致；再重启 Worker（`launchctl kickstart -k gui/$(id -u)/com.xiaoba.agentos`）并运行 `doctor`。不登记时小八照旧只用 AGY + DSH。
- 用 qodercli 的前提：本机已安装标准版 qodercli 并登录。`local_qoder` 采用 `qodercli_settings`，绑定 `settings_path=/Users/suzhizhou/.qoder/settings.json`，用用户在 qodercli 里上次选定的模型和该模型的思考强度，每次调用前读取、通过 CLI 单次参数执行；不读取 Qoder 桌面，不使用 Qoder CN 或 qoderclicn。读不到明确选择、思考强度不支持、或 CLI 实际换了模型，都停止并报告。 备用调用时关闭工具、MCP 为空、关闭 hook、不保存会话、在临时空目录运行，所以不接需要工具、联网或按名字分工的任务，细则见 `STANDBY_POLICY.md`。
- 三个后台的常规任务共用后台等待，不设 300 秒总截止；`timeout_seconds` 只保留给显式健康探针。AGY 原生等待为 0（直到完成），Qoder 使用 stream-json。运行中记录安全进度并续租，进程存在不等于上游已接单。300 秒是“连续无进展”窗口，不是补位许可：连续 300 秒没有真实进展就判为卡住，按外部上游故障记录（用户 2026-10-20 定：本地只能观察到 CLI 不再产出，分不清是上游停算还是本地挂起，统一按外部处理；代码里 `NoProgressTimeout` 的类别就是 `external_upstream`），先结束原来的 CLI 进程组（CLI 以独立进程组启动，整组回收，不留子进程），再按补位规则判断要不要补（心跳、PID、续租都不刷新计时）。CLI 自己报出的内部错误、工具调用标记、思考泄露等仍按内部处理，不补。能补的是外部上游故障、外部认证故障，以及真实的健康探针失败（不论原因，用户 2026-10-21 定）；任务中途的内部故障、输出预算耗尽、思考泄露不补。完整判断表见 `docs/STANDBY_POLICY.md`。
- 补位情况看 `status`、任务结果的 `standby_substitution` 字段和通知；被补位的主后台一直保持 `external_blocked`，直到用户下次“上班”时的真实探针重新记录它健康为止；探针仍失败就按这次失败重新记为不健康（外部故障仍是 `external_blocked`），不会因为“上班”本身而解除。
- `doctor` 只按两个主后台判断能否接单；备用 Qoder 的 CLI 是否可用单独写在 `standby_cli_routes`，坏了不会挡住上班、接单和安装，但需要用户修好，否则主后台出故障时就没有备用可补。

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
