# 小八工作体系接口契约

所有接口均服务于任意兼容 Agent。Agent 名称不是权限、事实或模型来源。

## 模块契约与运行时路径

机器可验证的模块声明位于 `系统/小八工作体系/registry.json` 及其 `modules/*.json`。每个 manifest 至少声明实现状态、实现路径、数据契约版本、能力、权限和健康检查；路径中的 `${ROOT}` 指私人助理根目录，`${KNOWLP_ROOT}` 默认为 `~/Documents/Codex/KnowLP`，也可由环境变量覆盖。验证命令为：

```bash
XIAOBA_ASSISTANT_ROOT="$HOME/Documents/私人助理" \\
KNOWLP_ROOT="$HOME/Documents/Codex/KnowLP" \\
"$HOME/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14" -B "$HOME/Documents/私人助理/工具/治理/xiaoba_contract_check.py" --json
```

`implemented` 表示入口存在且可执行，不表示所有外部依赖或上游请求都可用；外部运行时缺失只能标为 warning。模块契约检查不能替代真实端到端验收。

## 五工具持久化启动

五个本地媒体服务的持久化 LaunchAgent 管理脚本为 `工具/治理/xiaoba_media_services.sh`。它只管理 `com.xiaoba.media.*`，不修改 Codex、Headroom 或 DSH 路由。需要开机自动恢复时执行 `bash 工具/治理/xiaoba_media_services.sh install`；停止并移除这些 LaunchAgent 时执行 `bash 工具/治理/xiaoba_media_services.sh remove`；查看注册状态时执行 `bash 工具/治理/xiaoba_media_services.sh status`。启动前仍需确认对应虚拟环境和本地凭据文件存在。


- 位置：`第二大脑`（目录名是路径兼容标识，不随版本改名）。
- 读取：从 `第二大脑/00_小八AgentOS.md` 进入并按裁决顺序打开原文。
- 定位：`~/.agents/skills/xiaoba-agentos/scripts/xiaoba memory-retrieve "查询"`（另有 `skill-match`、`knowledge-retrieve`）；命中摘要不等于事实，必须回读原文。
- 写入：普通 Agent 用 `xiaoba memory-candidate-create --title … --body-file … --author <agent-id>` 登记候选，由用户在监看中心的裁决中心两次批准后发布；最高裁决只能由用户确认。
- 改了原文之后：在 `小八AgentOS` 目录运行 `scripts/build_knowlp_index.py --vault <Vault> --graph-dir .runtime/knowlp` 重建，再加 `--verify` 校验。

## KnowLP

- MCP：`${KNOWLP_ROOT}/runtime/knowlp_mcp.py`，stdio。默认 `${KNOWLP_ROOT}` 为 `~/Documents/Codex/KnowLP`，可通过环境变量覆盖。
- 工具：`knowlp_search`、`knowlp_get_note`、`knowlp_stats`、`skill_search`、`knowlp_record_feedback`。
- 图谱目录：`/Users/suzhizhou/Documents/小八AgentOS/.runtime/knowlp`，由 AgentOS 配置指定。
- 重建：第二大脑原文改动后，在 `小八AgentOS` 目录运行 `scripts/build_knowlp_index.py --vault <Vault> --graph-dir .runtime/knowlp`，再加 `--verify`；经 AgentOS 发布的经验由发布流程自动处理。
- Chroma 已退役；JSON skill index 是技能索引现行实现。

## 五工具

- `8890` 提示词反推：`GET /healthz`、`POST /reverse`。
- `8891` 深度：`GET /healthz`、`POST /depth_image`、`POST /depth_video`、`POST /parallax`。
- `8892` 超分：`GET /healthz`、`POST /upscale`。
- `8893` 控制器：`GET /healthz`、`GET /oss-tools`、`POST /service`。
- `9878` SFX：`GET /readyz`、`GET /match`、`POST /feedback`。
- 全部仅监听 loopback；浏览器跨域只允许本地页面与 `Origin: null`。

## Remotion（视频合成）

- 位置：`工具/Remotion`，Remotion 与 @remotion 包锁定 4.0.535。说明见同目录 `使用说明.md`。
- 视频管线的合成核心。不开机自启、不常驻：需要时由用户双击 `启动Remotion.command` 开启，Studio 地址 `http://localhost:3330`，在启动它的终端按 Control+C 关闭。
- Agent 调用：在该目录修改 `src/` 下的 React/TSX；`npm run lint` 检查；`npm run render:demo -- out/<文件名>.mp4 --codec=h264` 导出，执行前确认目标文件不存在。一次性渲染不需要 Studio 开着。
- Studio 没开而任务需要预览时，提示用户开启，不要自行拉起或配置自启。不擅自升级版本。
- 写作与验收方法见技能 `remotion`（第二大脑 `20_能力与技能/remotion`）；`AI视频管线/` 里的合成项目与渲染脚本是既有的项目绑定，本工具不替代它。

## BGM 与 SFX

