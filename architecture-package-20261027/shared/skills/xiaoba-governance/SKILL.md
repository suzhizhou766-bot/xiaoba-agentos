---
name: xiaoba-governance
description: 检查小八模块契约、守卫拦截策略、密钥箱或五工具开机自启项时使用；也用于判断某个动作会不会被小八守卫拦下。
---

# 小八治理与自检

## 守卫（硬拦截）

- 策略只有一份：`系统/治理/policy.json`（读禁区、写禁区、需确认的写入、禁用和需确认的命令）。改策略只改这一份；它在写禁区里，Agent 写不了，由 Agent 出草稿、用户本人替换。`工作区/小八融合迭代-20261022/` 下的 `第1步-拦截草稿/`、`第3步-守卫修订草稿/` 里的 policy.json 是历史草稿，不生效、不要用。
- 守卫脚本：`工具/治理/guard.sh` → `guard.py`（3.14.7，只读策略，不联网、不写日志、不常驻）。Claude Code、Codex、Qoder、Antigravity 四家的钩子都只写这一行命令，从不改；Claude Code、Qoder 的 matcher 是 `*`（2026-10-22 起，MCP 工具也经守卫）。DSH 没有钩子，只靠沙箱档位（见 AGENTS.md 第 6 节）。
- 分档（policy 第 4 版，2026-10-22 用户定）：直接拒绝（只拦写，读照常）：AGENTS.md 及其两个软链、`系统/治理/**`、`guard.py`、`guard.sh`、第二大脑发版生成区（根笔记、10、60、70 门禁与入口、历史版本记录）、git commit。需确认：mv、第二大脑 20–50、`skills/`、`配置/`、系统 python。哪些 Agent 能接改规则类的活，看 AGENTS.md 第 6 节的 Agent 能力表。
- 路径比对同时看符号链接的真实位置；参数里的通配符按实际文件展开；命令里 `cd`/`pushd` 过的目录也算相对路径的起点（2026-10-22 修）。仍做不到：变量拼接、别名、base64 解码后执行、写进脚本文件再执行。
- 自查某个动作：`工具/治理/guard.sh cmd "<命令>"`、`guard.sh read <路径>`、`guard.sh write <路径>`；退出码 0 放行、2 拒绝、3 需用户确认。
- 钩子体检：`工具/治理/钩子体检.sh`（只读；查四家钩子和 matcher、版本变化、守卫自测、全局 AGENTS.md 软链；退出码 0 正常、1 有异常）。有 Agent 升级时它会提示跑 V4 卡第 1、2 步。各 Agent 的配置文件在 deny_read 里，查钩子装没装只能跑这个脚本，不要 grep 配置文件。
- 测“需确认”会不会弹窗：编辑必须真能匹配，编辑前先完整读一遍目标文件；替换串对不上或读态过期时工具会先报错，钩子根本没跑。只有带“小八守卫：”前缀的报错才算守卫的结果。
- 被拦下时：照提示换做法（删除改成移到 `_待删/`，读凭据改用密钥箱），不要绕过，也不要改策略来放行自己。

## 密钥箱

- 需要用户提供 API Key 时一律走 `工具/密钥/xb-secret`，用法见 `工具/密钥/README.md`。
- 流程：`xb-secret has <名字>` → 没有就请用户运行 `xb-secret set`，**绝不让用户把 Key 贴进对话** → 调 API 的脚本请用户 `xb-secret allow` → `xb-secret run <名字> -- <脚本>`。

## 模块契约检查

```bash
XIAOBA_ASSISTANT_ROOT="$HOME/Documents/私人助理" \
KNOWLP_ROOT="$HOME/Documents/Codex/KnowLP" \
"$HOME/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14" \
  "$HOME/Documents/私人助理/工具/治理/xiaoba_contract_check.py" --json
```

`implemented` 只表示本地入口存在；外部上游、模型、运行时服务没起来时报 warning，不得说成端到端通过。

## 五工具开机自启项

`工具/治理/xiaoba_media_services.sh install|remove|status`，只管 `com.xiaoba.media.*`。`install`、`remove` 会改系统的 LaunchAgent，**只有用户明确要求才执行**；`status` 随时可查。

## 其它脚本

- `xiaoba_behavior.py`：与 Agent 无关的行为与策略记录库（独立于第二大脑，单次成功不算规则）。
- `prompt_reverse_regression.py`：反推服务（8890）的技术回归检查，会向本机服务发请求。
