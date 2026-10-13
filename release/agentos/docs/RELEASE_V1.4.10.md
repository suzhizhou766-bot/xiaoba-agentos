# 小八 AgentOS v1.4.10 · 裁决中心并入小八监看中心

用户裁决界面改在小八监看中心（`http://127.0.0.1:8799/`）的「裁决中心」标签里。裁决后端仍是 `integrations/review-center` 的同一份审核代码——候选版本比对、证据哈希校验、经已校验发布的 CLI 写审核、发布与索引校验都没有改——但不再是常驻的独立服务：由监看中心按需拉起并看护，只监听本机的内部端口，没有自己的网页。原 `127.0.0.1:8797` 页面与 `com.xiaoba.review-center` LaunchAgent 已停用（plist 保留为 `.disabled-20261005`）。

本次版本内的变更：

- `doctor` 的 `config_consistency` 新增 `review_backend`：裁决后端运行时把实际使用的配置记录在 `<runtime_dir>/review-center/backend-state.json`（由监看中心的后端启动器 `私人助理/工具/小八监看/monitor_core/review_backend.py` 写入，属于本发布之外的组件），doctor 据此与会话、已安装入口、Worker 一并比对；记录里的 `config_path` 必须是非空字符串。后端没在运行或记录过期时跳过。旧 LaunchAgent 的比对规则保留，重新启用时照常生效。
- 调用入口（`SKILL.md`）、协作核心技能、架构与运维文档改为新的裁决入口，并写明：监看中心没开时不能在浏览器里裁决，待审队列也不会同步到第二大脑；CLI 的 `review-list`/`review` 不受影响。
- 知识索引按价值文件现状重建：`视觉知识库/05_提示词库/README.md` 在 2026-10-05 有改动，旧索引哈希不再匹配，导致 doctor 不通过；本次按现状重建。

不在本次范围：`integrations/review-center` 不在源码清单内，本次未改动其中任何文件；监看中心是独立工具，位于 `私人助理/工具/小八监看`，不属于本发布。

版本更新不创建备份，不批准待审经验，不修改模型或上游配置。