- BGM 设计入口：`skills/bgm/SKILL.md`。
- ElevenLabs 两种方式并存（2026-10-17 用户确定）：
  - 生成走 API（`https://api.elevenlabs.io/v1/`，请求头 `xi-api-key`）。Key 在小八密钥箱（`工具/密钥/xb-secret`），名字 `elevenlabs`，变量 `ELEVENLABS_API_KEY`：调用 API 的脚本先请用户 `xb-secret allow elevenlabs <脚本>`，之后用 `xb-secret run elevenlabs -- <已授权脚本>` 运行；Agent 不读 Key 本身，不输出、不复制、不记录，也不让用户把 Key 贴进对话。音乐、音效、配音一次出几个版本，直接存进项目目录。
  - 试听挑选和精细调整（例如音乐分段编辑）走用户已登录的 Chrome ElevenLabs 网页。
  - 两种方式都一样：提交前说明模型、时长、版本数和会扣积分；用户明确说“生成”即可提交一个批次。不查询账户余额（用户 2026-10-17 明确不需要）。
  - 密钥是受限密钥，权限由用户在 ElevenLabs 后台控制；接口返回权限不足时停下请用户处理。
- 本地 SFX 优先；只有无合格候选且用户明确要求时才生成新音效。
- SFX 的采用、评分和否定通过 `/feedback` 或登记脚本记录。

## 经验回流

任何 Agent 都可以登记候选：

```bash
XIAOBA="$HOME/.agents/skills/xiaoba-agentos/scripts/xiaoba"
"$XIAOBA" memory-candidate-create --title <标题> --body-file <正文文件> --author <agent-id>
"$XIAOBA" review-list --stage all
```

候选不会自动成为事实：用户在小八监看中心的裁决中心完成交付初审和终审后，才由 AgentOS 发布进第二大脑。完整流程见 `learning-loop.md`。


## REA（授权软件分析）

- 部署：`工具/REA逆向`，精确版本 6.5.0；模块清单为 `系统/小八工作体系/modules/rea.json`，安装与验证事实为 `工具/REA逆向/deployment.json`。
- CLI（命令行）入口：`/Users/suzhizhou/Documents/私人助理/工具/REA逆向/bin/rea`。静态分析可直接使用；原生反编译引擎尚未配置。
- MCP（模型上下文协议）：同一入口加 `mcp`，stdio（标准输入输出）；接入片段在 `工具/REA逆向/config/`，未改个人客户端配置。`mcp doctor --json` 与实际 MCP 分析已通过，不代表当前聊天已加载。
- 技能：`skills/rea-reverse-engineering/SKILL.md`。已通过 AGY/DSH 独立审查并经 skill-publish（审核登记入口）纳入正式 `skill-match`（技能匹配），当前 active/callable（活动/可调用）；查询 deployment.json 与技能卡确认验证范围，未配置的原生引擎仍不可用。
- 2026-10-17 主动引导：输入“调用rea”后，信息不全时以选项收集分析对象、重点、授权，再补应用名/路径；已知信息不重复问，需求齐全则按已授权范围执行。宿主有提问组件时使用组件，否则用编号列表；不会因默认选项或未答复取得授权。
- 输入仅限已授权软件；输出为 Evidence（证据包）及摘要。默认静态，不执行目标、附加进程或捕获流量；此类操作须另在任务授权范围内。
- `publish_enabled=false`（模块关闭自动发布）；不改小八全局 memory（记忆）策略、现行 release（发布）或后台路由。完整说明见工具目录的 使用说明.md。

## AE调用（After Effects 脚本控制）

- 部署：`工具/AE调用`，模块清单 `系统/小八工作体系/modules/after-effects.json`，安装与验证事实 `工具/AE调用/deployment.json`，说明 `工具/AE调用/使用说明.md`。技能：`skills/after-effects-control/SKILL.md`。
- 前提：AE 2026 正在运行；“允许脚本写入文件和访问网络”已开。不开机自启任何新东西；AE 没开时提示用户打开。
- 文件队列（任何 Agent，含远程会话）：`.jsx` 先写临时文件再 `mv` 进 `工作区/ae-scripts/queue/`，AE 每秒执行一次队列，脚本移到 `done/`，日志 `logs/<名>.log`，心跳 `logs/_heartbeat.txt`。执行器由用户安装的 `/Applications/Adobe After Effects 2026/Scripts/Startup/xk_bootstrap.jsx` 在 AE 启动时加载。
- MCP（本机 Agent）：stdio，命令 `配置/node/bin/node`，参数 `工具/AE调用/after-effects-mcp/build/index.js`，57 个工具；需要用户把 `build/scripts/mcp-bridge-auto.jsx` 装进 AE 的 ScriptUI Panels 并打开面板，共享文件夹 `~/Documents/ae-mcp-bridge`。接入片段在 `工具/AE调用/config/`，未改个人客户端配置。
- 本机终端：`工具/AE调用/adobe-agent-skills/skills/after-effects/scripts/runner.sh --background <脚本> '<JSON>'`（34 个现成脚本）；远程 Agent 经 `工具/AE调用/队列桥接/xk_lib_call.jsx` 调用同一批脚本。
- 删除、覆盖已有渲染、丢弃未保存工程前先问用户。
