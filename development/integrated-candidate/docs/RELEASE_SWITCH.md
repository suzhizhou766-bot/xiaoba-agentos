# 固定发布切换命令

命令：发布包里的 `agentos/scripts/switch_release.py`。版本、前置发布和编号步骤来自经过哈希验证的包，命令代码不写死版本号。正式执行由用户在个人终端运行；Agent 只做只读核查和隔离演练。

下面的 `PACKAGE` 代表最终验收通过的不可变发布包绝对路径，`PREFLIGHT` 代表用户保存的预检 JSON。此处是用法说明，不代表当前候选已经可以切换。

```sh
python3 -B PACKAGE/agentos/scripts/switch_release.py plan --package PACKAGE
python3 -B PACKAGE/agentos/scripts/switch_release.py preflight --package PACKAGE > PREFLIGHT
python3 -B PACKAGE/agentos/scripts/switch_release.py apply --package PACKAGE --preflight PREFLIGHT
python3 -B PACKAGE/agentos/scripts/switch_release.py readback --old-pid OLD_PID
```

`plan` 是缺省用法。计划、预检、读回均不写文件、不启停服务、不调用模型；保存 stdout 是调用者自己的输出操作。预检有效期 300 秒，执行前重新检查全部门禁与现场指纹，过期或漂移则零写入拒绝。每次失败后的再执行必须取得新的预检，不复用失效回执。

## 成功与失败

执行顺序：新包入位 → 停 Worker 并确认旧进程退出 → 替换运行源码 → 安装两处入口 → 更新配置中的发布版本绑定 → 编号步骤 → 生成树合并与索引重建 → 发布索引 → 启动新 Worker 并等待就绪 → 完整读回。

必须同时满足：旧 PID 不存活，新 PID 不同；服务与绑定配置的唯一 Worker 子进程一致；运行状态中的代码和清单指纹与磁盘一致；两处入口以正式配置验证目标版本；doctor 通过；KnowLP 用正式生成器的原格式逐文件读回通过。读回健康但版本不是预期目标，也判失败。

队列中的 running/retry/frozen_paused/resume_ready 和领过的 pending 都阻止切换；从没被领过的 pending（attempts=0、没有租约）可以留到切换后由新版本处理（发布说明第三十八节）。未释放租约包括已过期但尚未清除的租约。未完成修复任务同样阻止切换。命令不会删除任务、释放租约或替用户解冻。

外部命令检查返回码；`launchctl bootout` 使用 `gui/UID/LABEL` 服务标识。启停后的等待检查真实进程与服务状态。独立读回若没有传入切换前 PID，只能证明当前运行态，结果明确标为 `restart_verified=false`，不证明历史重启成功。

中途失败：先证明新服务和新 Worker 已停止，再按逆序恢复原字节、权限及原不存在状态，恢复原来的运行版本，并再次读回。只有全部恢复门禁通过才写“已退回”；出现并发写入、退回不完整或旧服务无法恢复，写“退回失败，需要人工处理”，不启动混合状态。

恢复原文只保存在进程内存；正常结束不留下副本，不新建备份。不修改已有备份或既有旧发行。强杀、掉电后内存无法恢复，此时需要人工检查，命令不承诺自动恢复。

## 编号步骤

声明文件为包内 `agentos/docs/DEPLOYMENT.json`，包含 `schema_version`、前置源码清单哈希和有序 `steps`。每步含 `id`、`check`、`execute`、`undo`。执行历史保存在 `.runtime/release-steps.json`，不增加数据库表。

每一步的整个声明都有内容哈希。历史记录必须能回到当时的不可变包验证；旧编号内容变化则预检失败。纠正旧步骤必须增加新编号，不能修改已执行步骤。编号步骤的原始字节也由同一内存日志撤销。

目前实现两种受限处理器：

- `install_absent_file`：仅在共享 skills/工具/系统范围安装不存在的文件；相同内容跳过，不同内容拒绝覆盖，禁止凭据和配置目录。
- `config_set`：只允许已审查的固定迁移：将已退役自动恢复字段 `recovery.automatic_actions` 设为空列表，以及把三个内容路径移到助理目录的固定位置（`skills.root_dir`、`skills.index_path`、`knowledge.index_path`，步骤 0002～0004）。检查原值并保留其它字段。自动恢复限制的是切换命令能**写**什么，不是配置能**加载**什么：加载侧缺省与空列表等价，非空列表拒绝（见 `src/xiaoba_agentos/config.py`）。
- 线路迁移唯一例外（用户 2026-11-01 授权）：`0005-agy-configured-model` 把 `cli_routes.local_agy` 从声明中逐字段相同的旧路由（`driver=agy`、`selection=local_account`、原 `desktop_debug_port`，无 `model`）整体迁为 `{"driver":"agy","selection":"configured_model","model":"Gemini 3.8 Flash (High)"}`。编号、路径和执行值由脚本固定校验；其它线路、模型、认证及任意配置补丁继续拒绝。现场与 `check.value` 不同则预检零写入拒绝。失败时配置原始字节和步骤历史都由内存日志撤销。该步骤执行后不再比对现场路由值，用户以后可以自行换模型；历史步骤内容哈希仍须一致，其它步骤的结果比对保持不变。

甲版声明的专属步骤为空；当前声明包含 0002～0005，一般版本绑定仍只更新 `release.expected_version`。切换前由用户把 `integrations.agy_command` 和 `paths.agy_command` 两处都改为 `/Users/suzhizhou/.local/bin/agy`；AGY 路由由切换中的 0005 迁移，切换后不用再改路由。新版本读回的 doctor 必须全部通过，否则自动退回。数据库结构迁移、新的数据格式迁移和新的处理器需要另版明确实现与验收，遇到结构版本变化直接拒绝。

## 内容保护与首次使用

第二大脑以旧生成树、当前原文和新生成树比较：生成内容未变则保留现场原文；如果现场恰好是完整旧生成内容加追加段，更新生成部分并逐字节保留追加段；生成正文内部有编辑则拒绝覆盖，不做模糊合并。用户经验不由此命令改写，既有历史笔记不覆盖。未解决的冲突必须在候选阶段交用户处理，不能为通过预检扩大写入范围。

首次包含本命令的发布必须从已验证包运行此命令，不再手写版本脚本。正式验收还需要复制现场的完整演练和故障退回证据。隔离单元测试、当前线上只读读回、AGY/DSH 独立审核、复制现场演练、用户终端正式切换是不同证据，不能相互替代。

## 发布目录只读（v1.6.5 起）

切换成功、读回通过后，切换命令把新发布目录设为只读（文件 0444，可执行文件与目录 0555），结果在 `release_sealed` 里报告。这样任何进程都不能往发布目录里写文件；以前一个 Python 字节码缓存文件就会让入口的发布校验失败。封不上只报告、不退回已成功的切换。`doctor` 的 `release_read_only` 字段报告当前发布目录是否已只读，只作报告，不改变 `ok`。以后确需整理旧发布目录时，先由用户决定，再 `chmod -R u+w` 该目录。
