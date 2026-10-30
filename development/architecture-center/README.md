# 架构与升级发布中心

2026-10-27：按用户授权新增监看中心模块。HTML 拓扑作为修改模块规范、生成待发布任务、跨 Agent 接续及用户正式发版的统一入口。此目录是开发实现，未在用户 Mac 上安装或执行生产切换；v1.6.4 活动版及 v1.6.5 冻结材料保持原样。

## 用户流程

在监看中心打开“架构与升级发布”，点击拓扑节点，再选“编辑所属模块规范”。填写修改目标、源码基线和提案，保存生成独立改动 ID。待发布页展示文件差异、源码哈希、当前进展、Agent 接续记录、检查结果和最终发布回执。

普通运行只有读取、调用和写入结果。这里的编辑属于明确的更新迭代，不即时写活动源码、规则或配置。两轮经验核验、任务综合评分、自动择优和交付自动回流仍是待实现模块；编辑其规范不表示运行能力已实现。

“检查当前候选”只检查清单、文本、Python/JS/JSON 语法。接续 Agent 仍须完成真实功能验证、适用的独立审查和正式构包。正式包登记后，用户在页面运行原生预检，阅读目标版本和具体改动，再点击“正式发版”。原生脚本的包签核、源码/配置绑定、审查证据、预检时效、现场核对和回读全部保留。没有受审包、固定执行器或有效预检，按钮不可用。

正式切换仍由用户所在机器执行；本实现将用户操作从命令行迁移到监看页面，没有用前端提示代替实际切换。经验审批仍走原裁决流程。

## 接入现有监看

仓库根目录启动现有扩展运行器：

```bash
python3 -B development/review-ingress/run.py \
  --runtime /absolute/path/to/isolated-runtime --port 8800 \
  --updates-workspace /absolute/path/to/upgrade-workspace
```

打开运行该服务的机器上的 `http://127.0.0.1:8800/`，选择“架构与升级发布”；直接地址为 `/architecture/`。原监看导航和审查接入口保留。云端回环地址不等于用户 Mac 地址。

接入真实现有配置时，沿用运行器的 `--config`、`--core-source`、`--monitor-source`。本模块对运行器新增路由、导航和独立更新数据库，不重写原监看快照。安装过程仍需有权限的部署任务完成。

原生发布桥接需由部署者在宿主上明确配置：

```text
--switch-script /absolute/path/to/reviewed/switch_release.py
--switch-sha256 <该固定脚本的已核对 SHA-256>
--switch-python /absolute/path/to/compatible/python3
```

页面及待发布任务不能选择任意脚本、解释器或 shell 命令。脚本必须与受审目标包中的脚本逐字节一致，原生命令会再次核验。不配置时，提案及接续可用，正式发版不可用。本云端没有用户 Mac 的配置、LaunchAgent 和活动目录，未验证真实生产切换。

在线托管的架构页仅展示拓扑和入口说明；它没有本机服务和写入令牌，不会自行连接用户电脑。

## 模块与文件分类

`MODULES.json` 登记模块 ID、规范文件、拓扑节点、契约版本、依赖和可编辑候选路径。九个模块为任务计划、任务执行、交付学习、两轮核验、审查调用、综合评分、业务工具、数据治理、升级中心。规范放在 `modules/<模块>/SPEC.md`。新实现归入对应 `development/<模块>/`，每项任务保留明确模块归属。

这是新增开发区的分类边界，不是已将旧 CLI/store/orchestrator 全部物理拆分。历史与冻结目录不移动、不就地改造。后续拆分须明确依赖、兼容、数据迁移和正式包映射，一起验证发布。新模块或写入范围变更本身也需审查更新注册表。

更新工作区结构：

```text
upgrade-workspace/
  upgrade-center.sqlite3       # 改动状态、执行权、幂等发布与审计链的权威记录
  待发布/<change-id>/
    change.json               # 可读状态与源码清单投影
    ACCESS.json               # 模块候选写入边界
    HANDOFF.md                # 目标、基线、进度、下一步
    events.jsonl              # 审计投影
    files/<repo-relative>     # 实际提案/源码
    diffs/<path-sha>.patch    # 与基线的差异
    artifacts/package/       # 接续 Agent 构建的受审正式包
    reports/preflight.json   # 原生预检回执
  版本记录/<change-id>/RELEASE-RECORD.json
```

每条记录关联基线说明、逐文件前后 SHA-256、正式包摘要及映射、版本、修改历史和实际回读。实际候选源码和受审包留在同一任务目录；不自动复制备份或删除旧任务。数据库是权威，投影文件不能通过手改获得批准或发布权限。文件与数据库并非跨文件系统原子事务；异常后不匹配内容会阻断验证，需盘点接续。

## 任意授权 Agent 接续

详见 [HANDOFF-PROTOCOL.md](HANDOFF-PROTOCOL.md)。Agent 使用本机已有授权，读取文件夹并调用 JSON stdin 入口，认领、续租、限定路径写入、登记正式包、记录检查点和完成交接。执行权默认 15 分钟，修订号避免覆盖，过期后新 Agent 可接续；旧执行权不能再写入。Agent 名称只是审计标签，不是认证。工作区依赖本机所有者文件权限，HTTP 写入沿用监看的本机 Host、同源 Origin、页面令牌及 JSON 检查。

只读数据和待发布任务不构成新增机器权限。CLI 不提供正式发布操作，页面只对具体预检产生的一次性确认接受发布；这些本机门禁不能替代操作系统的账户隔离。

预检后改文件、基线、正式包或计划须重新核验；重复请求只结算一次，同一工作区只允许一个正式发布在运行。原生异常会暂停并记录失败，不重试。若服务中断或原生进程未明确返回，保留“发布中/待人工核对”，不能自动重启切换；需由部署者核对原生现场后开展恢复任务。

## 发布结果未知（release_unknown）

发布驱动返回 `requires_manual_readback`、驱动抛异常或返回无法解析的结果、或重启时发现遗留的 `publishing` 记录，候选都进入持久状态 `release_unknown`（页面上是“发布结果未知 · 必须先人工核对”）。这不是普通失败：结果可能已生效，因此不能重发，也不能沿用旧预检。

- 工作区里只要有一条 `release_unknown`，`prepare_release` 和 `publish` 都拒绝，报错写明要先核对哪一条候选。该候选本身也不可修改、认领或重新检查。
- 恢复入口 `acknowledge_release_unknown(cid, expected_revision, conclusion, evidence)`，页面为“登记核对结论”，HTTP 为 `POST /api/release-center/acknowledge`。结论只有两种：`applied`（已生效）进 `published` 并写带 `verified_by: manual_readback` 的版本记录；`not_applied`（已证实没有改动或已退回）进 `release_failed`，允许重新预检。`evidence` 必须写明核对依据，结论和依据都写进审计事件 `release_unknown_acknowledged`。
- 恢复动作只记录人工核对结论，**绝不调用发布驱动**；恢复后是否重发仍由用户重新预检、重新确认。
- 明确的干净失败（没有 `requires_manual_readback`）行为不变：仍记 `release_failed`，可重新预检。
- 重启恢复在 `Center` 初始化时执行：遗留的 `publishing` 记录改记为 `release_unknown`，对应发布行同步改状态；审计链损坏时不写入，遗留的 `running` 行继续占住单飞锁。

## 验证

```bash
python3 -B -m unittest discover -s development/architecture-center/tests -v
```

测试驱动仅使用隔离夹具，不是生产发布成功证据。验证记录见 [TEST-RESULTS.md](TEST-RESULTS.md)。
