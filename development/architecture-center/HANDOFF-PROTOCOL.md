# Agent 中立升级接续协议

接入者先读取待发布任务的 HANDOFF.md、ACCESS.json、change.json，再用中心命令读取权威状态。不得直接篡改投影或在工作树、活动包就地写入。此协议不授予调用模型、改认证或发版权限。

在仓库根目录执行下面的命令，通过 stdin 传入一个 JSON 对象；输出为 JSON。`--workspace` 必须是监看中心同一个更新工作区：

```bash
python3 -B development/architecture-center/cli.py --workspace /absolute/path/to/upgrade-workspace
```

| action | 参数 | 结果 |
|---|---|---|
| list | 无 | 模块和任务列表 |
| read | cid | 原卡、差异、修订号和历史 |
| claim | cid, actor, expected_revision | 新执行权 lease.id 和到期时间 |
| update | cid, actor, lease_id, expected_revision, operation | 下述候选操作 |
| validate | cid, expected_revision | 无执行权持有时进行语法及清单检查 |
| register_package | cid, actor, lease_id, expected_revision, mapping | 登记实际正式包及逐文件映射 |

示例输入：

```json
{"action":"claim","cid":"<change-id>","actor":"当前接续 Agent 的审计名称","expected_revision":1}
```

`update.operation` 支持：

- `write`：提供 `path`（仓库相对路径）、`body`；只写 ACCESS.json 范围内的候选文件，重建差异，清除旧验证、包登记和预检。
- `checkpoint`：提供 `summary`、`next_step`；说明实际完成、执行过的检查、未完成和阻断。
- `renew`：续租；所有操作都使用上次返回的最新修订号。
- `complete`：提供 `summary`；释放执行权，进入待检查。可由下一 Agent 重新认领。

接续 Agent 将构包结果放在该改动下的 `artifacts/package/`，保留原生要求的 FILES.sha256、RELEASE.json、agentos/scripts/switch_release.py 及全部验证材料。`mapping` 的键必须完整覆盖候选文件，值为正式包内相对路径，字节必须一致。路径穿越、软链接、漏文件、包漂移及基线漂移均停止。登记包不代表包已通过原生验收；中心不替代实际测试、审查或构包器。

如果规范文件不属于当前软件包发布内容，先在受审构包方案中安排其明确位置；不能拿一个不含该改动的旧包登记成新版本。超出单模块允许范围的依赖改动需先更新范围/拆出关联提案，不扩大既有执行权。

完成、检查后由用户在监看中心运行正式包预检并查看具体版本和差异，再点击正式发版。Agent 不通过这个 CLI 执行发布，也不能用自填 PASS、旧回执或改名掩盖失败。
