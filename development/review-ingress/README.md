# 小八 AgentOS：DSH＋开放第二方审查接入

2026-10-27，用户授权从架构设计进入云端实现并提交仓库。本目录是新增的可运行扩展，保留 v1.6.1 历史实现与 architecture-package-20261027 原包，不修改或解冻 v1.6.5 候选，也不切换本机活动 v1.6.4。

本次实现的是审查接入链：固定 DSH 角色、任意独立第二方、身份任务卡、统一回执接收、来源核对、持久投递、裁决材料收件箱及运行监看。此前“双方均无绑定”的设计已被用户最新确认取代。待验证经验独立索引、真实任务交付自动回流不是本次新增实现，继续保留其设计边界。

## 运行

Python 3.9+，业务模块只用标准库；浏览器端无需构建。默认核心源码来自本仓库的 `development/integrated-candidate/src`（当前候选核心），监看组件来自本仓库已接收的材料包，路径均可显式替换。

在仓库根目录执行：

```bash
python3 -B development/review-ingress/run.py --runtime /absolute/path/to/cloud-review-runtime --port 8800
```

访问**运行该命令的机器**上的 `http://127.0.0.1:8800/open-review`。这是本机监听服务，不是自动创建的公网域名；云端执行器的回环地址不等于用户电脑上的地址。无配置启动时创建独立空库，没有模拟审查者或预置经验，不调用任何模型。

接入已有 AgentOS 配置时：

```bash
python3 -B development/review-ingress/run.py \
  --core-source /absolute/path/to/bound-agentos/src \
  --monitor-source /absolute/path/to/小八监看 \
  --config /absolute/path/to/current-agentos-config.json --port 8800
```

配置路径只读使用，数据库按该配置定位，新增 ri_* 扩展表；不重写配置、认证、模型、上游或发布指针。运行中裁决后端的 backend-state.json 若声明不同配置，接入会停止。原监看页面新增导航，原“裁决中心”标签显示独立审查材料；原人工裁决继续走已安装后端，后端未运行时其批准功能仍不可用。新收件箱只负责材料，不伪报原生批准后端已上线。

## 使用流程

1. 在“登记审查来源”登记真实身份 ID、实际来源标识和来源依据；固定一方须登记为 DSH。登记是本地操作者的声明，不是密码学认证。不同别名应使用同一实际来源标识。独立性看“入口程序＋模型”：同一入口换模型算两个来源，改名称或别名不算。
2. 输入已有经验卡 ID、范围和带 SHA-256 的证据引用，选择 DSH 与任意第二方。原经验正文从受控 outbox 回读并校验，初审回执一起冻结。不会复制一张新经验卡。
3. 分别导出两张身份任务卡及回执模板。外部 Agent 可以由用户交接，也可以由后台 Agent 按卡完成；不要求外部 Agent 直连本机。任务卡包含相同冻结源材料，不含另一方的结论。
4. 导入结构化回执。公共入口始终先标为“身份待核对”，忽略自称已验证的字段；本地操作者记录实际交接或来源核对依据后才能计入。模板本身、哈希和自填 ID 均不证明真实身份。
5. 两份有效回执齐备，自动建立稳定裁决材料，经收件箱确认后显示“已接收”。PASS、FAIL、NEEDS_INFO、分歧和各方自带评分均保留；缺失评分不编造，不设置自动批准阈值。
6. 用户通过原经验卡的原入口裁决。这里不写 memory_reviews，不批准初审/终审、不发布经验、不增加正式复用计数，不替换方法的现行真实执行和验证门禁。

已经有本请求、同版本且经来源核对的 DSH 回执时，更换第二方会沿用该 DSH 卡和回执，不重跑。其它请求或来源不明的旧 PASS 不会自动搬入新卡。中途替换保留旧卡、回执、失败和原因；运行中的后台任务不能直接换人。旧失败不会从历史消失。

## 真实后台适配器

默认不注册、不调用后台。BackendRegistry 是可信宿主显式注册的 Python 接口，未知身份明确失败，不回落到 AGY/DSH/Qoder。任意后台实现声明 `kind`、`source_ref` 和 `run(card)`，注册后使用同一回执核验路径；不需要修改引擎。适配器代码须由部署者审查后加载，不允许通过网页传入模块路径执行代码。

原生 DSH 适配器复用现有 DshAdapter，沿用客户端模型选择、错误与无进展策略；拒绝 mock，真实调用前核对发布绑定与实际加载源码路径。先在界面登记来源，再以明确参数重启接入：

