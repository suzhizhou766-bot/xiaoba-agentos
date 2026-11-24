"""W4-P1 / W4-O3: Argument parser specification and 4-group --help layout."""
from __future__ import annotations

import argparse
from pathlib import Path

from ..routing import AGENTOS_MULTI, ASK_USER, DIRECT, REVIEW

PROJECT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_DIR / "config" / "agentos.json"


# Strategy evolution was retired by the user on 2026-10-11. The seven commands
# it owned are gone; they stay in the parser only so an existing script gets a
# plain "retired" answer instead of argparse's generic "invalid choice".
RETIRED_COMMANDS = {
    "evolve": "已退役：策略进化（2026-10-11 用户裁决）",
    "eval": "已退役：策略进化（2026-10-11 用户裁决）",
    "replay": "已退役：策略进化（2026-10-11 用户裁决）",
    "regression": "已退役：策略进化（2026-10-11 用户裁决）",
    "judge-baseline": "已退役：策略进化（2026-10-11 用户裁决）",
    "promote": "已退役：策略进化（2026-10-11 用户裁决）",
    "rollback": "已退役：策略进化（2026-10-11 用户裁决）",
}

_RETIRED_COMMAND_NOTICE = (
    "命令 {name} 已退役：策略进化于 2026-10-11 由用户裁决退役，{name} 不再存在，"
    "本次没有执行任何操作，也没有改动数据库。原 24 条评估用例已改为离线测试，"
    "每次全量测试都会跑（tests/test_offline_judge_cases.py）。"
)


def retired_command_message(name: str) -> str:
    return _RETIRED_COMMAND_NOTICE.format(name=name)


