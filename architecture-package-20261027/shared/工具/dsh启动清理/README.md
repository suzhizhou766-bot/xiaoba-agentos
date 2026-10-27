# DSH 启动清理

修复 DeepSeek Harness「点图标后窗口 10–60 秒才出来」的清理工具。

## 问题与根因（2026-10-24 实测）

DSH 桌面版启动时，主进程要等宿主 `backend ready` 才显示窗口（`main.js`：`backend.state.phase !== "ready"` 时不开窗）。宿主加载 profile 时会对 `~/.dsh/storages/session_projcache/sessions/` 的**每条**缓存记录做一次校验，而校验调用的 `findLog(id)` 每次都要把 `~/.dsh/sessions/` 下**所有**项目目录扫一遍（`readdir` + 每目录 2 次 `open` 探测 + `realpath`）。

当会话存储里攒了：

- 2000+ 个项目目录（绝大多数是 xiaoba AgentOS 在临时目录跑 DSH 留下的空壳，形如 `--private-var-folders-…-T-xiaoba-*--`）；
- 800+ 条陈旧缓存记录（对应会话早已不存在）；

启动成本约为 `800 × 2000 ≈ 480 万次文件操作`，全部是 ENOENT。CPU profile 里热点正是 `node:internal/fs/promises` 的错误构造与 `openFileHandle`。

同一份数据 A/B 实测（克隆到 /tmp，不动真实数据）：

| 状态 | 宿主 boot-to-ready |
|---|---|
| 清理前 | **188.2 s** |
| 清理后 | **2.5 s** |
| 清理后（同步真实数据、含 union 插件）| **3.72 s** |

## 用法

```sh
# 只预览，不动任何文件
python3 /Users/suzhizhou/Documents/私人助理/工具/dsh启动清理/cleanup.py

# 实际执行
python3 /Users/suzhizhou/Documents/私人助理/工具/dsh启动清理/cleanup.py --apply
```

执行后文件被**移动**到 `~/.dsh/_cleanup-<日期时间>/`（不删除），并写一份 `说明.md`。确认 DSH 一切正常后，整个目录可以直接清空；若有异常，把 `sessions-empty/`、`projcache-stale/` 里的东西原样移回即可回滚。

- 只移动「内部没有任何 `*.jsonl` / `*.jsonl.zstd`」的项目目录；
- 只移动「id 已不存在对应会话目录」的缓存记录，存活会话的缓存一律保留；
- 不动 `session_projcache_archive_manager_v2`（第三方插件 @michengai/dsh-archive-manager 自己的缓存）；
- 移动是 rename，DSH 运行中执行也安全，但**重启 DSH 后才生效**。

## 防复发

空目录主要是 xiaoba AgentOS 在临时目录里跑 DSH 攒出来的，会持续增长。

本工具已做成 Agent 自动化任务（2026-10-24 用户定：定时任务用 Agent 自己的调度，不往系统额外塞 launchd/cron）：DSH 计划任务 `schedule-c06ce8c2-fb62-4cdc-9ace-d008de98f99d`，每周日 03:30（Asia/Shanghai）用统一解释器 `配置/python/cpython-3.14.7-…/bin/python3.14` 执行 `cleanup.py --apply` 并汇报结果。查看/修改用 DSH 的 schedule 清单；换接入 Agent 时在对应 Agent 的调度里重建同款任务即可。

## 上游问题（可反馈给 DSH）

`@deepseek-ai/dsh-session-persistence-jsonl` 的 `findLog(id)` 是「对每个 id 全量扫项目目录」，`@deepseek-ai/dsh-session-query-sqlite` 启动时 `_reconcile()` 会遍历全部缓存记录做校验，二者相乘是 O(N×M)。建议上游：启动时按一次 `list()` 结果重建索引，而不是逐条 `findLog`；或给项目目录/缓存记录加定期剪枝。
