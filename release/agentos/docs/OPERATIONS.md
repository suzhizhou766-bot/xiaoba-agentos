# v1.6.1 运维

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
- 裁决后端由小八监看中心（`http://127.0.0.1:8799/`，「裁决中心」标签）按需拉起并看护：监看中心开启时运行，关闭时一并停止，没有独立网页，原 `127.0.0.1:8797` 地址与 `com.xiaoba.review-center` LaunchAgent 已停用。监看中心没开时不能在浏览器里裁决，待审队列也不会同步到第二大脑；重新开启后补同步。CLI 的 `review-list`/`review` 不受影响。
- 裁决后端必须与 Worker 使用同一份 `XIAOBA_CONFIG`。监看中心默认读取 Worker LaunchAgent 里的配置路径；裁决后端运行时把自己实际使用的配置记录在 `<runtime_dir>/review-center/backend-state.json`，`doctor` 的 `config_consistency` 据此把它列为 `review_backend` 一并比对：内容不一致判失败，路径不同但内容相同给警告。后端没在运行（或记录已过期）时跳过，不算失败。旧的 `com.xiaoba.review-center` LaunchAgent 若被重新启用，仍按原规则比对。
- `intake --submit` 返回 `blocked_local_health` 时任务未入队，按 `blockers` 处理后重新提交。
- 宿主撰写候选使用 `memory-candidate-create --title … --body-file …`，不要直接写数据库。
- AGY 路由可设置 `desktop_auto_start: true` 与 `desktop_app_path`（`.app` 绝对路径），仅在 desktop 未运行且有任务时启动；默认关闭。
- 详见 `docs/RELEASE_V1.4.7.md`。

## 冻结任务恢复操作

- 自动修复通道退役后不再有后台自修：`status`/`doctor` 里出现的历史修复记录只供审计，Worker 不租它们，也不计为可运行任务。
- 重探通过只把后台健康改为合格，不等于任务解冻。看到 `frozen_paused` 时向用户报告并等待决定，不代替用户执行恢复，也不静默更换模型或上游。
- 恢复命令 `task-resume TASK_ID --freeze-hash HASH`；遗留的 `resume_ready` 任务同样只有用户显式恢复才推进。恢复前复核冻结哈希、成功侧执行回执与失败侧新鲜真实健康，缺任一项就停止并说明证据缺口。两种入口都只补跑失败侧。
- 单轮输出预算耗尽的任务：手动恢复返回拆分提醒，按提醒把任务拆小后重新提交，不原样重发；真实认证、网络、容量故障仍按后台级阻断处理并要求重新探测。
- 以上操作都不创建备份；需要备份仍按本文的备份授权规则，由用户直接、明确要求。

## 失败恢复保留物（v1.6.1）

失败恢复只保留已认领原对象，不复制为备份；不自动清理、过期删除或进入检索。doctor与session-start显示待人工核对提醒，普通待核对不阻断接单、版本切换或新经验发布；真实发布门禁及索引错误仍按原规则拒绝。

用 xiaoba memory-retained-list 列出位置、身份与异常，重启后仍可查。用户亲自核对后逐个运行 xiaoba memory-retained-resolve ABSOLUTE_PATH --trash 移入废纸篓，或确已手动删除后运行 --confirm-deleted。两选一，无批量/自动清理。无身份的旧记录须先从清单核对并给出 --expect-dev DEV --expect-ino INO，不凭文件名盲目处理。失败按返回的实际位置、原因与重试条件处理；误捕获对象按清单人工核对。详见 docs/MEMORY_RECOVERY.md。

## 第二大脑里的内容卡片

技能卡与价值文件索引这 9 篇笔记不随发布生成。技能索引或价值文件索引变化后运行 `xiaoba content-notes-sync`；只想检查用 `--verify`。方法与知识、工作流、资产与索引、能力与技能四个分支下的其它笔记归笔记库，直接修改即可，改完按 AGENTS.md 的命令重建并校验笔记索引。详见 `docs/RELEASE_V1.6.0.md`。

## 技能与价值文件

技能在 `私人助理/skills/`，价值文件索引在 `私人助理/系统/价值文件索引/`，都不随发布。改技能不就地改：把技能目录拷到别处当草稿，在草稿里改；`xiaoba skill-review-prompt <技能名> --draft <草稿目录>` 生成审查说明，`xiaoba submit` 提交，两个后台通过后 `xiaoba skill-publish <技能名> --draft <草稿目录> --review-task <任务编号>`。改价值文件：`xiaoba knowledge-index-rebuild`。手改索引没用，运行时会核对每个技能的来历（发布基线或审查任务）。详见 `docs/RELEASE_V1.6.1.md`。