# W4-O3: Four-group --help presentation without renaming any subcommand.
CLI_COMMAND_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "日常 8 命令（最高频入口）",
        (
            ("doctor", "系统与节点只读健康检查"),
            ("status", "查看最近任务状态与队列概览"),
            ("show", "查看单条任务详情与执行轨迹"),
            ("submit", "提交后台工作/审查任务"),
            ("intake", "统一消息接入口（路由判断与用户交付声明）"),
            ("session-start", "上班初始化（纯本地检查，不发外部探针）"),
            ("session-close", "下班收口并汇总待审材料"),
            ("review-list", "查看待初审/待终审经验列表（--stage initial|delivery|final|all）"),
        ),
    ),
    (
        "经验裁决与记忆管理",
        (
            ("review", "用户终/初审裁决（--stage initial|delivery|final；initial 与 delivery 等价）"),
            ("review-packet", "按 JSON 裁决包执行用户裁决"),
            ("publish-memory", "发布已终审通过的经验到第二大脑"),
            ("memory-candidate-create", "登记宿主撰写的经验草稿进入价值评分队列"),
            ("memory-value-list", "列出待独立价值评分的经验准备项"),
            ("memory-value-dispatch", "受控派发待价值评分队列（单条或批量）"),
            ("memory-pending-index", "只读查看独立待验证经验索引"),
            ("memory-pending-read", "回读绑定初审及正文版本的待验证资料"),
            ("memory-trial-run", "在隔离沙箱执行候选验证并提交独立双审"),
            ("memory-trial-status", "查看候选隔离验证状态"),
            ("memory-trial-match", "只建议同类验证试验，不执行候选命令"),
            ("memory-trial-resume", "恢复暂停中的隔离验证"),
            ("memory-trial-pause", "显式暂停指定在途隔离验证"),
            ("memory-trial-diff", "补交两轮真实证据的差异材料"),
            ("memory-validation-basis-propose", "为真实任务提议版本化核验依据包"),
            ("memory-validation-basis-revoke", "用户撤回明确版本的核验依据"),
            ("memory-application-record", "登记经验在真实任务中的实际应用记录"),
            ("memory-delivery-record", "登记用户成品交付事件（需 --user-decision）"),
            ("memory-revision-submit", "提交正式经验修订稿"),
            ("memory-revision-resubmit", "重新提交退回后的正式经验修订稿"),
            ("memory-formal-call", "登记正式经验调用事件"),
            ("memory-retrieve", "只读检索已发布正式经验"),
            ("memory-receipt", "查看经验候选审核与发布回执"),
            ("memory-retire", "用户退役已发布经验并归档"),
            ("memory-rename-notes", "将纯编号经验笔记重命名为可读标题"),
            ("memory-retained-list", "列出待用户处理的留存原件"),
            ("memory-retained-resolve", "按用户指令处理单条留存原件"),
            ("constraint-hold-release", "解除约束拦截挂起状态"),
        ),
    ),
    (
        "内部维护、协作与发版",
        (
            ("init", "初始化数据库与运行目录"),
            ("run", "启动后台 Worker 编排循环（--once 单轮）"),
            ("bridge", "启动本机 Unix Socket 桥接服务"),
            ("capability-verify", "核验本机能力清单"),
            ("handoff-status", "检查交接单状态"),
            ("receipt-log", "查看任务回执日志"),
            ("repair-status", "查看修复/自愈历史状态"),
            ("workday-read", "读取共用工作日台账位置"),
            ("workday-record", "登记原产物路径与哈希入台账队列"),
            ("workday-rebuild", "从 SQLite 重建共用工作日台账"),
            ("task-resume", "按冻结哈希恢复被冻结任务"),
            ("task-abandon", "放弃冻结任务并保留历史记录"),
            ("task-cancel", "取消尚未开始的排队任务"),
            ("task-work-record", "追加任务实际参与者记录"),
            ("task-host-bind", "为等宿主任务补绑宿主身份"),
            ("task-host-complete", "提交宿主回执结算任务"),
            ("task-delivery", "登记用户成品交付（与经验初审 initial/delivery 独立）"),
            ("task-delivery-view", "只读查看任务成品交付版本与待处理列表"),
            ("task-result-register", "登记直出任务成品与结果文件"),
            ("collaborate", "提交多节点协作任务"),
            ("collaborate-review", "提交协作产物的独立双审任务"),
            ("review-ui-install", "按源清单校验并同步监看中心 UI 文件"),
            ("install-plan", "只读预览服务安装方案"),
            ("install-runner", "安装/核验小八 AgentOS Runner"),
            ("install-service", "安装并校验后台 LaunchAgent 服务"),
            ("agent-enable", "启用指定旧配置节点"),
            ("agent-disable", "停用指定旧配置节点"),
            ("skill-verify", "校验技能索引与目录完整性"),
            ("skill-list", "列出已登记技能"),
            ("skill-match", "按提示词匹配适用技能"),
            ("skill-review-prompt", "生成技能草稿的双审提示词"),
            ("skill-publish", "核对双审通过后发布技能草稿"),
            ("knowledge-verify", "校验价值文件目录与索引"),
            ("knowledge-list", "列出价值文件目录"),
            ("knowledge-retrieve", "检索价值文件"),
            ("knowledge-index-rebuild", "重建价值文件索引并同步第二大脑笔记"),
            ("content-notes-sync", "同步技能卡与价值文件索引笔记到第二大脑"),
            ("demo", "在隔离配置下演示核心主流程"),
        ),
    ),
    (
        "已退役占位（仅返回停用说明，不执行任何操作）",
        tuple((name, RETIRED_COMMANDS[name]) for name in sorted(RETIRED_COMMANDS)),
    ),
)


def format_grouped_commands_epilog() -> str:
    """Format the 4-group command directory shown in `agentos --help`."""
    lines = ["子命令分组导航（执行 `agentos <命令> --help` 查看详细参数）："]
    for group_title, entries in CLI_COMMAND_GROUPS:
        lines.append("")
        lines.append(f"[{group_title}]")
        for cmd_name, desc in entries:
            lines.append(f"  {cmd_name:<32} {desc}")
    lines.append("")
    lines.append("术语提示：经验初审推荐 `--stage initial`（兼容旧名 `delivery`），与用户成品交付 `task-delivery` 互相独立。")
    return "\n".join(lines)


