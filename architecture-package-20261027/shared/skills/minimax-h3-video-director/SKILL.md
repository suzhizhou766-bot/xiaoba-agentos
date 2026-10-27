---
name: minimax-h3-video-director
description: >-
  Expert guide and prompt generator for MiniMax H3 (Hailuo 3.0 / FL2VA) AI video workflows in ComfyUI.
  Use when the user wants to write or optimize AI video prompts, design camera motion, handle first/last
  keyframes (FL2VA), generate native audio and lip-sync dialogue, or orchestrate multi-layer cinematic visual
  effects (e.g., fantasy-to-reality transitions, particle synthesis, typography legibility).
---

# MiniMax H3 (Hailuo 3.0) 视听协同导演工作流指南

本 Skill 沉淀自实际商业与艺术视频生成实践，专门用于指导 **MiniMax H3（海螺 3.0 / FL2VA 模型）** 在 ComfyUI 等环境下的专业提示词编写与工作流编排。

MiniMax H3 是一款**全模态（Omni-modal）端到端视听一体大模型**，能够同时生成高画质视频与原生立体声音频（台词、音效、BGM 配乐）。

---

## 核心提示词框架：6 段式全参考标准（Full-Reference Mode）

MiniMax H3 采用结构化英文提示词响应效果最佳，标准格式必须严格包含以下 6 个区块：

```text
subject_definitions:
<Picture 1> is ...
<Subject 1> is ...

summary:
[task_type] ...

retention_analysis:
<Picture 1> ... : fully_preserved - ...
<Subject 1> ... : attribute_transfer - ...

detailed_description:
[Shot 1] (0.0s to X.Xs): ...

overall_soundscape:
...

non_diegetic_music:
...
```

### 1. `subject_definitions:`（主体与素材定义）
* **`<Picture N>`**：标记输入参考图的角色。如首帧（first frame）、尾帧（last frame）、中间关键帧（intermediate keyframe）、构图基准（composition anchor）。
* **`<Subject N>`**：标记画面中的核心角色、物体、特效或排版图层。
* **`<Audio N>`**：标记输入的参考音频（音色克隆或配乐参考）。
* **台词人物标识**：若人物需开口说话，标记为 `<Subject N> (S1)`。

### 2. `summary:`（任务概括与类型标记）
以方括号前缀声明任务类型，常用模式：
* `[keyframe completion]`：首尾帧补间、单图定格起跑或终点帧锁定。
* `[reference generation]`：风格/角色参考图生成、纯文生视频（T2VA）。
* `[video editing]`：原视频局部修改或多图元素融合重构。

### 3. `retention_analysis:`（素材留存与继承度分析）
逐行定义每个主体相对原图的继承变化：
* `fully_preserved`：完全保留，与原图严格一致（如位置、外貌、建筑骨架）。
* `partially_preserved`：部分保留（如保留外貌但改变动作或光影）。
* `attribute_transfer`：属性迁移/形态蜕变（如书签变成活兔、拼豆蜕变为真实混凝土）。
* `weak_reference`：弱参考（仅参考大致氛围或构图）。

### 4. `detailed_description:`（时序分镜、运镜与对白）
* **镜头与时序**：精确到秒（如 `0.0s - 2.5s`, `2.5s - 3.8s`），分段规划运镜速度与动作幅度。
* **摄影机运动（Camera Motion）**：
  * 固定机位：`strictly static locked-off tripod shot`（防人物/道具漂移变形）。
  * 推进/拉远：`smooth steady dolly-in / dolly-out`（匀速推拉）。
  * 变焦与景深：`rack-focus`（焦点转移）、`bokeh out of focus`（景深虚化）。
* **原生对白与口型匹配（Lip-sync）**：
  * 使用 `<d>[Language] "台词内容"</d>` 标签（如 `<d>[Chinese] "国庆快乐！祖国万岁！"</d>`）。H3 会自动对齐唇形与发音。

### 5. `overall_soundscape:`（环境音与物理拟音）
* 描写真实世界的环境底噪（微风、水流、树叶沙沙声）与动作物理音（脚步声、风吹红旗猎猎声、烟花破空与轰鸣声、琴键机械声）。

### 6. `non_diegetic_music:`（画外配乐/BGM）
* 指定背景音乐的乐器、情绪与节奏（如中国风古筝/竹笛、激昂交响乐、轻快尤克里里、古典独奏钢琴）。

---

## 常用高频实战设计模式（Design Patterns）

### 模式 A：首尾帧补间插值（FL2VA - First & Last Frame）
* **适用场景**：A 状态到 B 状态的连续位移或镜头穿梭（如车辆从正面开往背面、航拍穿云到目的地）。
* **连接规范**：
  * `ref_images.ref_image_0` ＝ `<Picture 1>`（首帧：`first frame of [Shot 1]`）。
  * `ref_images.ref_image_1` ＝ `<Picture 2>`（尾帧：`last frame of [Shot 1]`）。
* **提示词要点**：在时序中描述平滑过渡机制（如机位平移旋转跟踪、匀速位移），让模型建立两帧之间的物理运动因果。

