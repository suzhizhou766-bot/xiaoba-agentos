# v1.4.4 验收证据

本文件记录 v1.4.4 版本标识同步候选的验收证据。状态标注只允许 `passed` / `pending` / `not_run` / `failed`；凡无原始日志、回执或数据库记录者一律保持 `pending` 或 `not_run`，并写明缺什么。本文件不把候选说成已提升的正式版本，不声称任何未执行的验收已通过。所有时间为 Asia/Shanghai 绝对时间。

## 0. 候选身份与基线

| 项目 | 值 | 来源 |
| --- | --- | --- |
| 软件版本声明点 | `pyproject.toml` 的 `[project] version = "1.4.4"`，由 `scripts/agentos_version.py::software_version()` 读取 | 候选树内文件 |
| 候选包 `RELEASE.json` version | `v1.4.4` | 候选包 |
| 候选包 `RELEASE.json` status | `release_candidate`（**不是** `verified_immutable_release`） | 候选包 |
| 候选工作目录 | `.runtime/release-candidates/v1.4.4-candidate-20261001-002253` | 候选目录名承载候选身份；`RELEASE.json` 的 version 不带候选后缀，因为提升时不改版本号 |
| 生产活动发布 | 仍是 `v1.4.3`，本候选未提升 | 生产 `.runtime/releases/index.json` |

**不可继承声明。** v1.4.3 的 360 项离线测试通过记录绑定源码清单 `a60f38c0f51adb4a33efb7fcda797b8115a8930112919b03ccd6e12c52574ce9`（日志 sha256 `5a67a9f9f773ab04b7b600d68ae028bd9b936bc87d7860066132b2fa2cf6296e`）。本候选源码清单与之不同，因此 v1.4.3 的任何测试、验收与绑定结论都不得继承到 v1.4.4，只能作参考背景。

**生产零写入基线**（建隔离运行时之前采集，`evidence/production-before-sandbox.json`）：生产 config sha256 `3111d3041f5cdafd03ed0b4ddc760bff978ea3549a7fbe7a4277ea9af1556836`；生产 release index sha256 `575a5f708c00ac82ba886267cfb36a32c86517974ab506703e4fb4ba7f47eb05`，`current_release=v1.4.3`；生产数据库 sha256 `7facf00fddd272408a8f1bc9c01c0feb2e4798ea9d7a067bd7e63c5ba5e872dc`，`-wal` 字节数 0；生产工作区 `SOURCE_MANIFEST.json` sha256 `a60f38c0…`，受管文件 122 个；生产 `.runtime/knowlp-v1.4` 3 个文件；已安装 skill `SKILL.md` sha256 `3d1e9233bc0e1b598207e81bbb8a3b4ebfa050f58ef64bed7acb32b3f5cded77`，入口脚本 sha256 `3a06d7f75ef8a54de05bd8e09169dcc57a98aca3abe90f77b63ddf89de0640db`。交付前须复采同一组哈希逐项比对；在拿到复采结果前，生产零写入只能记为「基线已记录」，不能记为「已复核」。

**相对活动发布的受管清单增量**（`evidence/delta-vs-promoted-v143.json`）：122 → 128 个受管文件，新增 6、修改 17、删除 0。新增：`docs/ACCEPTANCE_V1.4.3_EVIDENCE.md`、`docs/ACCEPTANCE_V1.4.4_EVIDENCE.md`、`docs/RELEASE_V1.4.3.md`、`docs/RELEASE_V1.4.4.md`、`docs/VERSIONING.md`、`scripts/agentos_version.py`。

## 1. 隔离验收运行时

本候选的双后台验收在 `/tmp` 下的隔离运行时进行，不在生产运行时进行。

