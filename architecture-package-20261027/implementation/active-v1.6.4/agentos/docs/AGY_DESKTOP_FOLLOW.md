# 后台模型选择边界

AGY 模型由 Antigravity desktop 选择，DSH 模型由 DeepSeek Harness Desktop 选择。AgentOS 读取后台自身的当前选型元数据，用于记录和查询能力档案，不自行决定模型、推理级别、Provider 或备用列表。

AGY 路由只绑定 desktop_debug_port。每次读取桌面的当前全局模型名称，原样交给 CLI 的 --model 参数；名称到真实模型的解析、可用性判断、登录和上游请求均由 AGY 执行。此参数是转交桌面选择，不是 AgentOS 另选模型。AgentOS 不再执行 agy models，不再有 7 秒的模型列表前置请求。CLI 默认设置和 desktop 设置可能独立，不能仅省略 --model 就假定跟随 desktop。

DSH 路由只绑定 desktop_config。每次以 Desktop 保存的完整配置作为 backend 自己的 profile overlay；AgentOS 不生成模型覆盖补丁，不在失败后自行切换另一模型。上游选择与执行交给 DSH。

本地后台路由禁止配置 model/model_pool/provider/provider_id/endpoint/credential。现有已禁用的兼容适配器保持禁用，不作为备用。

能力查询使用后台当前报告的名称匹配已有能力档案，显示名只能匹配同一身份，不能将另一模型或推理级别的能力当作当前能力。未声明的能力仍为 unknown，不据此分配需要该能力的任务。AGY/DSH 目前的 JSON 执行结果没有返回模型身份，因此结果中的选型字段来自调用前的后台界面配置观察，不能声称它是执行结果携带的模型证明。

每次调用跟随当次界面设置，后续调用重新读取。任务级等待上限、失败分类、重试与冻结沿用既有机制。桌面须打开且可读取，Desktop 配置须有效；无法确认后台当前选择时停止，不静默替换。桌面关闭或内部状态接口改变仍可能导致读取失败，不能将这类问题等同上游模型不可用。
