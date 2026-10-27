---
id: "experience-a9d6b6af-fb73-4841-8322-6ebe33219358"
type: "experience"
parent: "experiences"
status: "active"
authority: false
source_task_id: "a9d6b6af-fb73-4841-8322-6ebe33219358"
last_verified: "2026-10-13"
---

# 执行用户转交的 DeepSeek 任务卡：DeepSeek-分档瘦身与结构指纹（取代已作废的 DeepSeek-迁移输入时点与模板来源返修.md）。

## 任务

执行用户转交的 DeepSeek 任务卡：DeepSeek-分档瘦身与结构指纹（取代已作废的 DeepSeek-迁移输入时点与模板来源返修.md）。

任务卡全文：/Users/suzhizhou/Documents/私人助理/工作区/小八AgentOS-架构第2步乙/任务卡/DeepSeek-分档瘦身与结构指纹.md
工作位置：/Users/suzhizhou/Documents/私人助理/工作区/小八AgentOS-架构第2步乙/implementations/deepseek/（沿用，不另建副本、不建备份、不改其它实现目录、不改线上、不构正式包）
范围文档：同目录上级的 收口方案.md 与 公共约束与接口.md

要求摘要：
一、删除三类内容分析（迁移语句提取 _migration_signature 及辅助函数与规则 migration_signature 段；JSON 形状比对 _json_shape/_shape_of/_shape_matches/_shape_changed 与规则 contract_shape 段；_structural_diff 中受管根内移动改名与内部目录消失的特判）。删除后按普通新增/删除/修改走路径规则，对应用例一并删除，不得改写为断言新行为。
一之二、只保留源码布局，删除正式包布局支持（_build_package_side/_parse_files_manifest/_verify_inner_source_manifest、FILES.sha256 及 INTERFACE_MANIFEST/SECOND_BRAIN_MANIFEST 重算、规则包布局与 derived_hash_manifests 对应条目）与 _validate_rule_invariants；rules_path 为空时读 baseline_root 下规则文件；内置底线表留最小一份。SOURCE_MANIFEST.json 与 pyproject.toml 版本号继续按可重算验证不计档。
二、迁移档只认五样：store.py 的 DATABASE_SCHEMA_VERSION 两侧不同；docs/DEPLOYMENT.json 的 steps 规范化后不同；规则 contract_declarations 版本号任一不同（提取不到保守按迁移并写明原因）；受管顶层目录或根文件集合增减；tests/fixtures/schema_fingerprint.json 有改动。其余路径规则不变。
三、新建 tests/test_schema_fingerprint.py 与 tests/fixtures/schema_fingerprint.json：临时目录建全新库，读 sqlite_master 全部表/索引/触发器/视图定义与 PRAGMA user_version，确定性排序与空白归一后算 SHA-256 比对 fixtures；不一致时失败并说明必须同时升 DATABASE_SCHEMA_VERSION 与更新指纹文件；另加断言指纹文件 schema_version 等于代码常量。初始值在 schema 7 实算写入。不要改 tests/guardrail_marks.json，回执里写建议挂哪条底线。
四、release_policy.py 不超过 600 行。
验证：定向测试全过并报删除/保留/新增项数；指纹测试通过并做临时拷贝加列验证失败、更新指纹后通过且判迁移；真实样例基线 /Users/suzhizhou/Documents/小八AgentOS/.runtime/releases/v1.5.5/agentos（只读）七种场景逐项实测；scripts/create_manifest.py --verify 通过。
交回：在 回执/DeepSeek-发布分档.md 末尾追加一节（不改历史），含改动文件与 SHA-256、模块行数、验证实际命令与输出、未验证项、对卡片的疑问。

## 结果

本回答：小八 AgentOS 双 Agent 裁判

### 审查结论

经对任务卡《DeepSeek-分档瘦身与结构指纹》及相关接口约束的独立审查，本方案**判定完全可行，设计合理**。
核心改进是将原先复杂易错的“代码内容语义猜测”（如分析 SQL 文本语句、比对 JSON 数据形状、猜测文件重命名等）彻底剔除，替换为**“确定性的结构指纹”与“五项硬性规则”**。在满足系统安全底线的前提下，大幅精简代码体量（`release_policy.py` 可稳定缩减至 600 行以内），杜绝因格式变动导致的误判。