- 沙箱根目录 `/tmp/xiaoba-v144-accept-20261001`；沙箱 config `release.acceptance_mode=true`、`release.expected_version=v1.4.4`、`runtime_dir` 与 `release.index_path` 均在 `/tmp` 下、`memory.publish_enabled=false`、Vault 指向沙箱内 58 个文件的副本、`knowlp_graph_dir` 指向沙箱内隔离目录、独立数据库。
- 隔离运行时索引只有 v1.4.4 一条 `release_candidate` 记录，`current_release=v1.4.4`；生产索引未被读取也未被写入。
- 绑定通过**稳定入口脚本**验证，未绕过入口。`verify-release --config <沙箱 config>` 首次输出：`ok=true`、`skipped=false`、`acceptance_mode=true`、`current_release=v1.4.4`、`expected_version=v1.4.4`、`files_manifest_sha256=fba8c444ca46e6449aec11692b7ce490be78876a5465ab2170b34e1c715a77dc`、release 与 runtime 两侧源码清单同为 `c751b7f56b1f902b0ff8febb92157767920735fd0db68bdc41bc522619478431`。该组哈希属于**集成前**的引导构建；集成后的最终指纹见 §8。
- `acceptance_mode` 的三项强制约束均由代码校验并被本沙箱满足：索引与运行时路径必须在临时目录下、`memory.publish_enabled` 必须为 `false`、索引条目与 `RELEASE.json` 的 status 必须为 `release_candidate`。

**漂移 fail-closed 实测。** 集成阶段修改 `docs/RELEASE_V1.4.4.md` 与 `docs/HISTORY_VERSIONS.json` 之后、重建包之前，同一沙箱的绑定立即失效，`release_binding_status` 返回 `ok=false`，`problems=["runtime source manifest mismatch: docs/HISTORY_VERSIONS.json, docs/RELEASE_V1.4.4.md"]`，逐字点名被改动的两个文件（`evidence/drift-fail-closed-after-integration.json`）。这说明「改了受管文件就必须重建并重绑」不是约定，而是运行时强制。

## 2. 双后台健康探针

`session-start` 在隔离运行时执行（`evidence/sandbox-session-start.json`，2026-10-01T00:52 前后）：`status=ready`、`doctor_ok=true`、`ready_for_external_tasks=true`、`live_probe.ok=true`、`healthy_agents=[agy_review, dsh_review]`、`failed_agents=[]`、`remote_inference_tested=true`、`session_mode=dual_agent`、`memory_candidate_created=false`；沙箱 session `48d08dea-d636-49cb-8d3c-32a596b90525`。

**明确区分：** 健康探针通过**不等于**真实业务任务交付成功；`doctor` 通过**不等于**双后台端到端审核通过。健康是带时间的证据，不保证上游后续可用。

## 3. 双后台工作阶段（collaborate）

工作阶段在隔离运行时真实执行，两个后台各自独立产出，未互相看见对方文本。

| 项目 | 值 |
| --- | --- |
| message-id | `v144-metasync-work-20261001-01` |
| 任务 ID | `8398edf1-aaca-4300-ab99-d8e3b21cf61c` |
| phase / status / next_stage | `work` / `completed` / `integration` |
| 冻结上下文 | `work-context.txt`，32309 字节（31.55 KiB，≤ 64 KiB 上限），sha256 `338c9917fc9f17c55bae6b19257acfee2b3a1d679ca436f2e10d92ff898986c2` |
| 分工 | `agy_review` 起草 `docs/RELEASE_V1.4.4.md` 正文与 `docs/HISTORY_VERSIONS.json` 的 v1.4.4 记录字段；`dsh_review` 起草本文件并独立审计版本同步有无漏改、有无误升冻结兼容名、有无改写历史记录 |
| `agy_review` 运行 | run `5772dbc3-681a-4ce2-b3db-eb9a7adfd9fd`，`completed`，延迟 114583 ms，输出 2384 字符，output sha256 `0681b0f978cf048ffa2c2c60d87d3f77d4256c523facd55b1851496607c5023d`，receipt sha256 `2f2d6cf10a973d026951c1a28c613c874882c150b51402df5062e1fdab686878` |
| `dsh_review` 运行（第 1 次尝试） | run `1a71b557-39ad-423a-84e8-5d09593c62fd`，`transient_error`，`failure_class=external_upstream`，延迟 300075 ms，错误 `dsh timeout：dsh_review` |
| `dsh_review` 运行（第 2 次尝试） | run `745fd276-5f54-461a-89f3-1ef5747c3756`，`completed`，输出 16735 字符，output sha256 `2c34f0d946d507464c3a84c54e3aa777eae9b55f107f19224b4c99565dc6bbf2`，receipt sha256 `faa2172ae0d27f4ec4b84a8373749c59c0a49b7db183fcff5fb3529b7aa9e694` |
| 任务状态流转 | 第 1 次 `run --once` 返回 `retry`（`retry_in=30`，仅 `dsh_review` 失败）；退避后第 2 次 `run --once` 返回 `completed`。`agy_review` 第 1 次的合格输出被保留复用，未重复调用 |
| 原始输出副本 | `evidence/work-attempt1-agy-output.txt`、`evidence/work-attempt2-agy_review-output.txt`、`evidence/work-attempt2-dsh_review-output.txt` |
| 裁判与记忆 | 工作阶段无裁判决定、无记忆候选，符合 `docs/COLLABORATION.md`；`memory.publish_enabled=false`，未批准也未发布任何记忆 |

