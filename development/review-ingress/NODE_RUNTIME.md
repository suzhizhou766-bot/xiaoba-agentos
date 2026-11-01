# 自定义节点、对话补问与真实测试

2026-10-28，开发候选，承接用户明确的节点行为要求。

## 第八批：每步一次CLI调用（保留回执卡）

2026-10-29，开发候选。用户决定恢复“一次调用一次CLI”，回执卡功能保留：主入口把任务卡写成文件，每个步骤只启动一次`agy`，由它在节点固定工作区里用accept-edits权限自己写回执，主入口只读回执文件并校验。第七批的持续PTY会话保留为显式`agy_mode='interactive'`，默认不再是它。

- 新增`xiaoba_review/resource_agy_cli.py`的`AgyCliResource`，对外接口与`AgyInteractiveResource`一致（`supports_resource_plan`、`persistent_resource`、`supports_workday_receipts`、`execution_mode='native_cli_per_step'`、`test_budget_seconds=120`、`pending_action=None`，方法`resource_identity`、`terminal_snapshot`、`retire`、`close`、`test_resource`、`initialize_workday`、`run`、`run_task`）。调用方无需改动。
- `json_stdio_factory(source, workspace_root=None, agy_mode='cli')`：入口文件名是`agy`/`antigravity`时，默认返回CLI调用器，`agy_mode='interactive'`返回PTY调用器；其它入口保持原样。`agy_mode`只给测试和将来的显式配置用，不从网页或HTTP接收。
- 每步argv固定为`--input-format stream-json --output-format stream-json --model <节点配置原值> --mode accept-edits --sandbox --print-timeout <N>s`，健康探针与开工初始化N=90，审查与普通工作N=0（等本轮结束）。不加print/prompt类参数、跳过权限、额外目录，也不用会让`--mode`失效的开关；不查模型目录、不读默认模型。任务正文只放在卡文件里，argv不含任务文字或卡路径。
- stdin只写一行NDJSON `{"event":"user","message":{"content":…}}`后关闭。派发句沿用PTY写法，另加“只执行这张卡；不要检索旧任务记录、其它日期的工作日志或小八源码”，末尾附`XIAOBA_TASK_CARD=<卡路径>`。`cwd`是节点固定工作区，`start_new_session=True`以便按进程组回收；环境变量与PTY版同源但不设`TERM`。
- 每适配器一把锁串行；每次调用期间持有工作区`.session.lock`，结束后释放，同工作区占用仍报`workspace_session_busy`。顺序仍是`verify`→`begin`→`workday.prepare`。`work`类任务先查`previous_work`，命中直接返回并带`replayed=True`，不再起进程；原记录失败则报`work_step_failed`。
- stdout累计上限1MiB，超过即结束进程组并报`cli_output_limit`；stderr写入有界临时文件，原文不保存、不返回、不进证据。stdout新增可解析JSON行或回执文件大小变化都算进展；连续300秒无进展结束进程组并报`cli_no_progress`。健康探针与初始化总期限90秒，审查与工作无总期限。任何情况不自动重试、不换模型、不换后台。
- 完成判定只看`workday.read(ticket)`：合格回执且进程已退出即成功；合格回执但进程未退出，最多再等30秒后结束进程组，回执仍有效并记`process_terminated_after_receipt: true`；退出码0且`result`为SUCCESS却没有合格回执报`receipt_missing`；退出码非0或`result`非SUCCESS时先查原生目录信任字样，命中抛`NativeWorkspaceConsent`（状态记blocked，不自动确认、不加任何跳过参数），agy明确报模型名不被认可时报`model_not_recognized`（permanent，消息只用节点配置里的模型名），其余报`cli_failed`并只保留`failure_class`（transient/permanent）。`workday.read`抛出的`WorkspaceError`原样上抛。
- 证据字段为`provider='antigravity_cli'`、`model`（配置原值）、`route_source='native_cli_per_step'`、`model_source='node_configuration'`、`call_id`（每次调用）、`execution_mode`、`reply_source='daily_worklog'`、`worklog`；不声称已核验实际上游模型。旧回执里的`model_verification`字段仍能读，新回执一律不再写。身份协议为`agy-cli-worklog-v1`，`implementation_sha256`由`resource_agy_cli.py`、`agent_workspace.py`、`workday.py`三文件字节拼接计算，`instance`在构造时生成、同一服务进程内不变，服务重启后必须重新测试。
- 只读终端预览返回`supported`、`state`（`not_started`/`running`/`receipt_received`/`failed`/`closed`）、`session_id`（当前或最近一次`call_id`）、`turn`（已完成调用数），再合并`TerminalPreview().snapshot()`；正文是一行进度摘要（第几次调用、已收到几条流事件、回执字节数）加回执日志本轮新增部分的最后40行，统一经`set_screen()`脱敏限幅，不含stdout或stderr原文。
- `retire()`置停用标志：无任务在跑立即关闭，有任务在跑等它结算后再关；停用后新任务报`session_closed`。`close()`结束进程组（SIGTERM，2秒后SIGKILL）并释放工作区锁。
- 主入口核对时补了两处：结束进程组不再以agy主进程存活为前提，agy退出后留在本步进程组里的辅助进程也会被结束（先SIGTERM，2秒后SIGKILL；macOS对只剩未回收僵尸的组返回EPERM，先回收主进程，之后EPERM视为已无存活成员，主进程仍存活时被拒才报`session_cleanup_blocked`）；主进程退出后若辅助进程占着stdout，最多再等2秒即收尾，并在判定`receipt_missing`前补读一次回执。新增用例`test_helpers_left_in_the_call_group_are_ended_after_agy_exits`。
- 本批在用户主机上全量146项通过（原126项保留，`tests/test_agy_cli.py`20项）。实现方在其嵌套沙箱环境中看到的`test_review.HttpTest.test_http_bound_diff_is_idempotent_and_preserves_human_rejection`失败是该环境`sandbox-exec`返回`Operation not permitted`所致，用户主机上改动前后均通过。`test_workday.py`改用CLI夹具，`test_agy_interactive.py`与`test_terminal_preview.py`显式走`agy_mode='interactive'`继续覆盖PTY。**本批只做离线实现，没有调用真实AGY；夹具通过不等于真实AGY可用，真实测试需用户另行授权。**

