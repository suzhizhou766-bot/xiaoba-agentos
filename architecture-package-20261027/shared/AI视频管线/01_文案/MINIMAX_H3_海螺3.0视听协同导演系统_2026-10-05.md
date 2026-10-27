# 🎵 MiniMax H3 (Hailuo 3.0 / FL2VA) 视听协同导演工作流指南
## 全模态端到端视频与原生立体声生成规范

> **收录日期**：2026-10-05  
> **归属管线**：小八提示词导演台 / AI 视频管线标准规范库  
> **核心定位**：针对 MiniMax H3 (海螺 3.0 / FL2VA) 视听一体全模态模型，规范 6 段式全参考提示词结构与实战设计模式。

---

## 一、核心特性与模型能力

MiniMax H3 是一款**全模态（Omni-modal）端到端视听一体大模型**，能够同时生成高画质视频与原生立体声音频（台词、动作物理音效、BGM 配乐）。

- **端到端音画同出**：无需后期外挂 TTS 与音效合成，台词唇形天然对齐。
- **FL2VA 首尾帧插值**：支持 `<Picture 1>` 为首帧，`<Picture 2>` 为尾帧，进行端到端平滑动态过渡。
- **结构化响应极佳**：严格遵循 6 段式全参考标准（Full-Reference Mode）提示词，抗幻觉能力极高。

---

## 二、核心提示词框架：6 段式全参考标准（Full-Reference Mode）

MiniMax H3 采用结构化英文提示词响应效果最佳，标准格式严格包含以下 6 个区块：

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
<d>[Language] "台词内容"</d>

overall_soundscape:
...

non_diegetic_music:
...
```

### 1. `subject_definitions:`（主体与素材定义）
* **`<Picture N>`**：标记输入参考图的角色。如首帧（first frame）、尾帧（last frame）、构图基准（composition anchor）。
* **`<Subject N>`**：标记画面中的核心角色、物体、特效或排版图层。
* **`<Subject N> (S1)`**：**台词人物标识**，若人物需开口说话，必须标记为 `(S1)`。
* **清角规则**：必须写入 `All brand logos in corners are strictly removed and omitted`。

### 2. `summary:`（任务概括与类型标记）
以方括号前缀声明任务类型：
* `[keyframe completion]`：首尾帧补间、单图定格起跑或终点帧锁定。
* `[reference generation]`：风格/角色参考图生成、纯文生视频（T2VA）。
* `[video editing]`：原视频局部修改或多图元素融合重构。

### 3. `retention_analysis:`（素材留存与继承度分析）
逐行定义每个主体相对原图的继承变化：
* `fully_preserved`：完全保留，与原图严格一致（如位置、外貌、建筑骨架）。
* `partially_preserved`：部分保留（如保留外貌但改变动作或光影）。
* `attribute_transfer`：属性迁移/形态蜕变（如虚实转换、材质生长）。
* `weak_reference`：弱参考（仅参考大致氛围或构图）。

### 4. `detailed_description:`（时序分镜、运镜与对白）
* **镜头与时序**：精确到秒（如 `0.0s - 2.5s`, `2.5s - 5.0s`），分段规划运镜速度与动作幅度。
* **摄影机运动（Camera Motion）**：
  * 固定机位：`strictly static locked-off tripod shot`（防人物/道具漂移变形）。
  * 推进/拉远：`smooth steady dolly-in / dolly-out`（匀速推拉）。
  * 变焦与景深：`rack-focus`（焦点转移）、`bokeh out of focus`（景深虚化）。
* **原生对白与口型匹配（Lip-sync）**：
  * 使用 `<d>[Language] "台词内容"</d>` 标签（如 `<d>[Chinese] "国庆快乐！祝愿祖国繁荣昌盛！"</d>`）。H3 会自动对齐唇形与发音。

### 5. `overall_soundscape:`（环境音与物理拟音）
* 描写真实世界的环境底噪（微风、水流、树叶沙沙声）与动作物理音（脚步声、风吹红旗猎猎声、烟花破空与轰鸣声、琴键受力回弹机械声）。

### 6. `non_diegetic_music:`（画外配乐/BGM）
* 指定背景音乐的乐器、情绪与节奏（如中国风古筝/竹笛、激昂交响乐、轻快尤克里里、古典独奏钢琴）。

---

## 三、常用四大高频实战设计模式（Design Patterns）

### 模式 A：首尾帧补间插值（FL2VA - First & Last Frame）
* **适用场景**：A 状态到 B 状态的连续位移或镜头穿梭（如航拍穿云到城市地标、车辆从起点开至终点）。
* **连接规范**：
  * `<Picture 1>` ＝ 首帧（`first frame of [Shot 1]`）。
  * `<Picture 2>` ＝ 尾帧（`last frame of [Shot 1]`）。
* **提示词要点**：描述两帧之间的平滑物理因果运动轨迹。

### 模式 B：多图融合（海报包装 + 实景航拍）
* **适用场景**：一张图提取排版文字/节日元素，另一张图提供真实自然/城市环境。
* **剔除商业 Logo**：在 `subject_definitions` 明确写明剔除角标 Logo。
* **文字避让与衬底光**：设计 **`<Subject N>` 半透明淡金月晕衬底层（Moonlit Glow Backplate）**，在文字出现前先行晕开（bloom），确保黑红文字极高清晰度。

### 模式 C：材质异化与一镜到底时序分割（Fantasy-to-Reality）
* **适用场景**：虚实结合（棉花糖/拼豆/粒子流 -> 真实建筑）。
* **时间轴严密咬合**：
  * `0.0s - 2.5s`：抽象幻想冲刺（颗粒粒子高速穿梭）。
  * `2.5s - 3.8s`：中间关键帧显现（云层破开，结构初步对齐）。
  * `3.8s - 6.5s`：冲击波扩散解构（能量波向外扩散，粒子逐层蜕变为真实航拍）。

### 模式 D：人物表情生动化与防崩坏（Character Animation）
* **固定机位防抖**：群像或特写时，必须指定 `strictly static locked-off tripod shot`，防止身体和手持道具位移畸变。
* **高难度手部特写（如弹钢琴）**：
  * 解剖学约束：明确写入 `anatomically precise five fingers on each hand, zero finger morphing`（严禁融指与多指）。
  * 物理真实感：写入 `keys physically depress and rebound`（琴键受力下沉与回弹）。

---

## 四、ComfyUI 生产参数配置速查

| 参数项 | 推荐设置 | 说明 |
| :--- | :--- | :--- |
| **UNet 核心模型** | `minimax_h3_fl2va_int8_convrot` | 官方 FL2VA 核心，支持首尾帧与多图参考 |
| **加速 LoRA** | `minimax_h3_turbo_4步加速` (权重 1.0) | 极大提升生成速度，低步数即可高保真收敛 |
| **分辨率** | 宽屏推荐 `1344 x 768` 或 `1280 x 720` | 符合 16:9 影视标准比例 |
| **帧率 (FPS)** | 默认 `24 fps` | 电影级平滑帧率 |
| **生成帧数 (Length)** | 秒数 × 24 | 2s=48帧，3s=72帧，4.5s=108帧，6.5s=156帧 |
