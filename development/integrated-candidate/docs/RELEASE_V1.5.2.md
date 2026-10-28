# 小八 AgentOS v1.5.2 · 候选发布说明（未发布）

编写与核对时间：2026-10-10 17:45（+0800）。作者：F3（Qwen 3.8 Flash）。本文是可审阅草稿，不是发布批准。

状态：候选说明。活动发布由 `/Users/suzhizhou/Documents/小八AgentOS/.runtime/releases/index.json` 的 `current_release` 决定；2026-10-10 稳定入口 `verify-release` 回读为 `ok: true`、`current_release: v1.5.1`（`PHASE_FOUR_REVIEW.md`、`ACCEPTANCE_STATUS.json`），本文不声称已构包、已通过正式独立审核或已上线。候选的发布标识与 `RELEASE.json` 的 `status` 在构包时按 `docs/VERSIONING.md` 的候选规则确定（发布标识须等于软件版本或以其为前缀，只有完全相等时才允许写 `verified_immutable_release`），最终补丁号由发布审查决定。

后台仍是 `agy_review` + `dsh_review`（`config/agentos.json` 中两项 `enabled: true`、`worker.minimum_successful_agents: 2`），分工、独立审核、确定性裁判、任务冻结与恢复、发布绑定校验的语义不变；`schema_version` 保持 `2`，数据库 schema 保持 `7`。

第四轮代码缺口已由主力验证关闭，但本版仍未整版验收：M4已完成D4续审；其独立探针通过，F3并行文案修改未被冒认为其改动，本文所有「离线已通过」都不含正式审核含义。

## 十项范围与候选状态

读法：**已实现** = 候选源码里已存在且被离线测试覆盖；**离线已通过** = 只证明当前候选程序的行为，不等于正式审核或线上生效；**尚未执行** = 本版没做过，不得当成已通过。

