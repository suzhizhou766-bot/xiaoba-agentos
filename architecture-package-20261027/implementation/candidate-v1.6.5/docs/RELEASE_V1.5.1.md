# 小八 AgentOS v1.5.1 · 经验笔记可读文件名、第二大脑名字去版本号、输出上限不再误判

在 v1.5.0 之上的一批小改动。后台仍是 `agy_review` + `dsh_review`，分工、独立审核、确定性裁判、任务冻结与恢复、发布绑定校验的语义不变；`config/agentos.json` 的 `schema_version` 保持 `2`，数据库 schema 保持 `7`。

本次版本内的变更：

- 经验笔记改用可读文件名：发布进第二大脑的经验笔记，文件名由整串任务编号改为“标题 + 任务编号前 8 位”（`MemoryBridge.note_filename`）。标题取正文第一个一级标题，自动生成的候选取“任务”一节的第一行；标题末尾“（…待审核…）”这类候选阶段的标注在发布时去掉。`find_note` 同时认旧的整串编号命名和新的可读命名。
- 新增命令 `memory-rename-notes`（可加 `--dry-run`）：给已经发布的经验笔记改名，同步经验索引笔记里对应的那一行链接，重建并校验索引，再把新路径记进数据库并留一条 `memory.note_renamed` 事件。重复运行不再改动。预览同样先检查重名和索引链接；改名中途失败时先写回原文再删除新文件；上一次改了名但没记进数据库的，下一次运行会补记。
- 第二大脑的三个名字去掉版本号：目录 `第二大脑`、根笔记 `00_小八AgentOS.md`、索引目录 `knowlp`。配置、文档、技能卡、生成脚本和契约测试同步。这三个名字是兼容标识，不是版本号。这是一次性的例外改名（`scripts/agentos_version.py` 的说明同步写明），此后不随软件发布变化。
- 三个名字的线上实物在切换上线时一起改，不属于源码清单，由切换脚本在 Worker 停止期间完成：先检查三个旧名都在、三个新名都没被占用；依次改 Vault 目录、根笔记、索引目录（`.runtime/knowlp-v1.4` 改为 `.runtime/knowlp`），任何一步失败就按相反顺序改回并恢复 Worker；随后改写指向旧根笔记名的链接，用新路径更新 Worker 使用的配置，重建并校验索引。数据库里已发布经验记录的旧目录路径，在切换后用 `memory-rename-notes` 改到新目录。Obsidian 需要重新打开新目录一次。
- DSH 单轮输出达到上限不再误判：DeepSeek Harness 的输出里 `turn_end` 的结束原因是 `max-tokens`、且没有任何答案文本时，适配器抛出带 `external_upstream` 的永久错误，并提示把任务拆小。此前这种情况落到 `internal_unknown_candidate`，会建修复单、把后台置为修复中并要求新的发布候选。现在按外部原因处理：不建修复单，后台记为 `external_blocked`，重新探测通过后恢复。结束原因是 `max-tokens` 但已有答案文本时照常返回答案。顺带的行为变化：`final` 只有空白而文本流有正文时，改为返回文本流正文。
- 入口技能卡与 `私人助理/AGENTS.md` 的重复项收敛：技能卡补上“任何未验证项都必须明示”，登录材料与密钥两条注明是 AGENTS.md 的摘要，删掉与备份授权一节重复的半句。`.agents/skills/xiaoba-agentos/SKILL.md` 与 `interface/SKILL.md` 保持逐字一致。
- 测试里的旧占位后台名 `gpt_codex`、`claude_code` 换成现在的 `dsh_review`、`agy_review`（四个测试文件，纯替换）。`tests/test_store.py` 里说明退役记录的注释有意保留原名。
- `scripts/build_second_brain.py` 校验失败的报错去掉旧版本号。

不在本次范围：

- `internal_headroom` 修复通道原样保留。现有“冻结任务、同伴修复”机制的测试都以它为例，清理它要先决定这套自动修复机制的去留，留到以后的版本。
- 两条适配器规则仍只有单元测试证据：未执行的工具调用标记不算成功答案；验收结论必须恰好出现一次。审查材料里一出现相关样本就会被后台自己拦下，所以不进文本审查，验收记录里单独说明。
- 监看中心、Key 池、`integrations/review-center` 不在源码清单内，不属于本发布。
- 历史 `docs/RELEASE_V1.4.*.md`、`docs/RELEASE_V1.5.0.md` 与 `docs/HISTORY_VERSIONS.json` 的既有记录按只追加规则不改写。

分工记录：经验笔记改名与第二大脑改名由 Claude、DeepSeek V4.1 Flash、Qwen 3.8 Flash 完成；输出上限一项由 Qwen 3.8 Max 执行；测试占位名由 DeepSeek V4.1 Flash 执行；Gemini 3.8 Flash 对改名相关源码做了只读预审，提出的六处问题已全部修正并补了测试。各项由 Claude 复核后合并。
