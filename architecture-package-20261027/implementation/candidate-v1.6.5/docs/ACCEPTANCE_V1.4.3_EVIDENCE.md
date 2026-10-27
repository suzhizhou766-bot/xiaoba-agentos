# v1.4.3 验收证据补充记录

本文件是**验收补充记录**，不是新的验收结论，也不重写已生效的不可变 `RELEASE.json`。它把 v1.4.3 提升时声称的验收项逐条回读到原始回执、日志与数据库记录，并区分「已证实 / 未执行 / 证据不足」。

## 绑定对象

| 项 | 值 | 来源 |
| --- | --- | --- |
| 活动发布 | `v1.4.3` | `.runtime/releases/index.json`（sha256 `575a5f708c00ac82ba886267cfb36a32c86517974ab506703e4fb4ba7f47eb05`） |
| 发布目录 | `/Users/suzhizhou/Documents/小八AgentOS/.runtime/releases/v1.4.3` | `verify-release` 回执 |
| `FILES.sha256` 指纹 | `023402d7a2cdb9c5cec98d6b4132547e7ac40e65b38d8d2727c8163113ae1d79` | `verify-release` 回执 + 独立重算 |
| 源码清单指纹 | `a60f38c0f51adb4a33efb7fcda797b8115a8930112919b03ccd6e12c52574ce9` | 发布内与工作区 `SOURCE_MANIFEST.json` 双向一致 |
| 期望版本 | `v1.4.3` | `config/agentos.json` `release.expected_version` |
| 数据库 | `.runtime/agentos.sqlite3`，`PRAGMA user_version = 7` | 只读查询 |
| 包内软件版本声明 | `version = "1.4.2"` ← **缺陷** | `.runtime/releases/v1.4.3/agentos/pyproject.toml` 第 7 行 |

所有时间均为 Asia/Shanghai 绝对时间。原始回执副本与逐文件 sha256 见候选目录外的 `evidence/v143-promotion-receipts/` 与 `evidence/receipt_manifest.json`。

## 一、已证实

**1. 正式发布绑定 fail-closed 通过。** `verify-release` 两次独立运行（2026-09-30T23:01:05 与 2026-09-30T23:10:20）输出字节相同，sha256 `dc2b9f0cea09be64262ef05c705136be0b5313874d6253defb443c2e6d863620`：`ok=true`、`skipped=false`、`acceptance_mode=false`、`current_release=v1.4.3`、`expected_version=v1.4.3`，发布内与工作区源码清单同为 `a60f38c0…`。

**2. 本地健康检查通过。** `xiaoba doctor --json`（2026-09-30T23:01:19，sha256 `9c29989b65a68ad973ac71430f543953e9ae4651ed60fd76d083a7706b6ba780`）：`release_binding.ok=true`、`database_schema 7/7`、`event_chain.ok=true`（723 事件，头哈希 `dd339eac…`）、`skill_registry 7/7 callable`、`knowledge_catalog 16 条（13 semantic_source / 2 machine_metadata / 1 runtime_binding）`、`memory_probe.ok=true`、`local_cli_routes` 两条路由 ok（`agy_review` → Antigravity `gemini-3.8-flash-low`；`dsh_review` → xiaomi `mimo-v2.6-pro`）、`gateway/dynamic_models/isolated_cli_routes` 无问题、`headroom skipped=true`（未触碰）。凭据字段存在但本记录不复制其值。

**3. 离线测试通过（正式包临时副本）。** 2026-09-30T19:39:23，日志 sha256 `5a67a9f9f773ab04b7b600d68ae028bd9b936bc87d7860066132b2fa2cf6296e`，退出码 0，结果行 `360 passed, 75 subtests passed in 3.01s`。运行对象是正式包的临时副本（`/tmp/xiaoba-accept-M9NvJM/agentos`），不是不可变包本体；解释器 cpython 3.11.16 + pytest 9.1.1。此结果只证明离线套件通过，**不等于**端到端交付成功。

**4. 双后台真实健康探针通过。** `xiaoba session-start`（2026-09-30T23:08:09，sha256 `c159ea25354d1f3daafaea92a1480c8ae88bf7715ba869fe32a198fbdeb486c7`）：`status=ready`、`doctor_ok=true`、`remote_inference_tested=true`、`session_mode=dual_agent`、`healthy_agents=[agy_review, dsh_review]`、`failed_agents=[]`；会话 `0d7cc132-c8e0-419f-be4f-b70314879c31`（created 2026-09-30T22:47:03+08:00，仍 active）。`agy_review` 探针 20401 ms、`dsh_review` 3910 ms，数据库 `agent_health` 两条记录时间戳与之一致（2026-09-30T23:08:09 / 23:07:53）。`memory_candidate_created=false`，未提交业务任务。健康是带时间的证据，不保证上游后续可用。

