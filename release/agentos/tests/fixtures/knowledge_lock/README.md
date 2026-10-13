# knowledge_lock 误引用回归语料

借鉴 clone_25 与 lock_vs_neighbor：一个权威「锁」页面，四周是标题/摘要高度接近的相邻页、别名页、
25 个克隆页，以及已归档/被取代页面。语料用来证明「相似页面不能冒充权威页面」。

这里是完全离线的合成内容，不含真实私人 Vault 数据。测试先把本目录复制到临时目录，再在副本上
做篡改（哈希漂移、符号链接、路径越界、归档状态），所以这里的文件本身永远不被修改。

| 路径 | 作用 |
| --- | --- |
| `vault/` | KnowLP 形状的迷你 Vault：`status` / `authority` / `source_task_id` 全部来自 frontmatter |
| `vault/70_复利规则/经验/lock.md` | 正确页面（active + authority + UUID `source_task_id`） |
| `vault/70_复利规则/经验/neighbor.md` | 相邻但错误的页面：标题与锁页几乎一致，canonical id 与内容不同 |
| `vault/70_复利规则/经验/alias.md` | 别名相似、canonical id 不同的页面 |
| `vault/70_复利规则/经验/clone_template.md` | 克隆页模板，测试按 `{index}` 展开成 25 份 |
| `vault/99_归档/` | `status: archived` 与 `status: superseded` 的历史页面 |
| `knowledge_index_template.json` | 价值文件索引模板；`path_ref` 由测试解析成绝对路径并计算 `sha256` |

每个页面正文都有唯一的 ASCII 标记（`LOCK_ACTIVE_MARKER`、`NEIGHBOR_WRONG_MARKER`、
`ARCHIVED_MARKER` 等）。断言依赖这些标记判断「哪一页的内容真正进入了 Agent context」，
而不是依赖 `assert False`。