**DSH 第 1 次超时的根因（结构性，非偶发）。** `adapters.py` 的 DSH 分支把 Agent 的 `timeout_seconds` 预算按跟随到的模型数均分：`timeout = remaining / (len(models) - index)`。生产 config 的 `dsh_review.timeout_seconds=300`，本次跟随到 6 个模型（`mimo-v2.6-pro`、`deepseek-flash`、`deepseek-v4-pro-0813`、`glm-5.3`、`glm-5.3-flash`、`mimo-v2.6-flash`，provider 均为 xiaomi），因此每条路由只有约 50 秒；6 条路由各约 50 秒耗尽 300 秒预算，全部记 `dsh timeout`。第 1 次尝试的 6 条 `agent.provider_attempt` 事件延迟为 50017 / 50013 / 50013 / 50003 / 49999 / 49974 ms，与该公式一致。`agy_review` 只跟随 1 个模型（Antigravity `gemini-3.8-flash-low`），独占全部 300 秒，故长文本生成可以在 114583 ms 内完成。

含义：在路由跟随设计不变的前提下，DSH 单条路由的可用时间随跟随到的模型数成反比下降，长输出任务在模型数较多时会在每条路由上都超时。本候选**不修改** `~/.dsh/profiles/web/cordis.patch.yml`（跟随式路由是刻意设计，不得加白名单），也**不修改** adapter 代码（超出「版本标识与验收元数据同步」范围，且会破坏 `src/` 与 v1.4.3 候选逐字节相同的归属证据）。第 2 次尝试在同样的 50 秒/路由预算内成功，说明该失败是可恢复的上游慢响应，已由既有有界退避策略吸收。

## 4. 后台产出的宿主校验与两处事实修正

后台文本不被当作已执行补丁。宿主对两份产出的全部可核验断言逐条回读磁盘，28 项断言 28 项与磁盘一致（含两个日志 sha256、包指纹、文件计数、索引字段、历史记录受保护字段、`diff -r src/ tests/` 为空）。集成时修正了后台产出中的两处事实错误：

1. `dsh_review` 草稿把候选发布标识写成 `v1.4.4-candidate`。实际 `RELEASE.json` 的 `version` 是 `v1.4.4`，候选身份由 `status=release_candidate` 与候选目录名承载。已按磁盘实际值改正。
2. `agy_review` 草稿写「新增 `tests/test_current_release.py` 版本契约测试」。该文件已存在于活动 v1.4.3 包内（5 项，28 行）；本候选是把它扩充到 9 项（94 行），新增四项版本契约断言，离线套件由 360 项增至 364 项。已按磁盘实际值改正，并补上 `agy_review` 漏掉的三项增量：`scripts/agentos_version.py` 为新增文件、四个生成器改为单点读取版本、活动 v1.4.3 包内缺失的 `docs/RELEASE_V1.4.3.md` 与 `docs/ACCEPTANCE_V1.4.3_EVIDENCE.md` 由本候选补入。

`dsh_review` 的独立审计结论：在其拿到的上下文范围内，版本同步无漏改、无冻结兼容名被误升、无历史记录被改写；对未获得逐行正文的两个新增 docs 文件与未展开 diff 的 `src/` 生成器，它主动判为「证据不足」而非「通过」，并要求宿主补做逐字节校验。这两类保留项已由宿主在集成时补齐：新增 docs 的正文即本文件与 `docs/RELEASE_V1.4.4.md`，`src/` 与 `tests/` 相对 v1.4.3 元数据同步候选 `diff -r` 无输出。审计全文见 `evidence/work-attempt2-dsh_review-output.txt` 的 `=== AUDIT ===` 段。

## 5. 离线测试

