---
name: xiaoba-agentos
description: 小八 AgentOS v1.6.1 的 AGY+DSH 工作入口。用于小八上下班、双后台分工与独立审核、任务状态与恢复、记忆审核、索引和发布检查；普通问答不强制调用后台。
---

# 小八 AgentOS v1.6.1

唯一现行后台：`agy_review` + `dsh_review`，两份不同 Agent 的非空合格结果才能进入本地确定性裁判。两者都只走各自的本地 CLI 路由，模型由对应桌面端自行选择；宿主自身不算这两个后台之一。

## 唯一入口与事实源

```sh
XIAOBA="$HOME/.agents/skills/xiaoba-agentos/scripts/xiaoba"
"$XIAOBA" doctor --json
"$XIAOBA" status --json --limit 10
```

入口先验证活动配置、不可变发布及源码哈希，再调用同一发布的 CLI；失败停止，不用旧入口绕过。发布索引是 `/Users/suzhizhou/Documents/小八AgentOS/.runtime/releases/index.json`。当前 Vault 物理目录是 `/Users/suzhizhou/Documents/私人助理/第二大脑`，目录名不含版本号，不随版本变化，其笔记属于 v1.4 树格式族，正文声明的当前软件版本随每次发布更新，以 `60_当前上下文/系统当前态.md` 为准。

从 Vault 的 `00_小八AgentOS.md` 当前根进入。KnowLP 只定位，命中后回读原文；归档不参与活动规则检索。具体工作流程读取活动发布 `agentos/skills/collaboration-core/SKILL.md`（路径由 `verify-release` 返回的 release_dir 解析），不要猜旧版本目录。软件版本、知识协议版本、索引格式版本与历史版本的分层及不可改名的兼容标识见活动发布 `agentos/docs/VERSIONING.md`。

## 三条执行底线

- B09 配置一致：会话、已安装入口、后台进程和裁决中心必须使用内容一致的配置；发现分歧就拒绝执行。配置结构与读取它的代码同批修改。
- B19 非答案不算成功：空输出、只有工具调用标记或泄露思考过程的输出不得交付、复用或进入记忆；检测不能误伤正常正文。
- B20 恰好结算一次：任务完成与经验候选登记必须原子成功或一起失败，重复调用不能重复结算；状态变化进入可校验事件链，链坏则停派任务，异常须释放租约。重试只补失败侧且不超预算。

## 上下班和消息路由

- “上班了小八助理”：`session-start`。只有本地 doctor、两个后台真实探针均通过，才报告可接单；任何未验证项都必须明示。本地检查不等于实时模型可用。
- 工作会话的新消息：调用 `intake "消息" --message-id 唯一消息ID`；有动作需求加 `--requires-action`，用户明确要求小八加 `--explicit-agentos`；需要任务时同一次调用加 `--submit`，不重复提交。返回 `blocked_local_health` 表示后台当前无法执行、任务未入队：按 `blockers` 向用户说明原因并先恢复（通常是重新“上班”或打开 Antigravity），不要自行加 `--queue-when-blocked`，除非用户明确要求先排队。
- `direct`：会话模型直接回答；`agentos_multi`：跟踪同一个任务等待两个后台合格结果；`review`：进入用户审核；`ask_user`：只问必要边界。不能把后台文字当已执行补丁。
- 可拆分实现走 `collaborate`，为 AGY/DSH 分别分工；宿主集成后，冻结新证据走 `collaborate-review`。两阶段都必须两者成功，审核不得复用工作阶段输出。参数见当前 `docs/COLLABORATION.md`。
- “下班”：`session-close`，展示待终审清单；下班不代表批量批准。

## 审核、恢复与边界

`review-list --stage all` 是动态审核入口；用户在浏览器里裁决的入口是小八监看中心的「裁决中心」标签 `http://127.0.0.1:8799/#review`（未开启时双击 `/Users/suzhizhou/Documents/私人助理/工具/小八监看/启动监看中心.command`），原 `127.0.0.1:8797` 独立页面已停用。监看中心没开时不能在浏览器里裁决，待审队列也不会同步到第二大脑，重新开启后补同步；CLI 的 `review-list`/`review` 不受影响。`review TASK_ID --stage delivery|final --decision approve|reject|revise|retire` 仅用于用户针对该候选明确作出的决定。两次批准绑定相同候选哈希；重建索引或正式发布软件版本都不批准待审经验。终审发布还必须完成敏感检查及 KnowLP 复验。

失败不静默换账号或启用其他后台。外部认证/配额/503/超时只报告并有界退避，不反复热改代码。同伴自动诊断与修复、Headroom 自动重启已退役：健康重探只更新健康结果，不自动改变冻结状态。冻结任务只在健康恢复后由用户显式 `task-resume TASK_ID --freeze-hash HASH` 解冻，遗留的 `resume_ready` 记录同样需要用户显式恢复；恢复仍只补跑失败侧。单轮输出预算耗尽按任务级失败处理，不自动原样重发，手动恢复时提示拆分任务。

不得修改桌面登录材料或桌面自选的模型来修复后台任务；不保存密钥、OAuth、原始聊天或未审核模型输出到第二大脑。这两条是 `/Users/suzhizhou/Documents/私人助理/AGENTS.md` 第 4、5、6 节的摘要，完整规则以那里为准。发布更新先候选验收，再经用户授权提升。

## 用户公共规则：备份授权

- 没有用户直接、明确的备份要求，不得自行新建备份文件、备份目录或备份快照。
- 可以提醒用户是否需要备份；提醒、一般操作授权或发布授权都不等于备份授权。
- 本用户规则优先于工作流、技能和发布脚本的默认备份建议；未经明确授权，不得执行内置备份步骤。
- 本规则不授权自动删除已有备份。

## 失败恢复保留物（v1.6.1）

失败恢复只保留已认领原对象，不复制为备份；不自动清理、过期删除或进入检索。doctor与session-start显示待人工核对提醒，普通待核对不阻断接单、版本切换或新经验发布；真实发布门禁及索引错误仍按原规则拒绝。

用 xiaoba memory-retained-list 列出位置、身份与异常，重启后仍可查。用户亲自核对后逐个运行 xiaoba memory-retained-resolve ABSOLUTE_PATH --trash 移入废纸篓，或确已手动删除后运行 --confirm-deleted。两选一，无批量/自动清理。无身份的旧记录须先从清单核对并给出 --expect-dev DEV --expect-ino INO，不凭文件名盲目处理。失败按返回的实际位置、原因与重试条件处理；误捕获对象按清单人工核对。详见 docs/MEMORY_RECOVERY.md。