## 当前实现：当日台账与Agent＋模型周评估（第七批）

本节取代下文第五、六批里依靠终端正文和JSON标记提取结果的历史做法；旧描述仅留作演进记录，不用于新派工。

- 每个节点继续复用同一PTY和固定命名工作区。本次会话用accept-edits而非plan，保留sandbox，不改持久设置、认证或用户模型；不自动确认原生权限。终端只显示进度和权限，任务结果来自文件。
- POST /api/agent-pool/work接受已健康节点上的普通授权工作卡，审查卡继续用原接口；同任务步骤重放只读回原结果。主入口落盘派工卡，Agent读取后执行授权工作，在固定工作区的YYYY-MM-DD-工作内容.md中追加完整过程和回执。回执最小元数据为任务ID、步骤码、状态及本轮完成标记，正文可用自然语言/Markdown，不强制JSON。文件完成标记、任务关联、普通文件及历史前缀校验通过后接收；不能将半写文件、旧回执或被改写的历史用于结算。
- 开工是显式的workday-start操作，仅由真实用户完整口令触发，绑定稳定消息ID和当天计划选定节点。先写独立健康回执并由宿主验证；通过后才派发WORKDAY-INIT步骤，由Agent新建或沿用当日Markdown。重复开工消息不再派活；失败停止，不自动重试。已安装xiaoba session-start仍属现行v1.6.4链路，未被本候选替换。
- 候选主入口可调用workday_session.py或POST /api/agent-pool/workday-start；后者的健康范围是选定后台链路和其工作区，不冒充生产doctor通过。调用者须先核对本机整体状态，正式接线需随版本发布。
- 审查结果收取和审查业务格式分别处理。完整正文已存文件而审查结论/证据字段不足时，保留原文并报告业务缺项；不把这个问题当连接失败，也不伪造有效审查或经验批准。
- 用户最新评分要求：不记录资源投入，不给永久能力总分。主入口实际核验后按每任务good＋1、not_good为0，按Agent实际来源＋配置模型＋自然周汇总；同任务多个步骤/重放不重复计分，更正使用修订号。旁列任务数、成功率和任务类型；健康检测不计业务分，原文不自动变成分数。
- workday-inputs提供可核对的任务事实；task-assess接受主入口核验后的任务评价；weekly-scores提供周数据。多个已授权、已开启且健康的节点可用同类任务周数据择优；无数据或并列仍沿用原补问。表与最新事件链、工作原证据不一致时拒绝排序。
- 本批最终全量124项通过，正常入口HTTP套件4项全部包含在全量内，通用工作任务路径归属检查另有2项复验；实际AGY写文件与真实多轮未执行，不能由夹具通过推定稳定可用。证据在第七批-工作日台账与文件回执-20261028/。

