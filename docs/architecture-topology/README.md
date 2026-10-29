# 小八架构拓扑

用户维护的小八架构设计底座。用户在交互页面上增删节点、接入或断开连线、写下每个模块的意图和问题；这里是它的快照，供所有接入的 Agent 读取。

## 先读哪个

| 文件 | 是什么 |
|---|---|
| [AGENT_READER.md](AGENT_READER.md) | 文字版：每个分组的节点、状态、用户意图、代码位置、问题与待办、全部连线和按日期的变更历史。**Agent 从这里开始读** |
| [topology.json](topology.json) | 拓扑原始数据：`groups`、`nodes`、`edges`，`savedAt` 为快照时间（毫秒） |
| [changes.json](changes.json) | 变更记录（新→旧）：`op` 为 add / edit / del / link / unlink；删除和断开的原内容在 `before` 里 |
| [topology-page.html](topology-page.html) | 交互页面源码，留作维护 |
| [render_reader.py](render_reader.py) | 由两个 JSON 重新生成 `AGENT_READER.md` |

## 规则

- 实时版本在用户的 claude.ai 页面里（私有链接，由用户本人打开和修改）。本目录是同步快照，以 `AGENT_READER.md` 开头的快照时间为准；用户在页面上的修改要再同步一次才会出现在这里。
- “用户意图”写的是用户想要的样子。代码和它不一致时，向用户报告差异，不要自行改图，也不要直接改这里的 JSON 冒充用户修改。
- 状态为“待用户裁定”的节点，裁定前不要实现。
- 动手前先看变更历史，避免重做用户已删除或已改掉的东西。

## 重新生成读本

```sh
python3 docs/architecture-topology/render_reader.py
```

首份快照：2026-10-29，载入 2026-10-27 设计图基线（5 个分组、54 个节点、59 条连线），并标注当日代码核对发现的问题与待裁定项。