**5. 三项功能确实存在于活动正式包。**
- 晋升回执：`.runtime/releases/v1.4.3/agentos/src/xiaoba_agentos/cli.py` 含 `memory-receipt`（v1.4.2 同文件为 0 处，v1.4.3 为 3 处）。
- 经验状态机：v1.4.3 `store.py` 含 `MEMORY_CANDIDATE_TRANSITIONS`、`MEMORY_ACTIVE_RETRIEVAL_STATES`、`validate_memory_transition`（v1.4.2 均无）。
- 近邻误引用回归测试：v1.4.3 新增 `tests/test_knowledge_lock.py` 与 `tests/fixtures/`（v1.4.2 无）；`knowledge.py` 相应收紧检索门禁。

## 二、证据不足

**`end_to_end_multi_agent`（端到端双后台生产验收）。** 当前数据库里绑定源码清单 `a60f38c0…` 的生产任务只有一个：`501a8b81-12d1-4a49-870a-73bbaabf3982`（`status=completed`、`execution_mode=production`、created 2026-09-28T10:25:39+08:00、completed 2026-09-28T10:26:06+08:00、`execution_snapshot_hash=dd58bbe3…`、快照 `source_manifest_sha256=a60f38c0…`、结果首行匹配 `^本回答：小八 AgentOS 双 Agent 裁判`）。两个后台都 `completed`：

| Agent | 严格 `FINAL_ACCEPTANCE` 行 | 时延 |
| --- | --- | --- |
| `agy_review` | `FINAL_ACCEPTANCE: PASS`（1 行） | 14353 ms |
| `dsh_review` | `FINAL_ACCEPTANCE: FAIL`（1 行；全文另有 1 处非行首提及） | 25447 ms |

用正式包自带的 `scripts/build_release.py::validate_acceptance_database` 复核该任务，结论是**拒绝**：`every successful acceptance Agent must explicitly return one FINAL_ACCEPTANCE: PASS`。因此「双后台端到端生产验收通过」不成立，只能记为证据不足。

缺少的证据：在同一源码清单绑定下新提交一个生产验收任务，且 `agy_review` 与 `dsh_review` **各自**输出恰好一行 `FINAL_ACCEPTANCE: PASS`。本任务未获授权提交后台任务，故未补。裁判当时选中 `agy_review`，业务交付本身有效；不成立的是「两侧都 PASS」这项验收断言。

## 三、未执行

| 项 | 状态 | 说明 |
| --- | --- | --- |
| `priority_route_production_acceptance` | `not_run` | `RELEASE.json` 原样记录为 `not_run`，本记录不改写 |
| `knowlp_tree` | `not_run` | 未做 KnowLP 图重建与树校验 |
| `candidate_release_binding` | `pending` | 候选期字段，提升后由 `verify-release` 覆盖（见第一节第 1 项） |
| `frozen_task_recovery` | `pending`（未验证） | 离线套件覆盖冻结/恢复逻辑，但**没有**生产冻结任务的解冻验证；不得记为通过 |
| 记忆终审与发布 | 未发生 | `memory_candidates`：18 `pending_delivery_review`、1 `delivery_approved`、3 `delivery_rejected`、**0 `published`**；`memory_reviews` 只有 delivery 阶段（1 approve / 3 reject），无 final 阶段记录。这是等待人工两阶段审批，不是系统故障，也不能为补证据自动批准 |
| 策略晋升闭环 | 未发生 | `strategies`：v1 `active`（score 0.0，sample_count 0）、v2/v3 `candidate`（score 0.43922500295753986，sample_count 59，指标仍引用已禁用的 `claude_code`/`gpt_codex`）；`promotion_gates` 只有 `statistical_margin` passed×2 与 `judge_baseline` failed×1/passed×1，**从无 `regression` 记录**，故 `approve_strategy` 会 fail closed（`required promotion gate missing: regression`）；`strategy_audit` 0 行。注意这是**策略**晋升，与 `memory_candidates` 的**经验**晋升是两条独立链路 |

## 四、不可继承

上述证据全部绑定源码清单 `a60f38c0…`。v1.4.3 元数据同步候选修改了受管文件，源码清单必然不同，因此：

- 第一节的绑定、健康、探针与测试结果**不得**直接写成新候选的验收结论；
- 新候选必须自己跑完整离线测试并保留日志与哈希；
- 新候选的端到端双后台生产验收仍是未验证项，需要在它自己的绑定下重新取证。

## 五、复验命令

```sh
cd /Users/suzhizhou/Documents/小八AgentOS
S="$HOME/.agents/skills/xiaoba-agentos/scripts"
"$S/verify-release"                     # 期望 ok=true，两个源码清单同为 a60f38c0…
"$S/xiaoba" doctor --json               # 期望各分区 ok=true
shasum -a 256 .runtime/releases/index.json .runtime/releases/v1.4.3/FILES.sha256

# 复核端到端验收任务（只读）
/usr/bin/python3 - <<'PY'
import importlib.util, pathlib
root = pathlib.Path('.runtime/releases/v1.4.3/agentos')
spec = importlib.util.spec_from_file_location('br', root / 'scripts/build_release.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.validate_acceptance_database(
    pathlib.Path('.runtime/agentos.sqlite3'),
    '501a8b81-12d1-4a49-870a-73bbaabf3982', root))
PY
```