```bash
python3 -B development/review-ingress/run.py \
  --core-source /absolute/path/to/bound-agentos/src \
  --monitor-source /absolute/path/to/小八监看 \
  --config /absolute/path/to/current-agentos-config.json \
  --dsh-reviewer actual-registered-id --dsh-agent actual-configured-agent-name
```

没有隐式模型、账号、主备、备用或第二方组合。认证/配置缺失时停止，不换账号、不降级成模拟输出。后台失败写事件类型，不存凭据；模型返回未满足结构化契约时需重新指派或人工处理，不自动反复调用。

## 接口和存储

所有地址由既有 monitor_core HTTP 内核提供，写入继承本机 Host、同源 Origin、页面令牌与 JSON 门禁，单请求体上限沿用内核 1 MiB。

| 动作 | 地址 | 作用 |
|---|---|---|
| GET | /api/open-review/state | 两方状态、历史、投递、材料收件箱 |
| POST | /api/open-review/sources | 本地登记来源（身份不原地改写） |
| POST | /api/open-review/requests | 原卡、DSH、第二方、范围、证据 → 两张身份卡 |
| POST | /api/open-review/receipts | 导入回执，不自动信任来源 |
| POST | /api/open-review/attest | 本地核对具体回执摘要及来源依据 |
| POST | /api/open-review/replace | 带原因替换第二方；保留历史 |
| POST | /api/open-review/run | 执行明确登记的后台适配器 |
| POST | /api/open-review/retry | 只重试材料投递，不重跑模型 |

ri_sources、ri_requests、ri_assignments、ri_receipts、ri_outbox、ri_inbox 为同一原生数据库的新增表，ri_version 记录本扩展结构版本 1。原生 SQLite schema 版本不冒充升级，原审批表结构不改。应用前应按正常发布流程评估这种新增表迁移；没有执行本机生产切换。

原卡正文或初审变化使依赖回执失效，当前收件箱材料随之取消有效标记。新无效/待确认回执会使旧成功不能继续充当当前结果。相同回执 ID＋内容幂等，同 ID 不同内容冲突。请求 ID 同样做内容一致性核对。并发通过 BEGIN IMMEDIATE 序列化，收件箱接收与 ACK 原子写入；中断重试不会复制材料。事件写入原生哈希链，链坏停止本模块写入。

材料收件箱与原生“用户裁决动作”是不同职责：收件箱接受表示材料到齐，并不代表验证通过。`--inbox-offline` 可验证待投递状态；恢复后周期重试同一材料。外部证据引用的 SHA-256 绑定本次输入，不能替代外部执行事实或认证；引用所指外部产物发生变化时应重新发起审查，当前模块只自动检查原卡文件及初审状态。

## 验证与已知限制

```bash
cd development/review-ingress
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v
node --check xiaoba_review/page.js
node --check xiaoba_review/native-center.js
# 可选：需已安装 playwright 和 Chromium，仅使用 TEST 夹具
PYTHONDONTWRITEBYTECODE=1 python3 -B tests/browser_smoke.py
```

本次完成 20 项单元/HTTP 集成验证，以及真实 Chromium 界面操作验证。完整记录见 TEST-RESULTS.md。测试中的 TEST-DSH 是明确标记的夹具，不是实际 DSH 审查证据；未把它写入正式经验。

真实 DSH 模型调用尚未验证：云端未安装用户实际客户端/认证，模块不会复制或伪造。本次也没有做本机生产发版或切换。原运行 Worker 和旧具名协作流程仍保持原状；新接入口不自动解冻旧任务，也没有宣称完成整个 v1.6.5 的发布。宿主进程异常退出后未完成的后台指派保留 running 标记，不自动接管或重跑；需操作者核查实际执行后另行处理。

## 架构与升级发布入口

同一运行器现已接入 [architecture-center](../architecture-center/README.md)。监看页面新增“架构与升级发布”标签，编辑先进入独立待发布工作区，正式切换保留原生门禁。未配置本机执行器时发布按钮不可用；本仓库的启动方式不等于已安装或切换用户本机。

说明：用户的最新规范（任务前直接记录资源、运行不重复核实模型，2026-11-01）已在本文描述的现行流程中生效——AGY 的新执行回执不再写”模型核对结果”这类字段，改动前写入的旧回执仍能读取。上面第 1 条的来源登记继续按”入口程序＋模型”判独立性，这一规则不因去掉模型核对而改变。
