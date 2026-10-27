# 🎯 角色一致性与四联资产防变脸系统规范 (CHARACTER CONSISTENCY)
## 一个方法解决远景变脸、细节丢失与造型混乱

> **收录日期**：2026-10-05  
> **归属管线**：小八提示词导演台 / AI 视频管线标准规范库  
> **核心公理**：切断错误头部来源（去头全身三视图） + 强制唯一高清面部参考（独立正脸特写）

---

## 一、核心病根与原理解析

### 1. 为什么 AI 一到远景就容易变脸？
- **模型的“就近取材”机制**：
  当生成中景、远景或全景镜头时，AI 视频模型会优先参考画面中的全身照。但全身照中的人脸面积极小、像素密度极低且边缘模糊。
- **无法调用高清特写**：
  如果全身照自带头部，AI 会默认“这已经是头部的参考”，而不再去关联其他清晰的头像，只能根据模糊的一团像素**自行脑补推断长相**，最终直接导致五官塌陷、发型异化、妆容混乱和频繁换脸。

---

## 二、破局方案：四联角色版（4-Panel Character Sheet）

采用 **16:9 标准画幅** 制作标准四联角色资产板：

```
┌──────────────┬──────────────┬──────────────┬───────────────────┐
│              │              │              │                   │
│   正面(无头)  │   侧面(无头)  │   背面(无头)  │    正脸高清特写    │
│              │              │              │                   │
│ 躯干/服装版型 │ 侧身线条/厚度 │ 背部/肩背剪影 │ 骨相/五官/发型/妆容 │
└──────────────┴──────────────┴──────────────┴───────────────────┘
```

### 1. 为什么必须“把头去掉”？
- **物理切断干扰**：把全身照的头切掉，让 AI 在生成远景时无法从全身照中“就近取材”到错误的模糊头部信息。
- **职责精准分离**：
  - 前三联（无头全身三视图）：100% 用于约束身高比例、体型骨架、服装剪裁、颜色材质与体态。
  - 第四联（正脸特写）：100% 成为 AI 读取眼睛、鼻梁、唇形、发型、妆容和微小细节（如创口贴、伤痕）的唯一信息源。

---

## 三、根据剧本制作多状态角色资产（严禁一张图赌全程）

商业短剧或连续叙事中，绝不能只做一张角色图。必须在开机前预制不同情节状态的资产包：

| 状态类别 | 资产描述要点 | 典型应用情境 |
|---|---|---|
| **1. 基础状态** | 正常状态、标准妆发、自然皮肤质感 | 日常镜头、开场亮相 |
| **2. 受伤/战损状态** | 明确标记创口贴位置（如鼻梁横贴）、嘴角擦伤、微汗/污迹 | 打斗、逃生、受挫后镜头 |
| **3. 情绪表情资产** | 微笑亲和、冷漠疏离、愤怒紧绷、悲伤落泪 | 剧烈戏剧冲突、情绪对峙 |
| **4. 服装状态资产** | 日常便装、战术机能服、风衣大衣、正式礼服 | 场景切换、身份伪装 |
| **5. 特殊造型资产** | 墨镜、遮阳帽、发带发饰、标志性手持道具（手枪/提包） | 遮挡视线、功能性动作 |
| **6. 标志细节特写** | 标志性眼妆/唇色、发色挑染、特定项链耳环特写 | 情感特写、身份象征 |

---

## 四、使用时的四大黄金法则

### 法则 1：图片垫图 + 文字精准描述（双重强约束）
- 单纯给图片，AI 会漏细节；单纯写文字，AI 会随机换脸。
- **标准操作**：在提示词中显式引用角色图（如 `@character_sheet`），并在文字提示词中把关键特征逐字写出，形成图片与文字的双重互锁。

### 法则 2：明确细节特征（不留脑补空间）
- 必须用文字明确写死的特征：
  - 发型与发色（如：黑色狼尾碎发、层次分明）
  - 妆容与肤色（如：冷白皮、暗红哑光唇色、烟熏眼影）
  - 标志性微特征（如：鼻梁正中横贴白色医用创口贴）
  - 饰品与服饰（如：银色心形锁骨项链、黑色露肩背心、百褶裙）

### 法则 3：控制画面比例与构图
- 四联角色板标准画幅统一采用 **16:9**，确保三视图和正脸特写比例舒展，不会被压缩拉伸。

### 法则 4：全项目资产统一与模型锁死
- 同一套角色资产必须在全项目、全分镜中统一调用；
- 严禁在中途混用不同风格的垫图（如真实照片与二次元插画混用），否则会导致底层风格漂移。

---

## 五、导演台与提示词集成模板

### 1. 英文标准化约束块（注入 Prompt）
```text
CHARACTER CONSISTENCY & ASSET LOCK: Cross-reference four-panel character asset board reference (@character_sheet in 16:9 ratio) containing headless body three-view (front, side, back) strictly locking wardrobe cut, silhouette, and proportions; independent high-resolution facial closeup strictly locking eye shape, nose, lips, hairstyle, makeup, and expression. In wide, full, and medium shots, the AI is strictly prohibited from inferring or re-hallucinating faces from distant blurry bodies; face must replicate the closeup reference faithfully with zero face mutation or identity drift. Locked identity & detail traits: [在此填入具体发型/妆容/创口贴/首饰描述].
```

### 2. 负面提示词（Negative Prompt 补充）
```text
face mutation in wide shots, distant face distortion, blurry facial features, inconsistent face, changing makeup, hairstyle drifting, morphing identity, wardrobe mutation, missing accessories, inconsistent clothing
```