### 模式 B：多图融合（海报包装 + 实景航拍）
* **适用场景**：用一张图提取文字/文字排版/节日元素，另一张图提供真实地理环境（如城市、河流、建筑）。
* **剔除商业 Logo**：在 `subject_definitions` 明确写明 `All brand logos in corners are strictly removed and omitted`。
* **文字避让与衬底光**：当复杂实景与文字重叠看不清时，设计 **`<Subject N>` 半透明淡金月晕衬底层（Moonlit Glow Backplate）**，在文字出现前先行晕开（bloom），确保黑红文字极高清晰度。

### 模式 C：材质异化与一镜到底时序分割（Fantasy-to-Reality）
* **适用场景**：虚实结合（棉花糖/拼豆/粒子流 -> 真实建筑）。
* **时间轴严密咬合**：
  * `0.0s - 2.5s`：抽象幻想冲刺（棉花糖高速穿梭爆裂成拼豆）。
  * `2.5s - 3.8s`：中间关键帧显现（云层大开，平飞对齐 `<Picture 1>` 拼豆城市中间帧）。
  * `3.8s - 6.5s`：冲击波扩散解构（中心能量波向外扩散，塑料微粒逐层现实化为 `<Picture 2>` 真实航拍）。

### 模式 D：人物表情生动化与防崩坏（Character Animation）
* **固定机位防抖**：群像或特写时，必须指定 `locked-off tripod shot`（锁定三脚架），防止身体和手持道具位移畸变。
* **高难度手部特写（如弹钢琴）**：
  * 解剖学约束：明确写入 `anatomically precise five fingers on each hand, zero finger morphing`（严禁融指与多指）。
  * 物理真实感：写入 `keys physically depress and rebound`（琴键受力下沉与回弹）。
  * 45度微滑轨运镜优于绝对静止，能让时序光影更连贯。

---

## ComfyUI 参数与配置速查

| 参数项 | 推荐设置 | 说明 |
| :--- | :--- | :--- |
| **UNet 模型** | `minimax_h3_fl2va_int8_convrot` | 官方 FL2VA 核心，支持首尾帧与多图参考 |
| **加速 LoRA** | `minimax_h3_turbo_4步加速` (权重 1.0) | 极大提升生成速度，低步数即可高保真收敛 |
| **分辨率** | 宽屏推荐 `1344 x 768` 或 `1280 x 720` | 符合 16:9 影视标准比例 |
| **帧率 (FPS)** | 默认 `24 fps` | 电影级平滑帧率 |
| **生成帧数 (Length)** | 秒数 × 24 | 2秒=48帧，3秒=72帧，4.5秒=108帧，6.5秒=156帧 |

---

## 成果交付标准规范：四步落地执行卡

为了降低用户在实际工作流（ComfyUI / 网页端）中的理解与操作成本，向用户交付或修改提示词时，**必须统一采用「四步落地执行卡」结构**，确保用户清晰获知提交素材、预期产出及参数配置：

### 1. 【素材准备】工作流输入清单
明确列出工作流中各输入节点所需素材，格式如下：
* `<Picture 1>`（主体/角色参考图）：明确说明使用哪张图（如三视图/设定图），用于锁定角色外观、比例、服饰与表情。
* `<Picture 2>`（场景/环境参考图）：明确说明使用哪张实景图或底图，用于锁定空间、材质、建筑与光影基准。
* `<Audio 1>`（音频节点）：明确说明是上传用户配音/音色参考，还是留空由 AI 自动生成全套音效与对白。

### 2. 【效果预演】预期生成画面与视听效果
在生成前对用户进行通俗直白的成片预期拆解：
* **运镜轨迹（摄像机怎么动）**：说明机位起止（如由远及近）、运动轨迹（如平稳前推、C字弧线运镜）与最终落幅构图（如九宫格对角构图）。
* **角色动作与时序演进**：分时段（如 0.0s~3.5s、3.5s~5.5s）说明主体的动态路线、正/侧面对镜头关系与表情切换（如开心转为眨眼）。
* **触发的特效与细节**：说明微动效（如眨眼星芒、浮动粉心、腮红光晕或阳光微粒）的出现时机与视觉质感，坚持克制高级原则。
* **原生视听体验（能听到什么）**：列出环境音（风声/脚步声）、对白旁白内容以及 BGM（背景音乐）情绪与配器。

### 3. 【参数速查】推荐工作流参数配置
以清晰表格给出对应生成的推荐配置：
* **视频时长 (Duration)** 与 **总生成帧数 (Length)**（公式：秒数 × 24 FPS）；
* **帧率 (FPS)**（默认 24 fps）；
* **分辨率 (Resolution)**（推荐 1280×720 或 1344×768 16:9 宽屏）。

### 4. 【完整提示词】标准 6 段式英文 Prompt
提供严格符合 6 段式语法（subject_definitions, summary, retention_analysis, detailed_description, overall_soundscape, non_diegetic_music）的代码块，支持用户一键复制运行。