| 项 | 实现状态 | 已验证到哪一步 | 尚未执行 |
|---|---|---|---|
| P01 宿主候选的显式标题参与审批哈希并用于发布 | 已实现：`store.py` 把标题写进审批哈希覆盖的候选文件 `title`，`memory.py` 在正文没有一级标题时读取它 | 离线：`tests/test_memory_publication_regressions.py` 5 项计入 593 基线，只改标题会作废终审批准。线上：搬家经验的标题、文件名、父笔记链接与数据库路径已修正，原题与「工作搬家 版本号」检索命中（`live-note-repair.json`、`live-retrieval-checks.json`） | 正式 AGY+DSH 独立审核；上线后在新活动程序里复测「无一级标题候选仍保留原题」 |
| P02 经验发布与索引提交不自动备份，失败用内存原文恢复 | 已实现：删除发布时新建 `backups/memory-publication/…` 与旧索引副本；失败路径用内存原文恢复目录与索引的字节和权限 | 离线：成功/失败发布都不新建备份、已有备份不删不改、索引提交失败恢复原字节与权限，均计入 593 基线 | 未上线；正式审核通过后需在发布验收里复测这四条，并确认没有默认开启的备份开关 |
| P03 审美手册的技能、模块登记与任务式匹配 | 已实现（staging 与守卫层）：`skills/catalog.json` 8 条目含 `aesthetics`；发布副本 `skills/aesthetics/SKILL.md`；实体读取约束集中在 `skills.py:verified_entity`（:138）与 `guarded_root_path`（:113，按写出顺序逐段检查根各段，任一段是软链即拒，覆盖上级段、中间多级段与 `..` 写法）；`doctor/skill-verify/skill-list/skill-match` 四个只读入口带 `--entity-root`，执行入口不接受该参数；`build_release.py:skill_index_verify_command`（:114）缺省不加覆盖参数 | 离线：`test_aesthetics` 59 项、`test_staging_acceptance` 23 项计入 593；staging 实体与发布副本逐字节相同（实测两文件同为 `7487c78182d9…55c9`）；staging 根核验通过、缺省正式根因实体缺失而失败；索引条目的 `entity_source_path` 始终是集中声明的正式位置，不因 staging 而改变 | 正式共享实体 `/Users/suzhizhou/Documents/私人助理/skills/aesthetics/SKILL.md` **未安装**（`ACCEPTANCE_STATUS.json:formal_root_check.aesthetics_entity_installed: false`）；安装后的缺省 formal 复验；`skill-match` 用任务式说法在**活动程序**里实测；registry 模块与模块清单落地（独立交付项，见 `reports/F3-safe-promotion-plan.md` §5）。动态手册正文与日报不进发布哈希，实现已按此约束 |
| P04 发版流程与审美入口纳入第二大脑生成 | 已实现：三节点冻结模板 `docs/second_brain/v152_extra_nodes.json`（实测 sha256 `c0c15b48dcc1…efe9`）+ 生成器接入与门槛 `build_second_brain.py:_load_extra_nodes`（:52）、`_merge_extra_nodes`（:99）、`TEMPLATE_FROZEN_IDENTITY`（:30） | 离线：临时树端到端 `{"ok": true, "notes": 50, "root": 1, "edges": 49}`，同参 `--verify` 复现；四条父分支各恰好 1 条子链接；两篇叶子逐字节复现线上原文（发版流程 `4f1336f6ef36…68e4`、审美手册 `68a79482c8a6…958d`，见 `first-wave-verification.json`）；模板缺项、重复、额外叶子、身份字段漂移、标题「（不变）」后缀都以明确错误退出（`test_second_brain` 12 项，含 6 项门槛） | 正式第二大脑落地（写线上 Vault）未执行；切换脚本对「线上已存在但不属于上一版生成树」的两篇叶子的接纳预检未实现，现状会命中 `activate_v151.py:217` 的 `New generated note already exists outside previous release` 而中止，属 S1 专卡 |
| P05 退役同伴自动修复与 Headroom 遗留修复通道，保留任务冻结与人工恢复 | 已实现：`RepairController`、session-start 建修复任务、冻结路径写 `repair_jobs`、同伴诊断、Headroom 自动重启、健康重探自动解冻全部删除且不留兼容空壳；`config.py:_validate_recovery_policy` 的 `automatic_actions` 契约改为「缺省或显式空列表通过，任何非空列表或错误类型拒绝」，`resume_requires_user` 必须为 `true` | 离线：`test_peer_repair_retirement` 19 项、`test_recovery_config_retirement` 10 项、`test_config_consistency` 13 项计入 593；`repair_jobs` 表、CHECK 约束、索引、历史行与 `repair.*` 审计事件保留；遗留 `resume_ready` 只由用户显式恢复推进；数据库版本仍 7 | 正式审核与切换未执行；上线后需确认 Worker 不租修复任务、`has_runnable_work()` 不计为可运行、历史行不被改写 |
| P06 经验文件名 40 字截断保留词边界 | 已实现：`memory.py` 的 `_FILENAME_TITLE_LIMIT`、`_WORD`、`_cut_point` — 标题部分 ≤40 码位，超出时整词退回；连字符、撇号、下划线算词内连接符；开头那个词本身超限时兜底「经验」 | 离线：`test_memory_title_edges` 22 项计入 593；M4 通过「回填两行即复现第三轮字节」反推确认截断与词边界规则一字未动 | 本轮**明确不扩大**：不新增标点边界处理、不把句点算作词连接符、不改截断规则。已发布笔记的历史文件名批量迁移未做，需用户单独授权 |
| P07 退役非真实的裁决测试经验 | 已完成运行态退役，不是待决定项：用户 2026-10-10 明确授权，回执 `test-memory-retirement.json`（`task_id 40cde45a-…`、`memory_status: retired`、归档 `archive_path` 已落盘、`backup_files_unchanged: true`） | 活动检索已排除该条；旧任务哈希与批准记录保留 | 发布后复查：仍为 `retired`、不进入活动检索、不被候选合并或索引重建恢复。**发版不得把它带回来** |
| P08 单轮输出预算耗尽后的任务级/后台级恢复边界 | 已实现：预算证据只取异常对象上的 `output_limit_exhausted` 结构化标识，不解析错误文本；预算耗尽按任务级失败，不自动原样重发、不把后台标为不健康；真实认证/网络/容量故障仍写 `external_blocked`/`degraded` 并由健康门阻断 | 离线：`test_g2_output_budget_marker_is_the_only_budget_evidence`、`test_budget_failure_keeps_health_clean_and_does_not_block_the_queue`、`test_real_environment_failures_still_gate_the_queue`、`test_unstructured_legacy_failure_keeps_the_conservative_gate` 等计入 593；旧式只有文本的冻结记录保守按环境故障处理 | 正式候选后台验收未执行（须待完整范围冻结后在沙箱里跑）；「不自动解冻」目前只有离线断言，线上未复测 |
| P09 两条适配器规则的真实正反例与误拦截核查 | 已实现并统一：运行期与构包两份标记集合逐条相同（实测 `output_hygiene.NON_ANSWER_MARKERS` 与 `build_release.NON_ANSWER_MARKERS` 均为 8 项、内容与顺序一致：invoke 标签开与合、parameter 标签开与合、DSML 竖线标记两种写法、tool-call serialization 两种写法）；构包侧两份扫描（聚合结果与各 Agent 输出）都查；推理泄漏另有硬签名 10 项、软签名 25 项（软需命中 ≥2 才计） | 离线：`test_output_hygiene`、`test_agy`、`test_dsh`、`test_release`（24 项）等计入 593；含不合格标记的正式结果仍按现有保守门槛拒绝，未放宽 | **正式双后台审核未执行**：G2 的正反例来自离线夹具与受控探针，不替代 AGY+DSH 对候选的独立审查；两份构包扫描的实际输出需在正式构包时再核 |
| P10 标题识别排除代码围栏内伪标题、正确处理末尾阶段标注 | 已实现：`memory.py:_prose_lines`（:501-530）过滤后供一级标题与「任务」节两处共用；闭合围栏仍要求同种字符、长度不小于开启行、行内无尾随内容；阶段标注只剥末尾完整且同种配对的括号，跨种嵌套保留。第四轮修正：判定开启行时只有反引号围栏才检查信息串的反引号（`memory.py:523`），波浪线围栏的信息串允许含 `~` 与反引号 | 离线：`test_memory_title_edges` 22 项（含 Q4 新增 4 项）计入 593；M4 独立复核 Q4 通过——24 项行为探针、7 条失败在旧字节上逐行复现的负向对照、22/60/179 项重跑 | 主力已核对CommonMark 0.31.2第4.5节及示例146（见工作根PHASE_THREE_REVIEW）；M4已补完D4续审。正式AGY+DSH审核和上线后复测仍未执行 |

