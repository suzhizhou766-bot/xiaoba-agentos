/* Editable topology source: facts, node positions and directed connections.
   This is a design viewer; it does not call agents or change production state. */
(() => {
  const facts = {};
  function f(id,title,summary,status='已定稿 · 待实现',kind='planned',rules=[],links=[]) {
    facts[id]={id,title,summary,status,kind,rules,links};
  }
  f('user','用户','提供目标、资源事实与授权；验收成品并作人工裁决。','体系角色','current',[
    '已有的 Agent、端形态、入口和模型信息直接采用，不重复询问。',
    '具体任务如何择优由 AgentOS 按评分、索引与历史记录完成。']);
  f('host','主入口 Agent','读取任务计划，使用对应工具实际调用并写入结果。','体系角色','current',[
    '可由 Codex、Claude 或其它接入者承担。',
    '桌面端用 computer use，Web 端用 Chrome 插件，CLI 端用终端命令。',
    '调用异常立即暂停并汇报，不自行修复、重试或切换组合。']);
  f('resource','可用资源池','是什么 Agent · 桌面 / Web / CLI · 入口路径 · 模型。','最新规范 · 待调整','planned',[
    '这些是主入口如何调用所需的资源信息，不是额外的身份认证流程。',
    '用户已给出的信息直接记录；只在缺少必要字段时补问。',
    '日常不逐任务核实模型或重复探测；真实开工口令可检测当天选定链路并初始化工作日志。'],['review']);
  f('choose','按历史记录自动择优','匹配任务类型，选择合适经验、第二开放 Agent 与模型。',undefined,undefined,[
    '先按授权、能力、适用条件和可用性筛选，再依据同类任务表现排序。',
    '依据任务质量、验证结果、耗时和返工记录；不采集资源投入。',
    '规则、评分和长期记忆支持快速选择，不表示 AgentOS 自己进行模型式思考。'],['scoring']);
  f('bound','任务 ID 与调用计划','绑定所选经验、Agent、端形态、入口、模型与任务目标。',undefined,undefined,[
    '计划是调用和结果回流的共同上下文，回执不重复登记模型身份。',
    '用户改变参与者或模型时保留本次计划修订及旧执行历史。']);
  f('entry','统一 xiaoba 入口','既有发布、配置与源码绑定机制。','既有核心机制','current',[
    '既有发布信任链与日常对外部 Agent 的重复模型确认是不同职责。',
    '本图不表示已完成正式构包或本机生产切换。']);
  f('route','消息路由','宿主直接处理 / 任务队列 / 人工审核与必要追问。','既有核心机制','current');
  f('direct','宿主直接处理','日常任务按既定授权直接执行，不强制进入 Worker。','既有机制','current');
  f('queue','任务队列','需要后台编排的任务进入队列。','既有核心机制','current');
  f('worker','Worker 与编排器','维护执行权、租约、预算和任务回执。','既有核心机制','current',[
    '冻结候选原执行链仍有具名驱动和旧协作名单检查，尚未整体改造。',
    '新审查接入口不自动解冻旧任务。']);
  f('clarify','人工审核 / 必要追问','仅补齐缺失事实或新的授权，不把逐任务择优交回用户。','最新规范','current');
  f('invoke','按已知入口正式调用','主入口按任务计划调用桌面、Web 或 CLI。','最新规范 · 待调整','planned',[
    '用户给出的资源信息直接使用，不反向确认模型或设置。',
    '主入口缺少所需工具或调用失败时，暂停并报告具体缺口。'],['review']);
  f('work','实际执行与宿主集成','完成授权任务、集成贡献、形成可检查的实际产物。','既有任务职责','current');
  f('qc','技术检查与输出验收','必要断言、结果检查、B19 输出卫生及确定性回答裁判。','既有核心机制','current',[
    '词面、结构等回答排名分不等于任务综合分或经验有效性评分。',
    '模型建议不代表宿主已经实际完成改动。']);
  f('result','任务结果与执行回执','保存产物、执行结果、失败原因及审计关联。','既有机制 + 新规范','current');
  f('pause','异常立即暂停并汇报','说明任务、所选入口、失败步骤、原始错误与已完成内容。','最新运行规范','danger',[
    '不自动重试，不自行修复，不更换 Agent、模型、账号或上游。',
    '调用中断和经验自身验证失败分开记录。',
    '等待用户处理或明确下一步指示。']);
  f('delivery','用户说“交付”','确认本次成品验收，触发交付与经验分流。','已有口令 · 增补待接线','current',[
    '口令应绑定明确任务及成品版本。引用或讨论该口令不触发验收。',
    '成品验收不等于经验初审或终审批准。'],['experience']);
  f('archive','交付归档','交付记录、踩坑、打法与偏好。','既有工作流程','current');
  f('new','本次新经验候选','从新候选开始，等待用户初审。','既有候选流程','current');
  f('initial','用户初审','用户批准当前候选版本后，进入核验流程。','既有人工门禁','current',[
    '新经验不会因成品已验收而跳过初审。',
    '初审通过不等于核验通过或正式经验。'],['validation']);
  f('initialPassed','初审通过','进入第一轮待隔离验证，同时纳入待验证资料范围。','已定稿 · 待运行实现','planned',[
    '第一轮通过即可待终审，不需要强制再跑第二轮。',
    '仅第一轮未通过，才进入第二轮待重新验证。'],['validation']);
  f('pendingIndex','待验证经验独立索引','定位初审有效、尚待验证的方法候选。',undefined,undefined,[
    '与正式长期记忆检索范围分开。',
    '检索命中后回读原候选；命中或阅读不等于实际应用。']);
  f('retrieve','后续相似真实任务检索','基于任务类型、问题与适用条件寻找经验。',undefined,undefined,[
    '正式经验和待验证候选明确区分。',
    '结合索引和历史评分快速择优。'],['scoring']);
  f('apply','核对条件并实际应用','记录使用哪条经验、用在哪里、采用哪些步骤。',undefined,undefined,[
    '不得把仅检索、仅阅读记为实际采用。',
    '实际应用结果随当前任务 ID、候选版本和成品版本记录。']);
  f('evidence','执行证据与任务评分','记录本次任务效果和综合表现，保留分项依据。',undefined,undefined,[
    '任务综合评分用于经验和参与组合的后续选择。',
    '按用户最新规则记录每任务＋1或0，按Agent＋配置模型汇总周表现；不记录资源投入。'],['scoring']);
  f('laterDelivery','后续任务交付验收','用户确认这次实际应用经验后的成品。','已确认业务规则','current');
  f('used','本次实际应用的旧候选','关联原卡，保留应用位置与本次执行证据。',undefined,undefined);
  f('feedback','材料自动回写原卡','将验证材料、任务评分和实际应用记录回原候选。',undefined,undefined,[
    '不重复建立同一条旧经验的新卡。',
    '只为当前核验轮次补充材料，不重置两轮上限。']);
  f('roundInput','关联原卡当前轮次','回写材料送当前第一轮或第二轮核验。','已定稿 · 待实现','planned',[
    '原卡回写不能直接进入待终审。',
    '只有有效核验通过路径可以进入待终审。'],['validation']);
  f('wait1','待隔离验证 · 第一轮','初审通过后的首轮核验状态。',undefined,undefined);
  f('verificationSummary','两轮经验核验','首轮通过，或第二轮通过并完成差异核对；第二轮失败退役。',undefined,undefined,[
    '这是概要节点，点击下方模块入口查看完整通过与失败分支。',
    '回执齐备、材料回写均不能直接成为核验通过。'],['validation']);
  f('run1','执行第一轮隔离验证','隔离执行、必要检查与独立审查。',undefined,undefined,[
    '本轮输出要绑定原卡版本、任务和本次执行证据。',
    '调用异常保持暂停原因，不伪造成经验通过或失败结论。']);
  f('decision1','第一轮结果','通过 → 自动待终审；未通过 → 保留证据，进入第二轮。','核验分支','current');
  f('fail1','保留首轮失败证据','保存失败原因、执行结果和任务评分。',undefined,'danger');
  f('wait2','待重新验证 · 第二轮','仅首轮未通过后进入，不新增第三轮。',undefined,undefined);
  f('run2','执行第二轮重新验证','围绕首轮问题采取措施，重新执行并核验。',undefined,undefined);
  f('decision2','第二轮结果','通过 → 核对差异；未通过 → 经验直接退役。','核验分支','current');
  f('compare','核对两轮差异与原因','重点标明“第二轮验证通过”。',undefined,'success',[
    '首次失败原因与对应证据。',
    '第二轮采取的措施、输入 / 环境 / 步骤差异。',
    '两轮结果与评分对照，以及仍存在的适用限制。']);
  f('finalQueue','自动进入待终审','只由第一轮通过，或第二轮通过并完成差异核对进入。',undefined,'success',[
    '材料到齐不等于核验通过。',
    '进入待终审不写用户批准，不自动发布经验。']);
  f('finalHuman','裁决中心 · 用户人工终审','由用户对明确候选与绑定证据作出最终裁决。','既有人工作用','current');
  f('publish','敏感检查与索引复验','终审通过后，完成现行发布检查。','既有发布门禁','current');
  f('formal','正式经验与长期记忆索引','审核发布后的有效经验供后续任务检索复用。','既有机制','success',[
    '第二大脑保存可复用的经验原文，索引负责快速定位。',
    '复用继续记录结果和失败，不能把历史通过理解为永远有效。']);
  f('retire','经验直接退役','第二轮未通过后停止检索与复用。',undefined,'danger',[
    '保留两轮记录与退役原因供用户查看。',
    '不进入第三轮，不自动删除。']);
  f('retireShow','监看中心展示退役记录','展示两轮执行、核验结论、差异及退役原因。',undefined,'danger');
  f('manualDelete','用户确认后手动删除','删除由用户确认并手动执行，系统不自动删除。','用户操作','danger');
  f('reviewPlan','读取任务 ID 绑定计划','沿用已知的 DSH、第二方、端形态、入口和模型。','新规范 · 接入口需调整','planned',[
    '不再次登记来源，不让模型为用户信息自证。',
    '本轮审查任务关联原卡、轮次与材料版本。']);
  f('callHost','主入口按端形态调用','桌面：computer use；Web：Chrome 插件；CLI：终端命令。','最新运行规范','planned',[
    '按用户给定的确切路径 / 地址直接执行本次正式调用。',
    '不核实模型，不发探针，不额外试调用。']);
  f('dsh','固定 DSH 审查','固定审查角色，读取本次任务计划并执行当轮任务。','接入基础已开发 · 待调整','current');
  f('peer','第二开放 Agent','可选任意已知可调用的外部或后台 Agent。','接入基础已开发 · 待调整','current',[
    '参与选择由资源池、任务历史与评分策略支持。',
    '不固定第二方、不内置默认模型或主备组合。']);
  f('receipts','按任务 ID 接收回执','归集实际执行结果、产物引用、结论、理由与分歧。','接入基础已开发 · 待调整','current',[
    '回执引用任务前绑定的信息，不再重复确认 Agent 或模型。',
    '同回执 ID 同内容幂等，不同内容需保留冲突记录。']);
  f('reviewResult','当轮审查结果归集','作为两轮核验及本条任务综合评分的输入。','既有归集基础 + 新规范','current',[
    '本部分负责调用和归集，不另建一套“真实来源确认”流程。',
    '两份回执齐备不代表经验核验通过。'],['validation','scoring']);
  f('score','每条任务综合评分','每项任务经主入口核验后完成得好＋1，其余0，按Agent＋配置模型按周汇总。',undefined,undefined,[
    '评分对象是整条任务，记录本次采用的经验、第二 Agent 与模型组合。',
    '不记录资源投入，不将Agent自述或健康探针直接换算为业务分。',
    '本地候选周账本按Asia/Shanghai自然周，保留累计分、任务数、成功率和任务类型；未切生产。']);
  f('history','同类任务组合表现','积累同类任务使用哪些经验与参与组合的效果。',undefined,undefined,[
    '优缺点来自实际任务样本，保留任务类型和适用范围。',
    '形成Agent＋模型的周阶段评估，不累积永久能力总分。']);
  f('memory','索引＋第二大脑长期记忆','用结构化任务记录与审核后的经验支持快速检索。',undefined,undefined,[
    '完整执行事实和评分保存在数据库。',
    '可复用经验按审核流程进入第二大脑，索引定位后回读。',
    '不恢复已退役的旧策略进化运行时。']);
  f('filter','按条件与授权筛选','筛除不适用或不可用的经验与资源。',undefined,undefined);
  f('rank','规则排序与选择','依据同类型任务表现选择更合适的经验、Agent 和模型。',undefined,undefined,[
    '不是让 AgentOS 自己进行模型式思考，也不是让用户逐次挑选。',
    '历史依据越充分，同类任务选择越快、更有针对性。']);
  f('runtime','日常运行：读取 · 调用 · 写入','读取任务和经验，调用资源，写入任务产物及记录。','用户最新规范','current',[
    '配置、规则与系统实现的修改放在明确的更新迭代任务中。',
    '日常异常不触发自动修改、自动修复或自动切换。']);
  f('tools','共享业务能力','项目、导演、资产、审美、视频、音频与 REA 工具。','已有能力目录','current',[
    'OriginCut、Remotion、AE、达芬奇、BGM / SFX 等按具体任务调用。',
    '登记或源码存在不代表当前全部服务已在线。']);
  f('data','数据与审计底座','SQLite、候选正文、回执、事件哈希链及不可变发布。','既有底座 + 接入扩展','current',[
    '既有数据库 schema 为 7，新增接入口有独立 ri_* 扩展表。',
    '审计事实、任务评分、候选正文与正式经验各有职责。']);
  f('guards','治理与发布边界','B09 配置一致 · B19 输出合格 · B20 原子与幂等结算。','既有约束','current',[
    '软件发布和经验批准分别处理。',
    '最新图是设计说明，不表示已完成本机生产切换。']);
  f('monitor','运行监看贯穿全过程','查看任务、计划、回执、轮次、差异、待终审及退役原因。','已有监看 + 待实现接线','current');
  f('upgradeCenter','架构与升级发布中心','监看中心统一入口：查看拓扑，按模块提出修改并接续升级。','开发实现 · 待本机接入','current',['日常运行保持读取、调用和写入结果；规范与源码修改进入更新迭代工作区。','点击节点编辑所属模块规范，保存不直接改动活动源码。'],['upgrade']);
  f('moduleDraft','编辑模块规范','选择模块，填写目标、基线、目标版本和规范提案。','开发实现','current');
  f('pendingFiles','待发布文件夹','按改动 ID 保存候选文件、差异、清单、权限和接续说明。','开发实现','current',['数据库为状态权威，目录用于接续；手工改清单或文件不能继承旧验证。']);
  f('agentHandoff','任意授权 Agent 接续','认领执行权、候选实施、记录检查点并交接。','开发实现','current',['只写本模块允许的候选路径；通过修订号与执行权防止并发覆盖。','Agent 名称只是审计标签，不会自动授予机器权限。']);
  f('acceptedPackage','验证、审查与正式构包','接续 Agent 完成实际功能验证和独立审查，构建受审包。','沿用原生门禁','current',['候选语法检查不能替代功能测试、独立审查或正式包核验。','本中心不为任何经验作初审或终审批准。']);
  f('releasePreflight','本机正式包预检','核对受审包、源码指纹、现场配置和预检时效。','桥接已实现 · 本机待接入','current',['执行器由本机明确配置固定路径和指纹，不接收网页提供的任意命令。','没有正式包、执行器或有效预检，正式发版不可用。']);
  f('releaseButton','用户点击正式发版','展示具体版本、文件改动与检查结果，用户确认后执行原生切换。','桥接已实现 · 未进行生产切换','current',['保留原生切换前校验和切换后回读；只有实际回读通过才记为已发布。','发布异常暂停，不自动重试或更换环境。']);
  f('versionRecord','版本源码与实际发布记录','保存基线、目标、候选源码哈希、包摘要、审计与实际回读回执。','开发实现','current',['源文件保留于对应待发布任务与受审包；本中心不自动创建备份。','在线架构页为只读；实际编辑和发版需接入本机监看服务。']);
  function n(id,x,y,w=238,h=82){return {id,x,y,w,h};}
  function e(from,to,label='',tone='normal',via=null){return {from,to,label,tone,via};}
  function g(title,x,y,w,h){return {title,x,y,w,h};}
  const views=[
    {id:'overview',title:'整体拓扑',subtitle:'从任务前置绑定，到执行、经验核验与历史反馈。点击任意节点查看规则。',w:1320,h:1000,
     groups:[g('01 · 接入与任务择优',20,25,405,410),g('02 · 执行与交付',455,25,405,410),g('03 · 经验与核验',890,25,405,410),g('04 · 审查调用与回执归集',20,480,405,420),g('05 · 评分、索引与长期记忆',455,480,405,420),g('06 · 运行底座与治理',890,480,405,420)],
     nodes:[n('resource',42,87,360),n('choose',42,210,360),n('bound',42,333,360),n('invoke',477,87,360),n('work',477,210,360),n('delivery',477,333,360),n('initial',912,87,360),n('verificationSummary',912,210,360),n('finalQueue',912,333,360),n('reviewPlan',42,545,360),n('receipts',42,665,360),n('reviewResult',42,785,360),n('score',477,545,360),n('history',477,665,360),n('memory',477,785,360),n('runtime',912,545,360),n('data',912,665,360),n('upgradeCenter',912,785,360)],
     edges:[e('resource','choose'),e('choose','bound'),e('bound','invoke','任务计划','normal',[[437,374],[437,128]]),e('invoke','work'),e('work','delivery'),e('delivery','initial','新经验初审','normal',[[880,374],[880,128]]),e('initial','verificationSummary'),e('verificationSummary','finalQueue','核验通过','pass'),e('reviewPlan','receipts'),e('receipts','reviewResult'),e('reviewResult','score','任务评分输入','feedback'),e('score','history'),e('history','memory'),e('runtime','data'),e('data','upgradeCenter')],note:'整体视图中的核验链为概要。完整的首轮失败、第二轮差异与退役分支见“两轮核验”。'},
    {id:'execution',title:'一、接入与任务执行',subtitle:'系统按历史择优，主入口实际调用；日常运行只读取、调用、写入。',w:1230,h:1000,
     groups:[g('任务计划',20,25,1185,210),g('路由与执行',20,260,1185,590)],
     nodes:[n('user',45,95),n('host',350,95),n('entry',655,95),n('route',655,300),n('direct',45,450),n('queue',350,450),n('clarify',655,450),n('worker',350,590),n('work',45,735),n('qc',350,735),n('result',655,735),n('pause',955,450,220,100)],
     edges:[e('user','host'),e('host','entry'),e('entry','route'),e('route','direct'),e('route','queue'),e('route','clarify'),e('queue','worker'),e('direct','work'),e('worker','work'),e('work','qc'),e('qc','result'),e('route','pause','调用异常','fail')],note:'直接处理不强制经过 Worker；原候选执行链和新增审查接入口分开部署。'},
    {id:'experience',title:'二、交付与经验回流',subtitle:'新经验先初审；旧经验实际应用后回原卡。回写材料不直接进入待终审。',w:1250,h:1020,
     groups:[g('本次任务交付分流',20,25,1210,530),g('相似真实任务应用与回流',20,580,1210,405)],
     nodes:[n('delivery',485,85),n('archive',55,260),n('new',485,260),n('used',915,260),n('initial',485,420),n('feedback',915,420),n('roundInput',915,610),n('pendingIndex',485,610),n('formal',55,420),n('retrieve',55,610),n('apply',55,790),n('evidence',485,790),n('laterDelivery',915,790)],
     edges:[e('delivery','archive'),e('delivery','new'),e('delivery','used'),e('new','initial'),e('initial','pendingIndex','初审通过'),e('used','feedback'),e('feedback','roundInput','沿用当前轮次'),e('pendingIndex','retrieve','待验证资料','feedback'),e('formal','retrieve','正式经验','feedback'),e('retrieve','apply'),e('apply','evidence'),e('evidence','laterDelivery'),e('laterDelivery','used','关联原卡','feedback',[[1210,831],[1210,301]])],note:'待验证索引与交付自动回写尚待实现；实际应用和任务综合评分需关联任务 ID。'},
    {id:'validation',title:'三、两轮经验核验',subtitle:'首轮通过即可待终审；第二轮通过必须突出差异；第二轮失败直接退役，无第三轮。',w:1280,h:1170,
     groups:[g('初审后的两轮状态机 · 已定稿，待运行实现',20,25,1240,1120)],
     nodes:[n('initialPassed',45,100),n('roundInput',405,100),n('wait1',45,240),n('run1',45,370),n('decision1',45,510),n('fail1',405,510),n('wait2',405,640),n('run2',405,770),n('decision2',405,900),n('compare',765,900,245,105),n('finalQueue',765,510,245),n('finalHuman',765,640,245),n('publish',1040,640,195),n('formal',1040,770,195),n('retire',45,900),n('retireShow',45,1040),n('manualDelete',405,1040)],
     edges:[e('initialPassed','wait1'),e('wait1','run1'),e('run1','decision1'),e('decision1','finalQueue','第一轮通过','pass',[[305,551],[305,475],[887,475]]),e('decision1','fail1','未通过','fail'),e('fail1','wait2'),e('wait2','run2'),e('run2','decision2'),e('decision2','compare','通过','pass'),e('compare','finalQueue','完成差异核对','pass',[[1025,952],[1025,551]]),e('decision2','retire','第二轮未通过','fail'),e('retire','retireShow','退役','fail'),e('retireShow','manualDelete','用户确认后手动执行','fail'),e('finalQueue','finalHuman'),e('finalHuman','publish','终审通过'),e('publish','formal'),e('roundInput','run1','仅当前为第一轮','feedback',[[330,141],[330,411]]),e('roundInput','run2','仅当前为第二轮','feedback',[[690,141],[690,811]])],note:'工具、认证或网络异常应暂停汇报；不得伪造成经验核验结论。只有两条通过路径能进入待终审。'},
    {id:'review',title:'四、审查调用与回执归集',subtitle:'使用任务前已绑定的信息调用，不再次确认来源、核实模型或发探针。',w:1250,h:800,
     groups:[g('任务信息与实际调用',20,25,1210,290),g('两方当轮审查与回执归集',20,340,1210,420)],
     nodes:[n('bound',45,95),n('reviewPlan',350,95),n('callHost',655,95),n('pause',960,95,230,100),n('dsh',350,405),n('peer',655,405),n('receipts',350,590),n('reviewResult',655,590),n('score',960,590,230)],
     edges:[e('bound','reviewPlan'),e('reviewPlan','callHost'),e('callHost','pause','调用异常','fail'),e('callHost','dsh','本轮任务'),e('callHost','peer','本轮任务'),e('dsh','receipts'),e('peer','receipts'),e('receipts','reviewResult'),e('reviewResult','score','综合评分输入','feedback')],note:'旧审查接入口已有开发基础，仍需按最新“直接采用用户信息、按入口调用”规范调整。'},
    {id:'scoring',title:'五、任务综合评分与自动择优',subtitle:'“越来越聪明”来自评分、索引和长期记忆的积累，由规则快速选择，不要求用户逐次挑选。',w:1280,h:890,
     groups:[g('任务前：筛选与择优',20,25,1240,260),g('任务后：综合评分、记忆与反馈',20,325,1240,410)],
     nodes:[n('resource',45,100),n('retrieve',350,100),n('filter',655,100),n('rank',960,100,235),n('bound',960,390,235),n('evidence',655,390),n('score',350,390),n('history',45,390),n('memory',45,590),n('choose',655,590)],
     edges:[e('resource','retrieve'),e('retrieve','filter'),e('filter','rank'),e('rank','bound','选择后绑定'),e('bound','evidence','执行完成'),e('evidence','score'),e('score','history'),e('history','memory'),e('memory','choose','同类任务历史','feedback'),e('choose','rank','下一次择优','feedback',[[1230,631],[1230,141]])],note:'用户最新规则：业务任务完成得好＋1，其余0；不记录资源投入；按Agent＋模型按周评价。候选实现和正式发布须分别验收。'},
    {id:'foundation',title:'六、共享底座与运行边界',subtitle:'业务工具、数据、治理与监看共同支撑；变更放在明确的更新迭代任务中。',w:1200,h:650,
     groups:[g('既有底座与最新运行规范',20,25,1155,585)],
     nodes:[n('runtime',55,100,310,95),n('tools',445,100,310,95),n('data',835,100,310,95),n('guards',250,340,310,100),n('monitor',640,340,310,100)],
     edges:[e('runtime','tools','按任务调用'),e('tools','data','结果与记录'),e('data','monitor'),e('guards','runtime','贯穿约束','feedback'),e('guards','monitor','审核与状态','feedback')],note:'架构示意页，不是生产监看实时数据；不代表全部工具当前在线。'}
    ,{id:'upgrade',title:'七、架构与升级发布',subtitle:'从模块规范修改到待发布接续；正式发版在本机监看中心由用户按钮执行。',w:1270,h:880,
      groups:[g('更新迭代工作区 · 不直接修改活动版本',20,25,1230,790)],
      nodes:[n('upgradeCenter',45,105),n('moduleDraft',350,105),n('pendingFiles',655,105),n('agentHandoff',960,105),n('acceptedPackage',960,375),n('releasePreflight',655,375),n('releaseButton',350,375),n('versionRecord',45,375),n('runtime',350,645),n('monitor',655,645)],
      edges:[e('upgradeCenter','moduleDraft'),e('moduleDraft','pendingFiles'),e('pendingFiles','agentHandoff'),e('agentHandoff','acceptedPackage'),e('acceptedPackage','releasePreflight'),e('releasePreflight','releaseButton','预检通过','pass'),e('releaseButton','versionRecord','真实回读通过','pass'),e('releaseButton','runtime','更新活动版本'),e('runtime','monitor')],note:'已实现模块提案、接续工作区与发布桥接。本云端未接入你的 Mac 执行器，未进行生产切换。'}
  ];
  window.XIAOBA_ARCHITECTURE={date:'2026-10-27',revision:'模块化待发布接续与监看发版入口',facts,views};
})();
