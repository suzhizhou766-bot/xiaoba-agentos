# 后台模型选择边界

（v1.6.5 起，用户 2026-10-20 定。文件名沿用旧称，内容以本页为准。）

模型由用户自己选，小八只读取、原样转交，不自行决定模型、推理级别、Provider 或备用列表：

- **DSH**：跟随 DeepSeek Harness 桌面的配置。路由只绑定 `desktop_config`，每次以桌面保存的完整配置作为 backend 自己的 profile overlay；小八不生成模型覆盖补丁。
- **AGY**：用用户在 agy CLI 里上次选定的模型。路由 `selection: agy_cli_settings`，绑定 `settings_path=~/.gemini/antigravity-cli/settings.json`，每次调用前读其中的 `model`（例如 "Gemini 3.8 Flash (High)"），原样交给 `--model`。不再读取或启动 Antigravity 桌面。线上旧写法（`local_account` 加桌面字段）仍能加载，按同样方式读 CLI 设置，桌面字段被忽略；旧写法不带 `settings_path` 时读默认位置 `~/.gemini/antigravity-cli/settings.json`（每次调用时按当前用户的家目录计算）；无论新写法还是旧写法，`settings_path` 都必须正好是本用户的这个文件，放在别处的同名文件不认。Qoder 同理：写了 `settings_path` 就必须正好是 `~/.qoder/settings.json`，省略时读这个默认文件（发布说明第二十一节）；模型名必须以字母或数字开头，形状像命令行开关的值不认；子进程环境里不保留任何 `QODER_` 开头的变量。
- **Qoder（备用）**：用用户在 qodercli 里上次选定的模型和该模型的思考强度。路由 `selection: qodercli_settings`，绑定 `settings_path=~/.qoder/settings.json`，读取 `model.name` 和 `model.preferences.<name>.reasoning.effort`，以 `--model`、`--reasoning-effort` 传入。不读取 Qoder 桌面，不使用 Qoder CN。选定的是 qodercli 的别名（auto、ultimate、fast、efficient）时，用户选的就是“由 qodercli 决定”，小八原样传入，并以 `modelUsage` 记录实际作答的那一个具体模型（这是对“不符即停”的明确例外：别名本来不指向某一个模型）；无论别名还是具体模型，回答里都必须恰好只出现一个模型，出现多个就判失败。

读不到明确的模型名、思考强度不受支持，或者 Qoder 实际响应的模型与选定的具体模型不一致（别名见上），都停止并报告。不写死，不擅换，不回退到别的模型。

本地后台路由禁止配置 model、model_pool、provider、provider_id、endpoint、credential。现有已禁用的兼容适配器保持禁用，不作为备用；备用只有 `qoder_review`。

能力查询用后台当前报告的名称匹配已有能力档案；显示名只能匹配同一身份，不能把另一个模型或推理级别的能力当作当前能力。未声明的能力仍为 unknown，不据此分配需要该能力的任务。AGY、DSH 的执行结果不带模型身份，结果里的模型字段来自调用前读取的设置，不能当作执行结果携带的模型证明；Qoder 的实际模型取自它回报的 `modelUsage`。

每次调用都重新读取，用户在 CLI 里换了模型，下一次调用就生效。任务等待改为三个 CLI 共用“连续 300 秒无实际进展”窗口、不设任何总时长截止（发布说明第七节），失败分类按第十三节修正，重试与冻结沿用既有机制。