## 测试基线与证据边界

- 593 项是**当前候选程序**的离线回归基线：`Ran 593 tests in 6.955s / OK`（`phase-4-offline-tests.log`），绑定 `phase-4-verification.json` 的 162 个输入文件指纹。构成：第三轮 574 + Q4 新增 4 + D4 新增 15（`PHASE_FOUR_REVIEW.md`）。
- 输入稳定性：两次指纹相隔 206.8219 秒（下限 60 秒），候选与报告文件零漂移（`phase-4-stability.json`）。
- 追加探针：四个围栏探针（含带 `~` 与反引号的信息串、未闭合围栏、伪任务节）与间接软链/`..` 消证据写法全部按预期拒绝或放行。
- 历史基线：440（首轮 P01/P02）→ 506（第二轮）→ 574（第三轮）→ 593（第四轮），逐轮绑定各自输入指纹；**593 只能引用为已验证的当前程序基线，不得写成正式发布测试数、正式审核通过数或线上通过数**。
- 本版整版测试数与改动文件数在定稿冻结时由实际文件和清单给出，不预先写死。
- 全程 `live_model_calls: 0`，未构包、未切换、未启停服务、未读正式运行库。

## 候选版本与清单冻结

- 主力已统一候选 `pyproject.toml`、发布绑定、当前入口与说明为 v1.5.2；活动发布仍为 v1.5.1。
- 技能/知识索引和源码清单由主力依次再生、逐项核验，最新哈希与实际文件计数见工作根 `FREEZE_V1.5.2.json`。源码说明不嵌入自身清单哈希，避免自引用。
- 旧 c831f768… 清单对应最初P01/P02修复候选，不是原v1.5.1未改源码；F3早期观测31项漂移，M4续审时为32项。文案本身也受管，计数随修改变化，当前以冻结记录为准。
- M4已追加D4续审，D-1…D-16通过；F3文案并行变化已如实归因。593项历史结果只绑定第四轮当时指纹；版本/文案冻结后的新结果另在冻结记录中保存。

## 本版新增的模板与文档同步

