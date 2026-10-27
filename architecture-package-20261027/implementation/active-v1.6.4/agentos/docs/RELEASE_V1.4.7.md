# 小八 AgentOS v1.4.7 候选

可靠性与一致性修复。后台模型选择边界不变：AGY 跟随 Antigravity desktop，DSH 跟随 DSH Web，AgentOS 不配置模型、不选备用模型。

## 发布与入口

- 稳定入口（`xiaoba`、`verify-release`）先用标准库独立校验发布索引指向的不可变发布（文件清单指纹、逐文件哈希、无多余文件、无符号链接），通过后才加载该发布自身的校验代码做完整绑定检查。入口不再导入可修改的工作区源码。
- Worker 服务经 `launchd/worker/bin/agentos` 走稳定入口，实际运行不可变发布内的代码；服务模板不再设置指向工作区的 `PYTHONPATH`。
- `doctor` 新增 `config_consistency`：会话、`~/.agents` 与 `~/.codex` 下的已安装入口、Worker 与裁决中心服务使用的配置内容必须一致；已安装入口与发布内入口只允许默认配置路径一行不同。范围仅限上述位置，不是对所有入口的保证。
- 裁决中心的配置路径贯穿发布校验、数据库位置与审核执行，校验返回的配置不一致即拒绝。

## 审核与任务

- 审核流程集中在 `review_flow.execute_review`，CLI、bridge、裁决中心共用；8766 bridge 不再绕过发布、复用对账与退役。
- 完成任务与写入待审候选为单事务（`complete_with_memory_candidate`），幂等；提交后的收尾异常不再把已完成任务降级为重试。`constraint-hold-release` 同样把释放与写候选合并为单事务。
- Worker 循环捕获意外异常并立即释放任务租约，不再退出或让任务等满租约。
- 含未剥离推理内容的历史输出在重试时不再被复用。
- 新增 `memory-candidate-create`：宿主撰写的候选经正式命令在单事务内登记，进入正常初审队列，不发布。
- 会话在任一审核决定后都会检查是否可关闭；处于 closing 且无未决项的会话在 `session-start` 与 `session-close` 时关闭。

## 入队与后台

- `intake --submit` 在后台当前无法执行时默认不入队，返回 `blocked_local_health` 与原因；`--queue-when-blocked` 可明确要求先入队。bridge 的 intake 同样处理。
- AGY 提示词经标准输入（`--input-format stream-json`）传入，不再出现在进程参数中；输出取 `result` 事件。
- Antigravity desktop 读取失败时给出固定原因码（未运行、多窗口、界面状态不可读、模型不可用等）。路由可选 `desktop_auto_start` 与 `desktop_app_path`：仅在 desktop 未运行且有待执行任务时启动应用，默认关闭。
- 空闲轮询先做一次只读队列检查；首轮与每个心跳周期仍执行完整前置检查，领取任务前的门禁不变。
- 仅被停用后台使用的路由不再要求本机存在 Headroom 可执行文件。
- `review-gateway` 的令牌默认写入 `runtime_dir`，不再落入发布目录。

## 首轮独立审查后的修正

- 候选状态 sidecar 是文件，事务回滚不会撤销它：完成、扣留释放两条路径在未提交时恢复原 sidecar，提交后的收尾异常则保留。
- 写候选失败且未提交时，清除已落盘的候选文件与 sidecar；宿主撰写候选的文件写入失败不留残片。
- 存储层在登记候选前复核文件存在且内容哈希与证据哈希一致。
- 任务已完成但缺候选时补登记候选，不再当作幂等空操作。
- `memory-candidate-create` 用相同幂等键重试时返回原记录（`idempotent: true`）。
- “会话是否已解决”只有一处定义，审核后关闭、换日与清扫共用。
- 空闲时完整前置检查的周期可用 `worker.preflight_heartbeat_polls` 单独设置，默认沿用 `log_heartbeat_polls`。

## 已知边界

- Worker 意外异常后的租约释放作用于所有 running 任务，以单 Worker 为前提；未做多 Worker 并发测试。
- `worker_error` 与其他空闲状态一样只在内容变化或心跳时打印，持续相同的错误不会逐轮刷屏。
- 宿主撰写候选的 `provenance.kind` 沿用 `session_model_direct`（正文由会话模型撰写），`execution_mode` 为 `host_authored`。

- AGY 仍依赖 Antigravity desktop 的界面内部结构，界面改版可能导致读取失败；此时明确停止并给出原因，不静默替换。
- `desktop_auto_start` 未做真实的关闭后拉起测试。
- 未启动真实 8766 HTTP 网关做端到端测试，未测试并发与进程中途退出。