---

### 依据分析

#### 一、彻底剔除三类内容分析（瘦身主体）
1. **删除 SQL 迁移语句提取**：
   - 移除 `_migration_signature`（迁移签名提取函数）及其辅助解析逻辑，删除规则文件中的 `migration_signature` 配置段。
   - 依据：不再通过 AST（抽象语法树/代码语法解析器）或正则去猜测 SQL 脚本的意图，SQL 文件的改动退化为普通文件路径规则处理。
2. **删除 JSON 形状比对**：
   - 移除 `_json_shape`、`_shape_of`、`_shape_matches`、`_shape_changed` 以及规则文件中的 `contract_shape`（接口结构契约）配置段。
   - 依据：放弃对 JSON 数据深层键值结构的递归扫描，JSON 文件改动按普通配置/文档路径规则处理。
3. **删除受管根内移动与目录消失特判**：
   - 移除 `_structural_diff`（结构差异比对）中关于文件重命名、同根移动与子目录消失的特殊分支。
   - 依据：文件移动直接表现为“旧路径删除 + 新路径新增”，目录消失直接体现为文件批量移除，一律按通用路径规则计算。
4. **清理旧用例**：
   - 关联的历史测试用例直接彻底删除，**严禁改写为断言新行为**，避免残留失效断言。

#### 二、纯化布局支持与底线规则
1. **仅保留源码布局（Source Layout）**：
   - 移除 `_build_package_side`、`_parse_files_manifest`、`_verify_inner_source_manifest` 等关于发布包/安装包形态的复杂支持逻辑。
   - 移除 `FILES.sha256` 以及 `INTERFACE_MANIFEST`、`SECOND_BRAIN_MANIFEST` 的校验与重算。
   - 移除规则包布局及 `derived_hash_manifests`（派生哈希清单）相关映射。
2. **规则加载与底线精简**：
   - 移除 `_validate_rule_invariants`（规则不变量检查函数）。
   - 当 `rules_path` 为空时，统一回退读取 `baseline_root` 下的规则文件。
   - 代码内置的底线规则表仅保留最小一份基线数据。
   - `SOURCE_MANIFEST.json`（源码清单）与 `pyproject.toml`（项目配置文件）内的版本号变更，继续作为可重算验证项，不计入升级分档（不触发档位升级）。

#### 三、迁移档（Migration Level）判定的“五样”硬指标
仅在出现以下五种情况之一时，才触发“迁移档”（其他常规修改均走补丁档或功能档路径规则）：
1. **数据库版本变化**：`store.py` 中的 `DATABASE_SCHEMA_VERSION`（数据库结构版本号）两侧不一致；
2. **部署步骤变化**：`docs/DEPLOYMENT.json` 中的 `steps`（部署步骤清单）经文本空白规范化处理后内容不一致；
3. **接口声明版本变化**：规则中 `contract_declarations`（契约声明）定义的版本号存在任一不一致（若提取失败，采取保守策略判定为迁移档，并在日志中明确说明原因）；
4. **顶层目录/根文件集合变动**：受监管的顶层目录集合或根目录文件集合发生新增或删除；
5. **数据库结构指纹变动**：`tests/fixtures/schema_fingerprint.json`（数据库结构数字指纹文件）内容发生改动。

#### 四、新建结构指纹测试与参照文件
1. **测试逻辑 (`tests/test_schema_fingerprint.py`)**：
   - 在临时目录中初始化全新的 SQLite 数据库（当前基准为 Schema 7）；
   - 查询 `sqlite_master`（SQLite 记录所有数据表、视图和索引的系统元数据表），提取所有表（table）、索引（index）、视图（view）和触发器（trigger）的 DDL（数据定义语句），并读取 `PRAGMA user_version`（数据库用户版本号）；
   - 对 DDL 文本执行空白归一化（压缩多余空格与换行），按对象名称执行确定性排序后，拼接计算出整体的 SHA-256（安全哈希算法/内容数字指纹）；
   - 断言一：计算出的哈希值必须与 `tests/fixtures/schema_fingerprint.json` 中的指纹完全一致。若不一致，测试直接报错并明确提示：“必须同时递增 store.py 中的 DATABASE_SCHEMA_VERSION 并更新指纹文件”；
   - 断言二：指纹文件中的 `schema_version` 必须严格等于代码常量 `DATABASE_SCHEMA_VERSION`。
