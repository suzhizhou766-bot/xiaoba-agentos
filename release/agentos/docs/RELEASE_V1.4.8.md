# 小八 AgentOS v1.4.8 · DSH 跟随 Desktop

DSH 后台每次调用读取 DeepSeek Harness Desktop 保存的默认模型与 Provider，完整配置由 `desktop_config` 指向 Desktop 自己的 `cordis.patch.yml`。AgentOS 不固定模型、不维护备用池、不存储或输出上游 API key。

CLI 以独立 headless 进程加载 Desktop 配置 overlay，直接执行 Harness 上游调用，不经过 Web 服务。Desktop 的模型选择持久化完成后，下一次后台调用重新读取；已开始的调用不在中途切换。

Desktop 配置缺失、选型无效或读取失败时明确停止，不回退 Web、headless 旧模型或其他 Provider。模型失败沿用任务级重试与冻结机制，不切换模型。

凭据保留在 Desktop 原生配置，仅以配置文件路径传入 CLI；AgentOS 路由只记录非敏感选型元数据，不复制配置内容。

此版本仅改变 DSH 配置来源和相关校验、测试、文档；AGY 路由及其他 v1.4.7 逻辑沿用。历史发布与业务审核记录保留。验收证据存于 work/v1.4.8-desktop-follow-20261004。
