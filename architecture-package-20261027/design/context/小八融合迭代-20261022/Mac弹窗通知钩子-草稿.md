# Mac 弹窗通知钩子（草稿，2026-10-24，Claude Code）

用户要的：Claude Code 等权限确认或等输入时，在 Mac 上弹系统通知。

> **状态：已移除（2026-10-24）。** 用户发现 Claude 桌面 App 自带通知（权限审批、提问、任务完成三类横幅，均默认开启，带系统提示音，App 在后台时弹出），决定删掉本钩子。用户本人在终端运行删除脚本：只删标题含“小八/Claude Code 需要你操作”的那条，`Notification` 键随之去掉，其它事件不动，保留文件原有 600 权限（脚本在临时文件上测过；用户机器上的运行输出 Agent 未看到，以用户“跑完了”为准）。注意：App 自带通知只覆盖桌面 App 里的会话，终端 `claude` 命令行不会弹；以后要在命令行用，可按下文重新加。
>
> 以下为移除前的记录。
>
> **原状态：已完成（2026-10-24）。** 用户本人在终端运行合并脚本，`Notification` 钩子已加入，原有 6 个事件保留；解释器用统一的 3.14.7（不再用系统 python3）。合并时临时文件替换使文件权限变成 644，用户已 `chmod 600` 收回。实测：直接运行命令能弹通知、有 Glass 提示音；Claude Code 等输入时自动弹窗，用户确认可用。

## 为什么是草稿

小八守卫（`工具/治理/guard.sh`，策略 `系统/治理/policy.json`）把 `~/.claude/settings.json` 列为凭据路径，Agent 不能读写。按规则被拦下不绕过，所以由用户本人加。

## 用户要加的内容

打开 `~/.claude/settings.json`，在顶层的 `"hooks"` 对象里加一个 `"Notification"` 键（已有 `"hooks"` 就合并进去，不要覆盖别的事件；没有就新建 `"hooks": { … }`）：

```json
"Notification": [
  {
    "hooks": [
      {
        "type": "command",
        "command": "/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14 -c 'import json,sys,subprocess; m=(json.load(sys.stdin).get(\"message\") or \"需要你操作\"); subprocess.run([\"/usr/bin/osascript\",\"-e\",\"on run a\",\"-e\",\"display notification (item 1 of a) with title \\\"小八/Claude Code 需要你操作\\\" sound name \\\"Glass\\\"\",\"-e\",\"end run\",m])'"
      }
    ]
  }
]
```

说明：
- 钩子从标准输入读 Claude Code 给的 JSON，取 `message` 字段当正文；正文作为参数传给 osascript，不拼进脚本，引号和特殊字符不会出错。
- 标题“小八/Claude Code 需要你操作”，提示音 Glass。
- 2026-10-24 已实测这条 osascript 命令能弹出通知（返回码 0）。第一次弹窗时 macOS 可能要你在“系统设置 → 通知”里允许“脚本编辑器”（或终端）发通知。
- 改完重开 Claude Code 会话生效；桌面 App 里也可以在设置的 Hooks 页面加。
