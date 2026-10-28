"""User-authored execution rules included in every backend prompt."""

USER_RULES_TEXT = (
    "- 没有用户直接、明确的备份要求，不得自行新建备份文件、备份目录或备份快照。\n"
    "- 可以提醒用户是否需要备份；提醒、一般操作授权或发布授权都不等于备份授权。\n"
    "- 本用户规则优先于工作流、技能和发布脚本的默认备份建议；未经明确授权，不得执行内置备份步骤。\n"
    "- 本规则不授权自动删除已有备份。"
)
