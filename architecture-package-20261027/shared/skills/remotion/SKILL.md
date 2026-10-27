---
name: remotion
description: Use the unified Remotion video pipeline for deterministic React/TSX motion graphics, data-driven animation, map animation, and technical delivery. Apply when a video request needs a reproducible local composition rather than an editor-only workflow.
---

# Remotion 视频工程 Skill

Remotion 是当前登记的视频合成引擎。正式成片必须通过 AgentOS 能力索引中状态为 `active` 且已验证的 `remotion_pipeline` 绑定；预览和中间产物进入运行时工作区，正式输出进入版本化交付目录。不得把用户尚未确认的创意验收写成 accepted。

## 路由

- MG/信息弹出/文字图形：使用 React 组件、`interpolate`/`spring`、可配置 props 和时间轴。
- 地图/路线/区域点亮：把路线、边界和镜头参数做成输入数据，使用分层组件与 schema props；地图 API key 只放本地 `.env`，不写入源码或交付包。
- 批量格式：通过 composition props 和版本化数据生成多个比例/时长，不复制组件实现。
- 空间/地产片：沿用导演台的镜头、材质、声音与技术规格卡，Remotion 负责可复现合成。

## 执行约束

1. 先建立项目决策卡和 composition schema，再写 TSX。
2. 所有素材用绝对路径或受控 public 资源引用；不把临时下载目录当资产源。
3. 预览可用 `remotion studio`，正式渲染只能调用已验证的 `remotion_pipeline` 绑定；绑定缺失时停止在可执行方案，不临时换引擎。
4. 交付前必须有技术 QC、业务机器检查、音频分轨/预混、资产清单和 checksum；创意/业务最终验收由用户完成。
5. 失败时保留日志和 `.runtime/work` 证据，禁止覆盖已有版本。

教程提炼与章节索引见 [references/tutorial_sources.md](references/tutorial_sources.md)。
