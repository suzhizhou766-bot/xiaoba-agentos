# 音乐生成器提示词档案

在用户点名音乐生成平台，或需要可直接粘贴的生成提示词时读取。动态能力、价格和版本必须以现场或官方资料为准；本文件只保留稳定的提示词原则。

## 通用单提示词结构

把最重要的信息放前面，按以下顺序组织自然语言：

1. instrumental 与内容用途；
2. 主类型、子类型、地域和年代质感；
3. 时长、BPM／体感速度、拍号和整体能量曲线；
4. 调性中心／调式、动机、和声与低音职责；
5. 主导、铺底、色彩、节奏和高潮声部的演奏法；
6. 段落、转折、收尾或循环返回；
7. 声场、距离、混响、频段和制作质感；
8. 必要的禁用项。

避免超过两种互相竞争的主风格、超过两件主奏乐器、多个互相冲突的年代或空间，以及无法执行的形容词堆叠。

## ElevenLabs Music（小八唯一生成器）

小八只使用用户指定的 ElevenLabs Music 生成配乐。生成走 API（Key 在小八密钥箱，名字 `elevenlabs`，经 `xb-secret run elevenlabs -- <已授权脚本>` 调用，细节见 SKILL.md），试听挑选和精细调整走用户已登录的 Chrome 网页。接口参数、模型、能力与价格可能变化，以当次官方 API 文档和可见界面为准。模型名称不清晰时停止生成并报告，不猜测，也不把语音合成模型当成音乐模型。

- 有镜头时间线、旁白让位、精确高潮或落版点时，优先 **composition plan**；每个 chunk 对应一个 cue，写明段名、时长、正向风格、少量负向风格和与前后段的连续性。
- 快速灵感稿才用单提示词；写明总时长、用途、BPM、主乐器、能量弧线、落版和 `instrumental only`。
- 需要可靠纯音乐时，启用接口或界面中对应的 instrumental 设定；不只依赖提示词否定人声。
- 分段风格用作曲机制表达，例如 `sparse low-register pulse`、`rising string ostinato`、`brief impact then immediate dialogue space`；负向项只禁止会真正破坏镜头的元素。
- 一次先生成一个批次（API 可一批 3—4 个版本）并试听。重试前判断是作曲设计错误、段落时间错误还是音色偏差，不盲目反复生成。
- 下载后保留原始生成文件，再进行 48 kHz WAV 转码、剪辑、对白避让和质检；不把转码说成音质升级。

官方核验入口：

- Music 生成与参数：https://elevenlabs.io/docs/api-reference/music/compose
- Composition plans：https://elevenlabs.io/docs/eleven-api/guides/how-to/music/composition-plans
- 生成提示词建议：https://elevenlabs.io/docs/overview/capabilities/music/best-practices

## 两栏或结构化平台

若平台把 Style、Lyrics、Arrangement、Key、BPM 或 Exclude 分成多个字段，按页面当前结构拆分，不把所有内容塞入一栏：

- 纯音乐模式：Lyrics 留空；结构提示放 Arrangement 或平台允许的结构区。
- Style：类型、年代、情绪机制、核心乐器和制作质感。
- Arrangement：段落、能量、动机、转折与结尾。
- Key／BPM：仅在平台支持独立字段时单独提供。
- Exclude／Negative：只放会真实破坏方向的少量禁用项。

## 多方案

默认一个最强方案。只有需要比较时输出多案，并改变音乐机制而不是换同义词：

- A：最贴合内容和生成稳定性；
- B：强化人物主观、地域或类型身份；
- C：极简、反向动态或实验声源。

评分衡量与当前内容的适配，不代表音乐高低。不要把三案都评成满分。

## 版权与真实性

- 不要求复刻在世艺术家、受保护作品、旋律、bassline 或编曲。
- 用户提供参考时，提取抽象机制：速度、节奏语法、和声压力、音色职责、段落、空间和年代制作。
- 合成器或程序音色写成 `string-like`、`choir-like` 等近似；只有真实样本、SoundFont、录音或可信渲染器才能直接称为真实乐器演奏。
