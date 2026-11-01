# 后台模型选择边界

（v1.6.5 起，用户 2026-10-20 定；AGY 一段按用户 2026-11-01 裁决改为写在配置里。文件名沿用旧称，内容以本页为准。）

模型由用户自己定，小八原样取用、原样转交，不自行决定模型、推理级别、Provider 或备用列表。用户 2026-11-01 定了：配置里的模型名就是实际使用的模型；Agent 节点配置由用户设定，测试连通通过即可，小八不再核对实际应答的是哪个模型，也不设模型比对门禁。

- **DSH**：跟随 DeepSeek Harness 桌面的配置。路由只绑定 `desktop_config`，每次以桌面保存的完整配置作为 backend 自己的 profile overlay；小八不生成模型覆盖补丁。
- **AGY**：模型写在路由配置里。路由 `selection: configured_model`，必须带 `model`（例如 "Gemini 3.8 Flash (High)"），小八把它原样交给 `--model`，不查询模型目录、不读取默认模型、不猜推理档。agy CLI 设置文件（`~/.gemini/antigravity-cli/settings.json`）和 Antigravity 桌面都不再读取。旧写法（`agy_cli_settings`、`local_account`）仍能通过配置校验（切换前用户改不了，旧代码不认 `model`），但调用 AGY 时会被拒绝，并写明怎么改；不会悄悄回退到别的模型来源。模型名必须以字母或数字开头，形状像命令行开关的值不认。
- **Qoder（备用）**：仍读它自己的设置文件——这是现状。用用户在 qodercli 里上次选定的模型和该模型的思考强度。路由 `selection: qodercli_settings`，绑定 `settings_path=~/.qoder/settings.json`，读取 `model.name` 和 `model.preferences.<name>.reasoning.effort`，以 `--model`、`--reasoning-effort` 传入。不读取 Qoder 桌面，不使用 Qoder CN。写了 `settings_path` 就必须正好是 `~/.qoder/settings.json`，省略时读这个默认文件（发布说明第二十一节）；子进程环境里不保留任何 `QODER_` 开头的变量。选定的是 qodercli 的别名（auto、ultimate、fast、efficient）时，用户选的就是“由 qodercli 决定”，小八原样传入。CLI 回报的 `modelUsage` 只留在原始输出里，不再用来核对模型，也不再作为回执的模型身份；回执里的模型身份取配置或设置文件的选定值。

AGY 配置里没写 `model`、模型名形状不对，或 Qoder 读不到明确的模型名、思考强度不受支持，都停止并报告。不写死，不擅换，不回退到别的模型；实际响应是不是同一个模型不在核对范围（用户 2026-11-01 裁决）。

DSH 与 Qoder 路由禁止配置 model、model_pool、provider、provider_id、endpoint、credential；AGY 路由只允许 `driver`、`selection`、`model` 三项，除本路由自己的 `model` 外同样不得配置上游、凭据或设置文件。现有已禁用的兼容适配器保持禁用，不作为备用；备用只有 `qoder_review`。

能力查询用节点配置或设置文件里选定的名称匹配已有能力档案；显示名只能匹配同一身份，不能把另一个模型或推理级别的能力当作当前能力。未声明的能力仍为 unknown，不据此分配需要该能力的任务。Antigravity 声明了 `gemini-3.8-flash-low` 与 `gemini-3.8-flash-high` 两份，内容完全相同，`shell`、`filesystem` 等执行权限两边都是 false；补上 High 只是让配置里的模型能力可知，不放宽任何权限。AGY、DSH 的执行结果不带模型身份，结果里的模型字段只是调用前使用的配置值，不能当作执行结果携带的模型证明，新回执也不再写“模型核对结果”这类字段（旧回执里的这个字段仍能读）。

AGY 的模型改在路由配置里，改模型就是改配置；Qoder 每次调用都重新读设置，用户在 qodercli 里换了模型，下一次调用就生效。会话、已安装入口、后台进程和裁决中心仍须用内容一致的配置（B09），改完配置按现行做法重启使用它的进程。任务等待改为三个 CLI 共用“连续 300 秒无实际进展”窗口、不设任何总时长截止（发布说明第七节），失败分类按第十三节修正，重试与冻结沿用既有机制。