- `docs/second_brain/v152_extra_nodes.json`：JSON 数组，三项，每项使用生成器 node 结构 `path`/`id`/`type`/`parent`/`authority`/`title`/`body`/`source_path`/`last_verified`。前两项是 `40_工作流/AgentOS发版流程.md` 与 `30_方法与知识/小八审美手册.md` 的完整固定正文，`last_verified` 为 2026-10-10，metadata 与线上原文一致，历史指路与加入日期原样保留；第三项只替换 `10_治理与裁决/故障自愈与任务恢复.md` 的 `body`，其余身份与属性沿用现值（`source_path` 为空、`last_verified` 为 2026-09-26）。生成器读取本模板后，构建不再读取线上正文；`审美提升/审美手册.md` 本体不进模板。
- 生成器门槛：模板必须恰好包含 `recovery-policy`、`agentos-release-workflow`、`aesthetics-handbook` 各一次、九键齐全、冻结身份逐字段一致；模板正文更新只允许 `recovery-policy` 的 `body`。
- 恢复机制现行表述同步位置：`docs/ARCHITECTURE.md`、`docs/OPERATIONS.md`、`docs/ACCEPTANCE.md`、`interface/SKILL.md` 与 `.agents/skills/xiaoba-agentos/SKILL.md`（两份逐字一致）、`skills/collaboration-core/SKILL.md`。
- 备份授权规则原文未改；历史发布说明与 `docs/HISTORY_VERSIONS.json` 按只追加规则不改写（本文也没动它们）。

## 不在本次范围

- 正式实体安装、registry 模块合并、切换脚本对两篇已在线叶子的接纳预检（S1）、正式第二大脑落地。
- 正式版本提升与上线后核验（归主力；本文件只描述候选）。
- 标点边界处理、句点作词连接符、按字符强切截断、Markdown 容器（列表/引用）级解析、把缩进匹配从 0–3 空格放宽到任意空白：本轮都不做，不因为「顺手」而扩大改动面。
- 空子目录的额外收紧：主力已裁决本版不收紧（空目录不增加可读或可执行内容，软链目录与含额外文件的目录继续拒绝）。
- 监看中心、Key 池、`integrations/review-center` 不在源码清单内，不属于本发布。
- 实体 MiniMax H3 技能不随 AgentOS 发布，不并入本版。

## 发布门槛（候选冻结与正式发布分开记录）

1. **离线**（已做到当前程序基线）：Python 全套 593 项 + 新增边界回归 + 围栏/间接软链探针。
2. **候选程序冻结**（主力执行，实际结果见FREEZE_V1.5.2.json）：主力统一版本号，再生技能索引/知识索引/`SOURCE_MANIFEST.json`，记录源码清单、测试日志、预审报告与证据哈希；计数从文件和清单算，不手写失效数字。候选再修改即更新指纹，按影响重新验证；正式审核后改动必须重新审核。
3. **正式独立审核**（未执行）：仍由候选在隔离运行库中使用AGY+DSH两个后台各自独立审查，桌面自选模型与登录材料保持不变，辅助模型回执与首轮预审不替代；两侧都要有非空合格结果，任何 FAIL、缺证据或错误都阻断。该要求不因「退役同伴自动修复」而取消。
4. **构包与只读切换预检**（未执行）：正式包、`--verify` 类只读核验、提升前顺序与停止条件见 `reports/F3-safe-promotion-plan.md`（草稿，不是部署批准）。
5. **用户授权后的提升**（未授权、未执行）。
6. **上线后读回**（未执行）：活动版本、Worker、发布哈希、技能 `source_path`、第二大脑树与 KnowLP 索引；复测冻结/恢复语义、预算耗尽的任务级边界、测试经验仍退役、无一级标题候选保留原题、成功与失败发布都不新建备份且已有备份不变。真实模型可用性须用新鲜受控探针验证，旧健康记录不算。

## 分工

候选发布说明与三节点冻结模板由 Qwen 3.8 Flash 起草（F2/F3）；第二大脑生成器与模板门槛由 DeepSeek 修改（D2/D3），实体根守卫与测试隔离由 DeepSeek 完成（D4）；退役自动修复与显式恢复由 Qwen Max 完成（Q2，实际实现按 `ACCEPTANCE_STATUS.json:actual_contributions` 记录归属），配置契约由 Gemini 清理（G3），标题与文件名边界由 Qwen Flash 实现、Qwen Max 独立复核（Q3/Q4）；适配器与构包门槛由 Gemini 完成（G2）；正式审核、版本号统一、清单再生、构包与安全提升由主力保留。各卡回执须由主力回读源码核实后集成，回执自述不等于文件已改或测试通过。M4的D4续审已通过；本版正式独立审核、构包和提升尚未执行。
