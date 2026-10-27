---
name: director
description: Coordinate a video project across planning, music, deterministic composition, and asset acceptance. Use for end-to-end directing decisions or when the user says “开拍”; delegate specialist work to project, bgm, remotion, and assets.
---

# 导演编排层

本技能只负责跨专业决策和交付顺序，不复制专项技能的实现规则。

## 编排顺序

1. 调用 `project` 建立项目决策卡、成片承诺、叙事结构、分镜和拍摄风险。
2. 调用 `bgm` 把叙事节点编译为音乐结构、提示词、对白让位和 SFX 触发点。
3. 仅当需求包含可复现合成、MG、地图、数据驱动动画或批量比例时调用 `remotion`。
4. 调用 `assets` 保护原文、核对版本、生成资产清单和最终技术交付报告。

## 决策原则

- 一个项目只有一张当前项目决策卡；后续专项输出引用它，不各自发明目标。
- 原文、格式版和优化版相互独立，不覆盖。
- 每个镜头、音乐节点和音效事件都要服务成片承诺。
- 技术机器验收与用户创意验收分开；机器通过不能代替用户确认。
- 需要发布、付费、删除、购买或改变创意主方向时，由用户决定。

## 最小交付

- 项目决策卡与执行分镜
- BGM 结构与 SFX 时间轴
- 合成方案或拍摄执行清单
- 资产清单、版本说明和技术验收
- 未决风险与用户验收入口
