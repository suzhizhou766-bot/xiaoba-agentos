---
name: media-services
description: 调用小八五个本地媒体服务时使用：提示词反推、深度图、超分、本地工具控制器、SFX 网关（端口 8890–8893、9878）。
---

# 五工具（本地媒体服务）

实体代码不在 `工具/` 下，而在 `AI视频管线/`（历史位置，暂不搬）：

| 端口 | 服务 | 代码 | 接口 |
|---|---|---|---|
| 8890 | 提示词反推 | `AI视频管线/06_内部工具/prompt_reverse_server.py` | `GET /healthz`、`POST /reverse`（导演台另用 `/describe`） |
| 8891 | 深度 | `AI视频管线/06_内部工具/depth_server.py` | `GET /healthz`、`POST /depth_image`、`/depth_video`、`/parallax` |
| 8892 | 超分 | `AI视频管线/06_内部工具/super_resolution_server.py` | `GET /healthz`、`POST /upscale` |
| 8893 | 本地工具控制器 | `AI视频管线/06_内部工具/local_tools_control_server.py` | `GET /healthz`、`GET /oss-tools`、`POST /service` |
| 9878 | SFX 网关 | `AI视频管线/scripts/sfx_server.py` | `GET /readyz`、`GET /match`、`POST /feedback` |

权威契约：`系统/小八工作体系/interfaces.md`「五工具」。全部只监听 127.0.0.1；跨域只允许本地页面和 `Origin: null`。

## 先确认在不在跑

先打 `/healthz`（SFX 是 `/readyz`）。不在跑时按下面三种情况处理，**不要自己装开机自启**：

1. 监看中心开着：8893 控制器由它看护，反推、深度、超分用导演台里的开关启停（或调控制器 `POST /service`）。
2. 一次性启动：用户双击 `AI视频管线/06_内部工具/start_prompt_director_services.command`。
3. 持久化（开机自启）：`bash 工具/治理/xiaoba_media_services.sh install`。这是 `interfaces.md` 允许的例外，但**只有用户明确要求时才执行**；`status` 可以随时查。

反推服务（仓库外的老代码）目前仍要求跟随 Antigravity 桌面当前选的模型，用它时 Antigravity 要开着且只有一个窗口。这和偏好里 2026-10-20 定的“AGY 改用 CLI 上次选定的模型”不一致，等改反推服务时一起处理。

## 边界

- SFX：本地素材优先；只有没有合格候选且用户明确要求时才生成新音效（走 `skills/bgm`）。采用、评分、否定用 `/feedback` 记录。
- 超分和调色交界处：不得把低清放大冒充原图精修（偏好 :65）。
- 已知不一致：控制器里深度服务的 LaunchAgent 标签写的是 `com.xiaoba.depth-server`，治理脚本装的是 `com.xiaoba.media.depth`。查状态时两个都看，修复等 AgentOS 发版。

## 已知坑（Playbook/踩坑/README.md）

- :70 换了配置写法后仓库外的工具还按旧写法读（反推报 local_agy 配置不完整）；:77 控制器没人拉起，开关全显示“启动失败”。
- SFX：:14 多套索引；:21 采样率不一致；:249 缺 whoosh、配乐起伏不明显。
