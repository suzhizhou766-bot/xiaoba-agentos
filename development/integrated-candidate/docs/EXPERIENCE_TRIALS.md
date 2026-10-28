# 经验候选分类、任务卡与受控验证

## 流程

技术审计用 audit（仅审计）结算：任务结果与审计回执原子完成，不产生经验候选。通过官方工具生成的技能审查说明自动识别为审计；其它技术审查显式使用 submit --memory-kind audit。方法和规则仍保留任务完成与候选原子结算，不削弱恰好一次和事件链。

rule（用户偏好或明确规则）走原有两次人工裁决；主力只有在有用户明确依据时才能选择此类。method（方法、参数、修复经验）是新候选默认：待初审 → 初审批准 → 待隔离验证 → 真实本机执行 → AGY/DSH独立审查 → 原卡待终审 → 用户终审 → 原有敏感检查及索引复验。两个审查者都PASS（通过）且真实执行满足断言才可终审；缺一侧、仅模型复述、旧证据、失败或运行中都不能通过。

验证关系绑定候选SHA-256（内容摘要）、当前初审回执、任务卡摘要、执行回执摘要、验证任务。重新初审使旧验证失效；新失败或未完成验证覆盖旧成功。终审与发布都在数据库事务中检查门禁，发布还检查终审记录绑定同一验证任务。验证任务只结算审计，不再产生第二张经验卡。

未发布候选可在明确授权的真实任务中通过独立待验证入口回读，以及在绑定该应用的隔离验证任务中提供；不进入普通长期记忆检索，不增加published（已发布）经验的复用计数。本版不替换已发布经验原有的复用计数、自动恢复与退役机制；受控验证门禁针对本版新登记方法的首次采用和显式重新初审。提交同类problem_key（问题标识）的普通任务仍只返回试验建议，不自动注入候选正文，也不执行候选命令。实际应用、交付回流及兼容边界见 EXPERIENCE_LOOP.md。

## 入口

```sh
XIAOBA="$HOME/.agents/skills/xiaoba-agentos/scripts/xiaoba"
"$XIAOBA" submit '独立技术审查说明' --memory-kind audit
"$XIAOBA" memory-candidate-create --title '方法名称' --body-file /绝对路径/候选.md --author host --kind method --problem-key stable-problem-key
```

主力先生成任务卡，写明目标、允许输入与改动范围、验收条件、停止条件、回执；外派由用户转交，报告交回后主力核实，不自动向其它对话派发。AGY/DSH是独立审查者，不承担该方法实现。任务卡没有执行过不算验证。

```sh
"$XIAOBA" memory-trial-match --problem-key stable-problem-key
"$XIAOBA" memory-trial-match --query 同类问题关键词
"$XIAOBA" memory-trial-run 候选任务编号 --card /绝对路径/试验任务卡.json
"$XIAOBA" memory-trial-status 候选任务编号
"$XIAOBA" review-list --stage all
```

memory-trial-run（执行试验）仅在当前用户已授权任务范围内由主力显式调用；匹配和Worker（任务执行器）不会自动执行卡片命令。范围不明、需访问生产或敏感资料时先澄清，不借候选文字扩大授权。两次经验批准只接受用户针对该候选的决定。

## 任务卡输入

JSON（结构化文本）必须有 title、problem_key、objective、scope、acceptance、stop_conditions 六个非空文本字段；另有 workspace 与 cases。workspace 必须是当前配置 runtime_dir/trial-workspaces/ 下独立子目录，主力先放入最小授权夹具；不复制真实生产库、账号或凭据。

```json
{
  "title": "指定案例的局部验证",
  "problem_key": "stable-problem-key",
  "objective": "写清本次验证的具体方法与预期行为",
  "scope": "只用本机临时夹具，不接触生产和用户原件",
  "acceptance": "实际断言具体结果，独立双审后交用户终审",
  "stop_conditions": "断言失败、依赖缺失、越出授权范围就停止采用",
  "workspace": "/当前runtime_dir/trial-workspaces/唯一案例目录",
  "evidence_files": ["case.py"],
  "cases": [{
    "argv": ["/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9", "-I", "-B", "case.py"],
    "expect_stdout": "CASE_ASSERTIONS_PASSED",
    "timeout_seconds": 30
  }]
}
```

上述解释器路径已在本机验证；其它本机环境先确认实际路径。case.py须包含具体业务断言，通过后才打印指定标记；标记本身不证明方法有效。把被审查的测试实现放入evidence_files（审查输入文件）；每个最多32KiB、最多8个。回执记录输入夹具摘要、指定文本全文、执行参数、开始/结束时间、返回码和输出，交给两个审查者检查方法和验收条件是否对应。复杂情况不能只交一行成功文字。

## 执行限制与失败

使用本机sandbox-exec／Seatbelt（macOS权限沙箱）。缺少沙箱就停止，不降级到无限制执行；仅供已核对的本机测试，不能用作恶意软件运行环境。直接网络访问被拒绝，文件写入只允许试验目录及/dev/null；用户目录、外接卷、用户临时目录的数据读取只放行该试验目录。保留系统程序所需读取及系统服务查询。

不得在试验目录放凭据或无授权素材，不运行常驻服务。夹具最多5000项且不含软链、硬链。只接受参数列表，不拼接shell（命令解释器）字符串；每例1至30秒，最多4例，单文件写入上限1MiB。输出超过16KiB、超时、敏感内容或断言缺失都算失败；结束后终止仍处于该进程组的子进程。

