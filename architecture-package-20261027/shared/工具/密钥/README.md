# 小八密钥箱

只要 Agent 干活需要你提供 API Key，就统一走这里：你在弹窗里填，Agent 只能调用，看不到值。

- **存在哪：** `~/.config/xiaoba/secrets/`，在私人助理之外。目录权限 700，文件权限 600。
- **不做的事：** 没有“打印密钥”的命令，不备份，不上传，不进 git。
- **入口：** `/Users/suzhizhou/Documents/私人助理/工具/密钥/xb-secret`，使用 配置/ 下的 Python 3.14.7。

## 你用的命令

| 命令 | 作用 |
|---|---|
| `xb-secret set <名字> <变量名> [用途]` | 弹出输入框，你填 Key。例如 `xb-secret set elevenlabs ELEVENLABS_API_KEY BGM生成` |
| `xb-secret import <名字> <现有.env文件> [用途]` | 把已有的 .env 收进密钥箱，会弹窗请你确认。原文件不会被动，请你自己把它移到 `_待删/` |
| `xb-secret allow <名字> <脚本>` | 弹窗请你确认：允许某个脚本使用这把 Key。系统会记下这个脚本的指纹，脚本以后被改动，就要你重新允许 |
| `xb-secret revoke <名字> <脚本>` | 撤销某个脚本的授权 |
| 换 Key | 重新 `set` 一次，旧值会被覆盖 |
| 删 Key | 你自己删掉 `~/.config/xiaoba/secrets/<名字>.env` 和 `<名字>.json` |

## Agent 用的命令

| 命令 | 作用 |
|---|---|
| `xb-secret has <名字>` | 查有没有这把 Key。没有时会提示你用哪条 `set` 命令 |
| `xb-secret list` | 列出名字、变量名、用途和授权脚本，**不显示值** |
| `xb-secret run <名字>[,<名字>] -- <已授权脚本> [参数]` | 把 Key 作为环境变量交给这个脚本运行。只有你 `allow` 过、而且之后没被改过的脚本才能拿到 Key |

## Agent 需要 Key 时的标准流程

1. 先运行 `xb-secret has <名字>`。
2. 如果没有，就停下来，请你运行 `xb-secret set …`，不要让你把 Key 贴进对话。
3. 调用 API 的脚本放在 `工具/` 或 `skills/*/scripts/` 下，写好后请你运行 `xb-secret allow`。
4. 之后都用 `xb-secret run` 调用这个脚本。

## 边界

- **能挡住的：**
  - Agent 直接读密钥文件。拦截启用后，守卫会挡住对 `~/.config/xiaoba/**` 的读写。
  - 没授权的脚本拿 Key。
  - 授权后被偷偷改过的脚本拿 Key。
- **挡不住的：** 你授权的脚本本身如果会把环境变量打印出来，Key 就会出现在输出里。所以每次 `allow` 之前，请让 Agent 先说明这个脚本做什么，只授权那些只调接口、不打印环境变量的脚本。
- 弹窗用的是 macOS 自带的 `osascript`。如果是在没有图形界面的环境里调用，脚本会改为在终端里输入，而且只接受你本人在终端里运行。