## 使用规则

1. 用户自定 Agent 名称、调用方式（CLI/桌面/Web）、调用入口与模型；可在原运行总览登记，也可通过与小八对话补齐。聊天宿主把用户已给资料传给 onboard，只问返回的缺项，不能猜填模型、入口或重新索要已知资料。
2. 确实需要后台时调用 prepare。缺哪个任务槽位就补哪个槽位；已有完整预设不再问名称、方式、入口和模型。只使用 direct 的普通任务不需要后台配置。槽位A/B兼容旧存储的dsh/peer，不绑定Agent品牌。
3. 新增默认关闭；用户在原运行总览开启并点击「测试运行」。测试会真实调用指定入口和模型，不是只检查路径或模拟输出。配置登记和打开开关都不能标记测试通过。
4. 同配置的有效测试、开启状态和受信调用器都具备，且任务已有明确选择或每个所需槽位只有一个适用节点时，dispatch直接运行，不再追问或每次重新探测。多个合格节点但无选择策略时只问选哪个，不自行发明B5排名。
5. 关闭或移除禁止直跑；入口内容、模型、配置或调用器身份变化使旧资格失效，需显式重测。开关关闭再开启不会重新询问未变资料。实际调用失败使资格失效，不自动重试、改模型或换后台。

## 调用接口

以下POST沿用现有同源、Host、CSRF与JSON门禁，本地受信聊天宿主也可调用对应Python方法；聊天文本不是HTTP授权。

| 路径 | 输入与结果 |
| --- | --- |
| /api/agent-pool/onboard | draft含已知name/interface/entry/model（可带稳定node_id及历史agent字段），kind为当前所需槽位；不全返回ask_user及逐项questions，齐全按同一稳定身份保存，默认关闭 |
| /api/agent-pool/prepare | required_kinds数组及可选selections；只读返回ask_user、blocked或run；不会自行测试 |
| /api/agent-pool/test | node_id、expected_revision、稳定test_id；只在启用后实际执行测试，结果持久化并写事件链；相同测试重放不再运行 |
| /api/agent-pool/dispatch | candidate_id、scope、evidence、review_type（必填：new_experience / revalidation / reuse_change）及稳定request_id，另有可选selections；缺项返回补问，已测适用节点直接调用；已有有效回执不重复运行成功侧 |

原create入口未传后台ID时也走prepare，不再猜默认后台。接口的ask_user.questions需由聊天宿主展示为主动问题，答案再传onboard。已知资料保留在调用方draft中，不把原始聊天存入第二大脑。

## 执行与可验证证据