隔离配置可能使用户目录中的依赖不可读；如实记录缺失，准备最小授权夹具或调整案例，不放宽沙箱以追求绿测。不向AGY/DSH复制凭据。实际回执另存runtime_dir/trial-receipts/，权限600；排队失败仍报告回执位置。没有经过独立双审的回执不等于验证通过。

资格不足时终审批准会被拒绝，不生成批准记录。首轮失败可执行第二轮；两轮失败则退役，第三次在实际执行前阻断。已有执行或待审任务占用当前轮次，重复入口明确拒绝。人工终审 reject/revise/retire 后，自动结算、恢复、补材料和迁移不能推翻该决定。候选正文或当前初审变动会使旧资格失效，不重置已消耗轮次。

## 裁决页面与兼容

新的验证状态、任务卡、执行回执和两个审查者意见显示在原候选卡。待验证时不能点击终审通过；可复制验证任务请求，粘贴到Agent主入口并补充具体案例。

核心数据库仍为 schema 7；本候选另有显式的验证扩展版本 1，增加 meta、lineages、records 三表和相应候选状态。普通 Store.initialize 不自动安装扩展。memory-trial-run 在执行前检查支持版本、必要列、非空、主键、外键、两轮/状态约束与唯一性；异常结构拒绝执行，不自动修补。

扩展安装仅在被授权的目标数据库显式调用 validation_migration.apply_validation_schema_migration(database_path, dry_run=True/False)。dry_run 对不存在路径不创建文件/父目录，对已有库使用只读连接；DDL 在同一事务内，失败完整回滚。rollback_validation_schema_migration 同样原子。此处不是生产迁移授权，安装扩展不等于软件发布。

已有无分类快照的候选保留原人工流程。旧 method 记录通过 experience_workflow.migrate_legacy_validation_records(conn) 在调用方事务中显式迁移：单一关联任务必须有当前初审、真实控制器回执、原生审计结算和独立审查；明确失败保留失败，无法证明的进入 awaiting_check；人工结束状态不改动。迁移记入原生事件链，不猜测补 PASS。

裁决页面属于包外工具，更新物与安装器放在源码清单内绑定；当前目标文件必须与已核实旧摘要或目标摘要一致，漂移则拒绝覆盖。核心正式切换后，由用户在个人终端执行：

```sh
"$XIAOBA" review-ui-install --plan
"$XIAOBA" review-ui-install --apply
```

安装器只更新四个固定适配文件，失败用内存里的原字节恢复，不创建备份。安装后用户关闭并重开小八监看中心，再核对页面与原卡证据。没有完成核心切换及页面安装，不能声称流程已在生产启用。


## 两轮状态、恢复与材料补交（2026-10-28 候选）

以下新命令须包含本次改动的发布入口才生效；当前安装旧版本不因此升级。候选测试使用其 src 的 Python 模块入口并显式指定隔离配置，禁止绕过真实后台发布绑定。

- 首轮通过：pending_final_review，仍需用户亲自终审。
- 首轮失败：revalidation_required，允许第二轮。
- 次轮通过但缺材料：awaiting_diff_materials，保留执行与审查，不重跑、不加轮。
- 次轮失败：retired，禁止第三轮。
- 外部工具/网络/认证异常：paused，不消费终局轮次；需要显式恢复。

memory-trial-status 给出 attempt_id、task_id、round、plan_digest、execution_digest。正常入口：

    "$XIAOBA" memory-trial-pause CANDIDATE_ID --attempt-id ATTEMPT_ID --reason '中断原因'
    "$XIAOBA" memory-trial-resume CANDIDATE_ID
    "$XIAOBA" memory-trial-diff CANDIDATE_ID --materials /绝对路径/diff.json

pause 只暂停验证状态，不终止宿主进程；先按执行器回执核实进程已停止。恢复返回 validation_task_id 时必须沿用该任务，不重新执行。原任务被健康冻结时仍须原 task-resume 用户显式解冻及新鲜健康证据，不自动探针/解冻。无关联任务的中断尝试可重新认领，新尝试身份使旧控制器不能入队结算。

差异材料使用五字段；evidence_changes 逐项引用 status 两轮实际值：

    {
      "initial_failure": "具体首轮失败点及影响",
      "remediation": "具体整改及新增断言",
      "evidence_changes": {
        "round_one": {"attempt_id":"实际ID","round":1,"task_id":"实际任务","plan_digest":"实际摘要","execution_digest":"实际摘要"},
        "round_two": {"attempt_id":"实际ID","round":2,"task_id":"实际任务","plan_digest":"实际摘要","execution_digest":"实际摘要"}
      },
      "outcome_changes": "两轮结果实际变化",
      "remaining_limitations": "未覆盖环境和剩余风险"
    }

第二轮完成后再补材料，避免把第二轮计划自身摘要循环写入计划。ID/摘要/轮次确定性对账；文字意义仍需审查和用户判断。补材料重查第二轮当前证据和回执；最新 FAIL、待认证回执、无效包或人工拒绝均不能通过。相同材料重复提交不增加轮次。

review-ingress 提供现有 Origin/Token CSRF 保护下的 POST /api/open-review/validation/resume（candidate_id）和 /validation/diff（candidate_id、materials）。与 CLI 共用原生逻辑，不批准经验。外部任务卡自动绑定 attempt/round/task/plan/execution；回执补充审查，不能替代执行及原生结算。
