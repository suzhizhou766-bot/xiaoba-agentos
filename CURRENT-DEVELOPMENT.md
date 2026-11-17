# 当前开发状态

本仓库是代码仓库，不在这里写死线上版本号。

- **线上版本**：以运行根 `/Users/suzhizhou/Documents/小八AgentOS/.runtime/releases/index.json` 的 `current_release` 指针为准；用 `~/.agents/skills/xiaoba-agentos/scripts/verify-release` 回读 `current_release` 与 `release_dir`。本文件与仓库 README 都不写死线上版本号。
- **进行中的版本**：待办、预发布内容和候选目录登记在 `私人助理/工作区/README.md`（本机为 `/Users/suzhizhou/Documents/私人助理/工作区/README.md`）。
- **本仓库**：源码在 `development/`；`development/integrated-candidate/pyproject.toml` 里的 `[project] version` 是候选软件版本，不等于线上版本。

## 历史

以下为 2026-10-28 的时点记录，原文保留，不代表当前状态。

# 当前本地候选 · 2026-10-28

本次由用户明确要求上传目前修改。最新范围、真实失败和接续待办见[交接说明](docs/local-update-20261028/HANDOFF.md)。

源码位于development。真实AGY文件写回未通过；用户最新决定恢复原CLI调用方式并保留回执卡，但这项恢复尚未实施，上传的仍是当前持续交互PTY候选。不得将本次上传当作稳定验收或正式发布。生产保持v1.6.4。