| 运行 | 对象树 | 结果 | 原始证据 |
| --- | --- | --- | --- |
| 集成前 | 引导构建树，源码清单 `c751b7f5…` | pytest `364 passed, 75 subtests passed`，退出码 0；unittest `Ran 364 tests` / `OK`，退出码 0 | `evidence/bootstrap-pytest.log` sha256 `5e02889d6c0d2904a96e8870260a2c5fa42fe8a2fbc65714bae75e918e01fc12`；`evidence/bootstrap-unittest.log` sha256 `71e55dbe92a5a3c4110519dee1a217efb399a67d399750974d8aced472d93646` |
| 集成后（最终树） | 本文件写入后的最终候选树 | 由宿主在本文件定稿后复跑；结果、日志路径与日志 sha256 记录在候选目录外的 `evidence/EVIDENCE_INDEX.json` | 本文件不内联该哈希：本文件自身是受管文件，内联最终树指纹会形成自引用 |

执行环境两次运行相同：`HOME=/nonexistent`、`PYTHONDONTWRITEBYTECODE=1`、`PYTHONPATH` 指向候选 `src`、pytest 加 `-B` 与 `-p no:cacheprovider`；执行后候选树 `__pycache__` 数为 0。解释器：pytest 用 `/tmp/xiaoba-uv-cache/archive-v0/7ZSydRo_4lH8vt8V/bin/python`（pytest 9.1.1 on cpython 3.11.16），unittest 用 `/usr/bin/python3`（3.9.6）。版本契约测试 `tests/test_current_release.py` 9 项全部通过。

离线测试通过只证明离线套件在该次运行中通过，**不**证明真实业务任务交付成功，**不**覆盖端到端双后台生产验收。

## 6. 已知间歇缺陷（既有，非本版引入）

- 现象：`tests/test_workflow.py::test_work_then_separate_review_with_artifact_drift_gate` 在全量套件中偶发失败，`outcome['status']` 为 `retry` 而非 `completed`，errors 为 `c: [Errno 2] No such file or directory: '<临时目录>/runtime/agentos.sqlite3-shm'`。
- 频率：候选树上 10 次全量运行中 1 次失败；单独运行该文件 3/3 通过；与其他任一测试文件两两组合运行均通过。10 次中只保留了失败那一次的原始日志（`evidence/known-issue-wal-shm-race.log`，sha256 `1f24e991f6eac110fa2ba1e053ad28960313674d419f99e40e234a64378df5a0`，含 `--showlocals` 输出的完整 errors 字段），其余 9 次通过的日志未逐次留存，因此「1/10」是本次观测频率，不是统计意义上的失败率。
- 定位：SQLite WAL 模式下 `-shm` 共享内存文件在并发连接间的删除竞争。该测试 config 的 `worker.parallelism=2`，orchestrator 用线程池并发跑 Agent，每个 Agent 运行都写同一个库。
- 归属证据：本候选 `src/` 与 `tests/` 与 v1.4.3 元数据同步候选逐字节相同（`diff -r` 无输出），故该缺陷不是本版版本同步引入，属既有并发缺陷。
- 处置：本候选不修复它（超出「版本标识与验收元数据同步」范围），只如实记录，并写入候选包 `RELEASE.json` 的 `known_external_issues`。
- 对结论的限定：因此 §5 的「364 passed」只代表该次运行通过，**不得**改写为「全量运行稳定通过」。§5 与 §6 并存不构成「矛盾已消除」，只构成「矛盾已被如实记录」。

## 7. KnowLP 校验

在隔离图目录上执行，生产 `.runtime/knowlp-v1.4` 未写入。

| 项目 | 值 | 原始证据 |
| --- | --- | --- |
| 构建 | `{"ok": true, "method": "v1.4_structural_tree_only", "analyzed_docs": 0, "nodes": 52, "edges": 51}`，退出码 0 | `evidence/knowlp-build.log` |
| 校验 | `{"ok": true, "method": "v1.4_structural_tree_only", "analyzed_docs": 0, "nodes": 52, "edges": 51}`，退出码 0 | `evidence/knowlp-verify.log` |
| 输入 Vault | 沙箱内副本（58 个文件），非生产 Vault 本体 | 沙箱 config |
| 输出图目录 | `/tmp/xiaoba-v144-accept-20261001/knowlp-v1.4`，3 个文件 | 沙箱 |
| 生产图目录 | 3 个文件 sha256 与 §0 基线逐项相同，未写入 | 复算比对 |
| 沙箱图与生产图 | 逐文件 sha256 完全相同，说明 v1.4.4 候选未改变 KnowLP 结构树产物 | 复算比对 |
| 方法学边界 | `analyzed_docs=0`、`method=v1.4_structural_tree_only`：这是结构树校验，不含深度语义抽取，也未调用模型或动态 embedding | 脚本输出 |