测试绑定节点配置摘要、调用器身份摘要、可执行入口的SHA-256（JSON入口）、当前随机挑战及实际响应、受信入口回报的provider/route_source、节点配置的model原值和证据摘要，并与原生事件链中的同一节点事件核对。用户2026-11-01裁决：配置里的模型名就是实际使用的模型，所以测试只验证连通（能调用、能按挑战原文回答、能写回执），不核对入口回报的模型，也不因缺少模型证据判失败；测试通过的理由统一记为`challenge_completed_configured_node`。仅前端传passed、模型正文自称身份、复制另一节点事件或旧ID重放均不能获得资格。测试期间改开关不算有效PASS；中断测试不自动重探，只允许在原期限后显式启动新测试。

CLI JSON入口从stdin接收对象，protocol=xiaoba-resource-v1，operation=test时附resource_plan/challenge；入口调用配置的模型后从stdout返回相同protocol/challenge、output=challenge及provider/route_source。operation=review时接收冻结card，返回相同protocol、原card_digest、verdict/findings/evidence_refs和execution_evidence。测试证据里的model记节点配置原值；入口回报的模型不比对，回报不同或缺少都不判失败，模型身份也不取信模型正文自述。入口必须是用户明确指定的可执行绝对路径，不接受shell命令，不猜任意厂商CLI的参数。标准入口45秒有界等待，失败或超时停止。

除下述原生AGY接入外，普通CLI、桌面与Web仍需相应受信适配器，明确声明supports_resource_plan并实现resource_identity、test_resource及run；没有时返回adapter_required，不能假装测试通过。只是能打开应用或网页不算模型调用成功。

## 原生AGY持续交互（第五批，候选）

2026-10-28用户选择：小八复用一个交互会话，逐条发送并等待结果。原生可执行入口basename为agy或antigravity时使用专用PTY适配，不按用户自定义名称识别品牌，也不再把裸CLI当作xiaoba-resource-v1。未知自定义JSON入口及旧显式受信适配器优先级保持原样。

- 每个节点在当前服务中只保留一个原生交互进程，任务锁串行发送；没有print、stream-json输入或跳过权限参数，不会为每项任务另起一次模型会话。不调用models子命令。
- 以节点资料为调用依据，入口与model值原样传入；不查询模型目录、不读取默认模型、不识别欢迎页模型，也不猜High/Medium/Low。model仅为本会话参数，不写入持久设置。证据标记model_source=node_configuration，不把配置模型冒充实际上游模型证明；model_verification是旧回执字段，新回执不再写、旧回执仍能读。若原生CLI拒绝配置值，保留失败并由用户修改，不自动代填或换模型。
- 首次启动使用宿主明确指定父目录下、按用户节点名建立的固定工作区，原生“Do you trust the contents of this project?”不会自动确认。状态携带pending_action，在原节点内显示目录、权限范围和用户确认按钮；interactive-confirm沿用同源/Host/令牌门禁，绑定节点修订号、action_id与session_id并写事件链。确认只推进目录许可，不发送模型任务、不标记测试通过，更不批准任何经验。用户随后显式点击测试运行。关闭节点可取消。
- 只有所配置入口完成本次真实随机挑战、结构化结果边界和原生输入提示齐全，才记录调用测试通过，不额外要求模型身份核验。原始TUI、输入回显、工具标记、思考泄漏或无法解析的结果不能成为答案；终端原文只在内存中处理，不保存账号横幅或原始聊天。
- 测试有明确短期限，普通任务无总截止，连续300秒无可验证结果进展停止。限流、登录、超时及其它失败不自动重试、重连或换后台。串行复用不是防封保证，不能据此规避平台限制。
- 关闭/移除结束空闲会话，运行中的既有任务仍按原约定保留结算后关闭；服务退出会回收其所有会话。重开、进程退出、入口变化或服务重启后需显式重测，不能沿用已消失会话的通过证据。

验收范围必须区分：PTY子进程夹具、真实AGY原生启动、真实模型往返、同会话多轮。原生终端界面不是稳定结构化API；版本/界面漂移时失败关闭，不将夹具通过等同于真实AGY交互可用。2026-10-28用户已在节点亲自确认固定目录。其后一次真实挑战超时，接续时一次挑战返回正文但回执解析失败；未发第二轮，未获得运行资格。下述离线修复尚待新的显式真实测试。