class _GroupedHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Hide the ungrouped 81-command dump so the 4-group epilog stays scannable."""

    def _format_action(self, action: argparse.Action) -> str:
        if isinstance(action, argparse._SubParsersAction):
            return ""
        return super()._format_action(action)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentos",
        description="小八 AgentOS 控制平面",
        epilog=format_grouped_commands_epilog(),
        formatter_class=_GroupedHelpFormatter,
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    sub.add_parser("init")
    sub.add_parser("capability-verify")
    handoff_parser = sub.add_parser("handoff-status")
    handoff_parser.add_argument("path", nargs="?", default=str(PROJECT_DIR / "HANDOFF.json"))
    receipt_parser = sub.add_parser("receipt-log")
    receipt_parser.add_argument("task_id")
    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.add_argument("--entity-root", type=Path, help="仅候选只读验收：显式共享实体根（默认核验正式部署位置；执行入口不接受该参数）")
    session_start = sub.add_parser("session-start")
    session_start.add_argument("--timezone", default="Asia/Shanghai")
    ledger_rebuild = sub.add_parser('workday-rebuild', help='从 SQLite 重建共用工作日台账')
    ledger_rebuild.add_argument('--day')
    ledger_read = sub.add_parser('workday-read', help='读取台账位置；发现待刷新标记时先重建')
    ledger_read.add_argument('--day')
    ledger_record = sub.add_parser('workday-record', help='登记原产物路径与哈希（只排队并标记待刷新，下次上班、手动重建或空闲时生成台账）')
    ledger_record.add_argument('path', type=Path)
    ledger_record.add_argument('--task-id')
    for field in ('node','agent','model','kind','status'):
        ledger_record.add_argument('--'+field, required=True)
    task_resume = sub.add_parser("task-resume")
    task_resume.add_argument("task_id")
    task_resume.add_argument("--freeze-hash", required=True)
    task_abandon = sub.add_parser("task-abandon", help="放弃一个冻结任务：结束任务，记录保留，不再运行")
    task_abandon.add_argument("task_id")
    task_abandon.add_argument("--freeze-hash", required=True)
    task_cancel = sub.add_parser("task-cancel", help="取消一个还在排队、没有开始的任务")
    task_cancel.add_argument("task_id")
    repair_status = sub.add_parser("repair-status")
    repair_status.add_argument("--limit", type=int, default=50)
    session_close = sub.add_parser("session-close")
    session_close.add_argument("--session-id")
    intake = sub.add_parser("intake")
    intake.add_argument("prompt")
    intake.add_argument("--message-id")
    intake.add_argument("--problem-key", default="")
    intake.add_argument("--delivery-receipt", help="当前真实用户交付消息绑定的任务、成品及交付记录；不批准经验")
    intake.add_argument("--user-decision", action="store_true", help="声明本次由用户明确选择交付；不是自动验收或身份认证")
    intake.add_argument("--active-task-id")
    intake.add_argument("--relation", choices=("auto", "related", "unrelated"), default="auto")
    intake.add_argument("--risk", choices=("auto", "low", "high"), default="auto")
    intake.add_argument("--requires-action", action="store_true")
    intake.add_argument("--memory-worthy", action="store_true")
    intake.add_argument("--explicit-agentos", action="store_true")
    intake.add_argument("--force-route", choices=(DIRECT, AGENTOS_MULTI, REVIEW, ASK_USER))
    intake.add_argument("--submit", action="store_true")
    intake.add_argument(
        "--queue-when-blocked", action="store_true",
        help="后台当前无法执行时仍然入队等待（默认拒绝入队并说明原因）",
    )
    intake.add_argument("--priority", type=int, default=0)
    submit = sub.add_parser("submit")
    submit.add_argument("prompt")
    submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--memory-kind", choices=("audit", "rule", "method"))
    submit.add_argument("--task-purpose", choices=("release_smoke",),
                        help="受控发布冒烟入口专用；用途冻结进任务快照且只能 audit 结算")
    submit.add_argument("--problem-key", default="")
    submit.add_argument('--work-task', help='待审查产物的来源工作任务；从记录和agent_runs推导参与者')
    submit.add_argument('--no-work-task', action='store_true',
                        help='明确声明没有来源工作任务；发布验收取证要求二者声明其一')
    submit.add_argument('--release-acceptance', action='store_true', help='发版工具显式声明验收任务，冻结进执行快照')
    submit.add_argument('--host-id')
    submit.add_argument('--host-entry')
    submit.add_argument('--host-model')
    submit.add_argument('--no-review', action='store_true', help='工作任务；等宿主提交实际结果')
    participant = sub.add_parser('task-work-record', help='追加实际参与记录，只增不减，不声明未干活')
    participant.add_argument('task_id')
    participant.add_argument('--actor-id', required=True)
    participant.add_argument('--role', choices=('author','executor','collaborator'), required=True)
    host_complete = sub.add_parser('task-host-complete', help='提交绑定任务的宿主回执；不批准经验')
    host_complete.add_argument('task_id')
    host_complete.add_argument('--receipt', required=True, help='JSON回执文件')
    host_complete.add_argument('--user-chose-host', action='store_true', help='用户明确选了全部审查员不可用时的宿主兜底')
    host_bind = sub.add_parser('task-host-bind', help='为旧的等宿主任务追加宿主身份，不改冻结题面')
    host_bind.add_argument('task_id')
    host_bind.add_argument('--host-id', required=True)
    host_bind.add_argument('--host-entry', required=True)
    host_bind.add_argument('--host-model', required=True)
    trials = sub.add_parser("memory-trial-run", help="在已授权任务中执行本机隔离测试并提交独立双审；不自动批准")
    trials.add_argument("task_id")
    trials.add_argument("--card", required=True)
    ui_install = sub.add_parser("review-ui-install", help="用户终端：先只读核对，再安装源清单绑定的裁决页面适配")
    ui_mode = ui_install.add_mutually_exclusive_group(required=True)
    ui_mode.add_argument("--plan", action="store_true")
    ui_mode.add_argument("--apply", action="store_true")
    resume_trial = sub.add_parser("memory-trial-resume", help="显式恢复暂停验证，不重新批准经验")
    resume_trial.add_argument("task_id")
    pause_trial = sub.add_parser("memory-trial-pause", help="显式暂停指定在途尝试；不会结束宿主进程")
    pause_trial.add_argument("task_id")
    pause_trial.add_argument("--attempt-id", required=True)
    pause_trial.add_argument("--reason", required=True)
    diff_trial = sub.add_parser("memory-trial-diff", help="补交绑定两轮真实证据的差异材料，不重跑试验")
    diff_trial.add_argument("task_id")
    diff_trial.add_argument("--materials", required=True)
    trial_status = sub.add_parser("memory-trial-status")
    trial_status.add_argument("task_id")
    trial_match = sub.add_parser("memory-trial-match", help="只建议同类试验，不执行候选命令")
    match_selector = trial_match.add_mutually_exclusive_group(required=True)
    match_selector.add_argument("--problem-key")
    match_selector.add_argument("--query", help="只按任务标题/原问题关键词建议，不读取候选正文")
    pending = sub.add_parser("memory-pending-index", help="独立待验证索引；不启动后台、不注入长期记忆")
    pending.add_argument("--problem-key")
    pending.add_argument("--query")
    pending_read = sub.add_parser("memory-pending-read", help="回读绑定初审及正文版本的待验证资料")
    pending_read.add_argument("task_id")
    pending_read.add_argument("--candidate-sha256", required=True)
    pending_read.add_argument("--delivery-review-id", required=True)
    delivery_view = sub.add_parser("task-delivery-view", help="只读任务交付版本与待处理列表")
    delivery_view.add_argument("--task-id")
    delivery_view.add_argument("--version", dest="artifact_version", help="可选：指定查询的成品版本号（如 v1）")
    historical_basis = sub.add_parser("memory-validation-basis-propose", help="为既有真实任务提议版本化证据包；不会确认或批准")
    historical_basis.add_argument("task_id")
    historical_basis.add_argument("--receipt", required=True, help="含完整 history_pack、method_claims 和 application_scope 的 JSON 文件")
    revoke_basis = sub.add_parser("memory-validation-basis-revoke", help="用户本人撤回明确版本的核验依据；不得由Agent代作决定")
    revoke_basis.add_argument("task_id")
    revoke_basis.add_argument("--receipt", required=True, help="绑定basis版本、用户决定来源、原因和幂等键的JSON")
    for name in ("memory-application-record", "memory-delivery-record", "task-result-register", "task-delivery", "memory-revision-submit", "memory-revision-resubmit", "memory-formal-call"):
        command = sub.add_parser(name, help="登记实际应用或真实用户交付事件，不批准经验")
        command.add_argument("--receipt", required=True)
        if name in {"memory-delivery-record", "task-delivery"}:
            command.add_argument("--user-decision", action="store_true", help="用户本次显式选择声明；不会代替经验批准")
    for name in ("collaborate", "collaborate-review"):
        command = sub.add_parser(name)
        command.add_argument("prompt")
        command.add_argument("--context-file", required=True)
        command.add_argument("--message-id", required=True)
        if name == "collaborate":
            command.add_argument("--assignments-file", required=True)
        else:
            command.add_argument("--work-task-id", required=True)
            command.add_argument("--artifact", action="append", required=True)
    for command in (submit, intake):
        command.add_argument("--requires-capability", action="append", default=[])
        command.add_argument("--needs-web", action="store_true")
        command.add_argument("--context-file", help="UTF-8 task summary and sourced evidence, max 64 KiB")
    submit.add_argument("--material", action="append", default=[], metavar="PATH",
        help="任务材料文件或目录；冻结 SHA-256 后派发进节点工作区（可重复给出）")
    run = sub.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--max-idle-polls", type=int, default=0)
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    status.add_argument("--limit", type=int, default=20)
    show = sub.add_parser("show")
    show.add_argument("task_id")
    review_list = sub.add_parser("review-list")
    review_list.add_argument(
        "--stage",
        choices=("initial", "delivery", "final", "all"),
        default="all",
        help="经验裁决阶段筛选：initial（初审，推荐）/ delivery（初审旧名，兼容）/ final（终审）/ all（全部）",
    )
    review_list.add_argument("--date")
    sub.add_parser("memory-value-list", help="列出尚待独立价值评分的经验准备项；不读取正文")
    value_dispatch = sub.add_parser(
        "memory-value-dispatch",
        help="受控派发待价值评分队列（单条或批量；同一候选正文哈希自动派发至多一次，失败转人工待办）",
    )
    value_dispatch.add_argument("--task-id", help="指定待评分候选 task_id；省略时按队列顺序处理")
    value_dispatch.add_argument("--all", dest="dispatch_all", action="store_true", help="处理队列中所有尚未派发的待评项")
    value_dispatch.add_argument("--json", action="store_true")
    review = sub.add_parser("review")
    review.add_argument("task_id")
    review.add_argument(
        "--stage",
        choices=("initial", "delivery", "final"),
        required=True,
        help="经验裁决阶段：initial（初审，推荐）/ delivery（初审旧名，兼容保留）/ final（终审）；与用户成品交付 task-delivery 无关",
    )
    review.add_argument("--decision", choices=("approve", "reject", "revise", "retire"), required=True)
    review.add_argument("--note", default="")
    hold_release = sub.add_parser("constraint-hold-release")
    hold_release.add_argument("task_id")
    memory_retire = sub.add_parser("memory-retire")
    memory_retire.add_argument("task_id")
    memory_retire.add_argument("--reason", required=True)
    memory_rename = sub.add_parser(
        "memory-rename-notes",
        help="把按整串编号命名的已发布经验笔记改成可读名字（标题 + 编号前 8 位）",
    )
    memory_rename.add_argument("--dry-run", action="store_true", help="只列出会怎么改，不动任何文件")
    memory_receipt = sub.add_parser("memory-receipt")
    memory_receipt.add_argument("task_id")
    for name in sorted(RETIRED_COMMANDS):
        # Registered only so an old script or habit gets a plain answer instead
        # of argparse's "invalid choice". Every argument is swallowed and the
        # command can never do anything.
        retired = sub.add_parser(name, add_help=False, help=RETIRED_COMMANDS[name])
        retired.add_argument("rest", nargs=argparse.REMAINDER)
    bridge = sub.add_parser("bridge")
    bridge.add_argument("--socket", default=None)
    authored = sub.add_parser(
        "memory-candidate-create",
        help="登记宿主撰写的经验草稿；method/rule 先进入独立价值评分准备队列",
    )
    authored.add_argument("--title", required=True)
    authored.add_argument("--body-file", required=True)
    authored.add_argument("--author", default="host_assistant")
    authored.add_argument("--source-task-id", action="append", default=[])
    authored.add_argument("--idempotency-key", default=None)
    authored.add_argument("--kind", choices=("rule", "method"), default="method")
    authored.add_argument("--problem-key", default="")
    review_packet = sub.add_parser("review-packet")
    review_packet.add_argument("path", help="JSON 裁决包路径，使用 - 从标准输入读取")
    publish_memory = sub.add_parser("publish-memory")
    publish_memory.add_argument("--task-id")
    sub.add_parser("install-plan")
    sub.add_parser("install-runner")
    install_service = sub.add_parser("install-service")
    install_service.add_argument(
        "--backup-database", action="store_true",
        help="仅当数据库需要迁移时使用：用户明确授权在迁移前备份数据库",
    )
    enable = sub.add_parser("agent-enable")
    enable.add_argument("names", nargs="+")
    disable = sub.add_parser("agent-disable")
    disable.add_argument("names", nargs="+")
    skill_verify = sub.add_parser("skill-verify")
    skill_verify.add_argument("--entity-root", type=Path, help="仅候选只读验收：显式共享实体根（默认核验正式部署位置；执行入口不接受该参数）")
    skill_list = sub.add_parser("skill-list")
    skill_list.add_argument("--entity-root", type=Path, help="仅候选只读验收：显式共享实体根（默认核验正式部署位置；执行入口不接受该参数）")
    skill_match = sub.add_parser("skill-match")
    skill_match.add_argument("prompt")
    skill_match.add_argument("--entity-root", type=Path, help="仅候选只读验收：显式共享实体根（默认核验正式部署位置；执行入口不接受该参数）")
    sub.add_parser("knowledge-verify")
    content_sync = sub.add_parser(
        "content-notes-sync",
        help="按技能索引和价值文件索引更新第二大脑里的技能卡与价值文件索引笔记；--verify 只检查不写",
    )
    content_sync.add_argument("--verify", action="store_true")
    for name, text in (("skill-review-prompt", "为草稿目录里的一个技能生成审查说明，供提交给两个后台"),
                       ("skill-publish", "技能改动的发布动作：核对审查任务后把草稿装到正式位置、写索引并同步技能卡")):
        skill_command = sub.add_parser(name, help=text)
        skill_command.add_argument("skill_id")
        skill_command.add_argument("--draft", type=Path, required=True, help="放着这个技能全部文件的草稿目录（不是正式技能目录）")
        skill_command.add_argument("--entry", type=Path, help="技能条目 JSON（触发词、说明等）；只在新增技能或改条目时需要")
        if name == "skill-publish":
            skill_command.add_argument("--review-task", required=True, help="两个后台都通过的审查任务编号")
    sub.add_parser("knowledge-index-rebuild", help="价值文件改动的发布动作：重建索引、自检并同步笔记")
    sub.add_parser("knowledge-list")
    knowledge_retrieve = sub.add_parser("knowledge-retrieve")
    knowledge_retrieve.add_argument("query")
    memory_retrieve = sub.add_parser("memory-retrieve")
    memory_retrieve.add_argument("query")
    retained_list = sub.add_parser(
        "memory-retained-list",
        help="list every retained original object the self-check can still find",
    )
    retained_list.add_argument(
        "--write-report",
        action="store_true",
        help="also write the self-check report next to the retention journal",
    )
    retained_list.add_argument(
        "--show-resolved",
        action="store_true",
        help="also show the objects the user already handled (history only)",
    )
    retained_resolve = sub.add_parser(
        "memory-retained-resolve",
        help="handle exactly one retained object named on the command line (user-run)",
    )
    retained_resolve.add_argument(
        "retained_path",
        help="the exact retained path from memory-retained-list, one object only",
    )
    retained_action = retained_resolve.add_mutually_exclusive_group(required=True)
    retained_action.add_argument(
        "--trash",
        action="store_true",
        help="move that one object into the system trash, keeping the object itself",
    )
    retained_action.add_argument(
        "--confirm-deleted",
        dest="confirm_deleted",
        action="store_true",
        help="record that you deleted that object yourself; refuses if it is still there",
    )
    retained_resolve.add_argument(
        "--expect-dev",
        dest="expect_dev",
        type=int,
        default=None,
        help=(
            "the device number memory-retained-list reports for this path; only needed when the "
            "discovery record carries no identity, and it can never override the record"
        ),
    )
    retained_resolve.add_argument(
        "--expect-ino",
        dest="expect_ino",
        type=int,
        default=None,
        help=(
            "the inode number memory-retained-list reports for this path; the object at the path "
            "must carry it or the command refuses to move anything"
        ),
    )
    demo = sub.add_parser("demo")
    # 策略进化的那半圈循环已随功能退役，默认题面只说剩下的一半。
    demo.add_argument("--prompt", default="验证小八 AgentOS 的生产循环")
    # W2-H4: 常用只读查询子命令统一兼容 --json 参数，避免传 --json 时报 unrecognized arguments。
    for cmd_name in (
        "capability-verify", "handoff-status", "receipt-log",
        "show", "review-list", "review-packet", "repair-status",
        "memory-value-list", "memory-retrieve", "memory-retained-list",
        "memory-trial-status", "memory-trial-match", "memory-pending-index",
        "memory-pending-read", "task-delivery-view",
        "skill-verify", "skill-list", "skill-match",
        "knowledge-verify", "knowledge-list", "knowledge-retrieve",
        "workday-read",
    ):
        sub_parser = sub.choices.get(cmd_name)
        if sub_parser is not None and "--json" not in sub_parser._option_string_actions:
            sub_parser.add_argument("--json", action="store_true", help="以 JSON 格式输出（默认即 JSON，兼容显式传参）")
    return parser