## 8. 索引、生成物与包指纹

| 项目 | 值 |
| --- | --- |
| `skills/index.json` | `release=v1.4.4`、`schema_version=1`、7 条 |
| `knowledge/index.json` | `release=v1.4.4`、`schema_version=1`、16 条 |
| 第二大脑预览 | 39 篇笔记、单根；较 v1.4.3 的 38 篇多 1 篇 `历史版本记录/v1.4.4.md`（生成器按 `HISTORY_VERSIONS.json` 逐条出卡，属预期） |
| 技能卡 `source_path` | 指向当前已发布版本 `.runtime/releases/v1.4.3/agentos`，与活动 v1.4.3 包内技能卡指向 `v1.4.2` 的既有先例一致 |
| 数据库 schema | 7，未迁移 |
| 冻结兼容标识 | `第二大脑_小八AgentOS_v1.4.1`、`00_小八AgentOS_v1.4.md`、`knowlp-v1.4`、`~/.agents/skills/xiaoba-agentos`、`~/.codex/skills/xiaoba-agentos` 均未跟升，由 `test_compatibility_identifiers_are_never_version_bumped` 断言 |

集成后最终包的 `SOURCE_MANIFEST.json`、`FILES.sha256`、`code_fingerprint` 与受管文件数记录在候选目录外的 `evidence/EVIDENCE_INDEX.json` 与 `ACCEPTANCE_SUPPLEMENT.json`；本文件不内联它们，因为本文件自身是受管文件，内联会形成自引用。集成前的引导构建值为：源码清单 `c751b7f5…`、`FILES.sha256` 自身 `fba8c444…`、`code_fingerprint` `6e18618b8ab495b0e902dbd137e72abda30d1d3dd93560a9818e6f9b0286ae3b`（该指纹只覆盖 `src/xiaoba_agentos/*.py`，与 v1.4.3 元数据同步候选相同，因为 `src/` 逐字节未变）、受管文件 128、包内文件 178、包内 `__pycache__` 0。

## 9. 未执行 / 未验证门槛

| 门槛 | 状态 | 缺少的原始证据 |
| --- | --- | --- |
| 双后台审核阶段（`collaborate-review`） | pending | 需要集成后重建并重绑的新冻结上下文与工件哈希；任务 ID、冻结上下文 sha256、工件哈希、裁判决定与两份审核回执在本文件定稿时尚不存在，记录在候选目录外的证据包中 |
| 端到端真实业务任务交付 | not_run | 本任务不授权提交真实业务任务；健康探针通过不等于业务交付成功 |
| 冻结任务恢复（`frozen_task_recovery`） | not_run（未实际验证） | 本候选上未做生产解冻验证，只有离线套件覆盖；不得据离线覆盖写为已验证 |
| 经验晋升 | pending | 无发布回执。记忆候选尚未发布不等于系统故障，也不得为补证据自动批准 |
| 正式提升 | not_run | 本任务不授权切换活动发布；提升需用户显式授权，并复采 §0 基线证明生产未被提前写入 |

**策略晋升链与经验晋升链相互独立。** `strategies` 与 `promotion_gates` 属策略晋升链，`memory_candidates` 属经验晋升链；策略门槛通过不是经验发布证据，反之亦然。

## 10. 结论边界

- 有可复核原始证据并通过的：隔离运行时发布绑定（§1，含漂移 fail-closed 实测）、双后台真实健康探针（§2）、双后台工作阶段两阶段两后台均合格完成（§3）、后台产出的宿主逐条校验与两处事实修正（§4）、集成前离线套件（§5）、KnowLP 结构树构建与校验（§7）。
- 如实记录的既有缺陷：WAL `-shm` 并发竞争导致的间歇失败（§6），非本版引入，本候选不修复。
- 保持 pending / not_run 的：审核阶段、端到端业务交付、冻结任务恢复、经验晋升、正式提升（§9）。
- 本文件是 v1.4.4 候选的验收证据记录，不构成正式发布结论。是否已完成提升及真实验收以活动 release index、`RELEASE.json`、运行指纹与验收数据库记录为准。