## 保留边界

本批直接调用覆盖开发接入口的开放审查任务。旧Worker及所有外部聊天宿主的自动接线尚未迁移，不能宣称已替换线上队列。旧四字段来源的显式流程、旧候选、正式经验检索与原人工批准链保持兼容。技术测试通过不代表用户交付接受、经验初审或经验终审；任何Agent或脚本均不得代替用户批准。未迁移生产、修改认证或选择真实模型。


## 节点内只读终端预览（第六批，候选）

用户2026-10-28先确认调用方式为“脚本＋持续交互PTY”，再确认节点内按需查看、收起后后台静默继续。此处不是Computer Use，也不接管用户已打开的终端。当前专用实现覆盖AGY入口；其他CLI保留原协议，Web/桌面仍需各自受信调用器。

- 原运行总览点击同一节点，在抽屉顶部展开“实时终端预览 · 只读”。只读取该节点已有适配器的有界内存快照，不创建进程、不测试、不发送输入；没有会话时如实显示。
- POST /api/agent-pool/preview只接受node_id，使用现有同源、Host、页面令牌和JSON门禁；不调用投递outbox，不新增审核或输入接口。预览不作为执行成功、用户交付或经验批准的凭证。
- 展开时每秒获取文本快照；收起、关闭抽屉、切换节点或页面隐藏时中止预览请求。只停止观察，执行线程继续；重开读取原会话。向上滚动查看近期内容时不强制跳到底。
- 输出使用textContent呈现，UTF-8跨块解码，按光标移动和擦除重建当前80行×240列画面后替换预览，避免把TUI重绘碎片不断追加。先脱敏再限幅，可识别的敏感字段隐藏整个画面，邮箱替换为隐藏标记；预览上限仍为160行、每行1024字符。重启清空，不把原始终端转录落盘。它是当前画面的文本投影，不宣称完整复刻TUI颜色或无限历史滚动。
- 本轮同时修复真实AGY Plan输入提示附带说明时的就绪误判。未知提示、权限和目录信任仍不能自动确认；夹具能完成两轮，不代表真实AGY已完成两轮。

固定工作区父目录由本机启动参数--agent-workspace-root指定，不从网页接收；本次用户指定/Users/suzhizhou/Documents/私人助理/工作区。首次实际使用按节点名称建立子目录；节点改名时目录名固定为改名前的值（存进节点资料的workspace_name），材料不搬。归属判定只认node_id：标记里的source_ref和name是写入时的快照，节点改资料后对不上也照常可用，不要求用户迁移。不接管同名非空目录、不跟随子目录符号链接、不自动迁移或删除已有文件；冲突交用户决定，提示写明占用节点名和它是否已移除。

.session.lock阻止同一工作区同时启动两份交互进程。任务卡、合格结构化结果和执行回执存于任务记录子目录；记录.jsonl绑定顺序、前项哈希、任务卡和回执文件摘要，启动及每轮发送前校验，异常停止该节点派活。目录信任仍由用户在节点确认；信任不发送模型任务、不使测试合格。

归档的execution_completed只表示当前调用完成；user_delivery_accepted=false、experience_approved=false。用户实际交付、经验初审与终审继续使用既有流程，不能从本地文件或Agent自报升级。关闭/移除节点保留记录。解释器、账号认证、用户模型和生产指针均不更改。

本轮证据集中于第六批-节点终端预览-20261028；第五批旧失败、先前非交互探测和旧截图保留，不能当作当前真实交互PASS。

接续解析修复：仅完整独占一行（允许缩进）的本次随机开始/结束标记能界定JSON，动画行或输入回显中的相同字串不能成为边界。结束后仍须原生就绪提示出现且忙碌页脚清除；不放宽挑战匹配、结果卫生、工作区校验或人工批准门禁。真实动画后缀反例在旧代码离线复现失败，修复后夹具验证同一PTY两轮；这不计为真实AGY通过，也不触发自动重测。