2. **底线挂载建议**：
   - 保持 `tests/guardrail_marks.json`（安全底线标记清单）不变；在交付回执中建议将该指纹测试挂载至现有的数据完整性与架构演进相关安全底线编号下。

#### 五、代码规模控制
- 移除大段解析器与复杂分支后，`release_policy.py` 逻辑将极度紧凑，行数预计收敛在 400~500 行，完全满足不超过 600 行的硬性上限。

---

### 潜在风险与防范

1. **SQLite 文本归一化差异风险**：
   - *风险*：不同操作系统或 SQLite 小版本生成的 DDL 语句在空白符或换行上可能微小不同，导致 SHA-256 计算结果漂移。
   - *防范*：在计算哈希前，先使用正则将所有连续空白替换为单个空格、去除首尾空格并统一字符大小写，确保跨环境哈希绝对确定。
2. **契约版本提取失败的过度保守风险**：
   - *风险*：若规则文件书写不规范，导致提取函数解析失败，会无条件升档为迁移档。
   - *防范*：在解析逻辑中提供友好的格式兼容并输出告警日志，回执中提示使用者保持声明格式规范。
3. **备份权限合规风险**：
   - 严格遵循用户执行权限规则：整个实施与验证过程**严禁自动新建任何备份文件、备份目录或备份快照**。

---

### 可验证步骤

实施阶段应按以下步骤进行闭环验证：

1. **测试套件清洗与定向测试**：
   - 删除旧的三类分析测试用例后，运行定向测试；
   - 统计并记录：删除用例数、保留用例数、新增用例数。
2. **数据库结构指纹自测与破坏性注入验证**：
   - 运行新建的 `pytest tests/test_schema_fingerprint.py`，确认初始 Schema 7 下测试 100% 通过；
   - **破坏性验证**：在临时副本中为某张数据表新增一列，重新运行指纹测试，确认测试如期报错拦截，并给出正确的引导提示；
   - **迁移联动验证**：更新测试副本中的指纹文件，确认测试恢复通过，且发布策略模块准确将其归类为“迁移档”。
3. **真实基线场景矩阵验证**：
   - 对照只读基线 `/Users/suzhizhou/Documents/小八AgentOS/.runtime/releases/v1.5.5/agentos`，实测以下 7 种场景并核对判档结果：
     1. 仅普通文档/配置变更（预期：非迁移）；
     2. 仅常规业务代码改动（预期：补丁档/功能档）；
     3. 修改 `DATABASE_SCHEMA_VERSION`（预期：精准命中迁移档）；
     4. 修改 `docs/DEPLOYMENT.json` 的 `steps`（预期：精准命中迁移档）；
     5. 修改规则中 `contract_declarations` 版本号（预期：精准命中迁移档）；
     6. 增减受管顶层目录或根文件（预期：精准命中迁移档）；
     7. 修改 `schema_fingerprint.json` 指纹文件（预期：精准命中迁移档）。
4. **代码规模与清单校验**：
   - 统计 `release_policy.py` 代码总行数，确认 `<= 600` 行；
   - 执行 `python scripts/create_manifest.py --verify`，确保清单校验完整通过。
5. **回执交付**：
   - 在 `回执/DeepSeek-发布分档.md` 末尾追加本次实施章节（不篡改历史内容），列明所有变动文件及其 SHA-256、模块行数、真实执行命令及输出日志。

---
AgentOS 裁判记录（确定性回退）
- agy_review: 0.560
- dsh_review: 0.482
- 判定依据：score（分项评分）

## 运行证据

- agy_review: completed, 73920 ms
- dsh_review: transient_error, 300222 ms
- dsh_review: completed, 36746 ms
