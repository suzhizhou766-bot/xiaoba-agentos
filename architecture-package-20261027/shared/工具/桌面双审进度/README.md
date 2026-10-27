# 桌面双审进度检查

当前用于小八 AgentOS v1.6.5 的 DSH、Qoder 桌面 FINAL 接续。脚本默认只读取双方任务卡约定的四个产物，以及固定索引、schema和索引绑定的材料。不会读取应用内部会话数据库、调用模型、发送消息或运行发布门禁。

运行：

```sh
/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14 -B /Users/suzhizhou/Documents/私人助理/工具/桌面双审进度/check_progress.py --write-report
```

`--write-report`只更新主持报告：
`工作区/小八AgentOS-v1.6.5-修复/desktop-review-20261026/manual-continuation-20261026/主持进度检查.json`。
省略该参数不写文件；增加`--json`显示完整检查结果。

读取的各端目录：

- `/private/tmp/xiaoba-v165-DSH-FINAL-manual-20261026/`
- `/private/tmp/xiaoba-v165-Qoder-FINAL-manual-20261026/`

每端仅读取`read-progress.json`、`dispositions-progress.json`、`final-receipt.json`、`blockers.md`。不会扫描工作区或应用私有目录，也不会改动审查者产物。支持稳定读取检查、UTF-8/JSON完整性、重复键拒绝、大小限制、符号链接拒绝，以及当前固定schema所用关键字的本地校验。该校验器只服务于哈希固定的schema，不作为通用JSON Schema实现。

最终稿还会核对当前会话身份、五绑定、232个精确覆盖键、69个处置键、原文件字节/行数/末锚、证据路径和行号。未知进度格式报告无法识别，不猜成零进度。最终格式齐备仍需主持核实桌面来源、真实全文阅读、69项裁决及18项义务，不能自动进入构包或生产。

无文件、文件修改时间较早或文件哈希不变，都不能证明桌面连续300秒无实际进展。脚本不依据文件沉默自动停止、重派或换模型。坏JSON可能是截断，也可能正写入，需复查与桌面观察。

2026-10-26用户安排两端使用私人助理工作区；桌面发现实际是新建会话，已分别补充身份说明。新会话不能直接继承旧187/40份已读声明。当前身份以接续目录`当前桌面会话观察.json`为准。

已在当前聊天创建每5分钟一次的进度跟进（automation ID：`automation`）。跟进调用本脚本、判断下一步；无实质变化保持安静。脚本本身不启动后台常驻进程，不设置系统定时任务，也不创建备份。
