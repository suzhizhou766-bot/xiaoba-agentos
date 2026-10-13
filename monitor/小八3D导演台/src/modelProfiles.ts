export type VideoModelId = "omni-flash-1.1" | "seedance-2.5";
export type GuideSection = { title: string; text: string };
export type ModelProfile = {
  id: VideoModelId;
  name: string;
  provider: string;
  apiModelId: string | null;
  checkedAt: string;
  version: string;
  summary: string;
  language: string;
  sources: { title: string; url: string; scope: string }[];
  officialNotes: string[];
  workflow: string[];
  sections: GuideSection[];
  template: string;
  assistantInstruction: string;
};
const date = "2026-09-16";
const common: GuideSection[] = [
  {
    title: "导演台实践：先把三维数据翻译成可见结果",
    text: "世界坐标供助手重建路径，不应直接堆进视频提示词。把坐标变化解释成主体位于画面左/中/右、景别如何变化、前后景如何遮挡。机位向目标靠近是推进；镜头焦距变化才是变焦，不能混为一谈。方向同时说明是画面方向还是角色自身方向。时间线不是对生成模型的逐帧约束。",
  },
  {
    title: "导演台实践：明确素材职责",
    text: "first-frame-blocking.png 与 last-frame-blocking.png 只控制取景和站位。由助手根据角色/建筑/风格参考制作 final-first.png 与 final-last.png，再交给视频平台。人物占位模型不代表最终服装、肤色、性别或画风；当前包不包含运动视频，不要声称已有白模参考视频。",
  },
  {
    title: "导演台实践：首尾图制作约束",
    text: "两张最终图使用同一组角色与场景参考，统一人物数量、服装、道具归属、建筑几何、材质和光照方向。分别匹配首尾投影与画面裁切。移动机位可以改变透视与可见区域，不应为了凑尾帧重排物体。先核对首尾图是否能由一段连续空间运动连接。",
  },
  {
    title: "导演台实践：排错顺序",
    text: "先查参考槽位是否用错，再查动作和镜头描述是否矛盾，再检查首尾空间是否连续。身份漂移时具体列出要保持的外观；左右互换时补充画面坐标关系；运镜跳跃时减少同一时段的运动指令。一次只改一类问题；这些是本项目的制作建议，不是官方成功率承诺。",
  },
  {
    title: "导演台实践：提交与验收",
    text: "用户在所选平台打开对应视频模型，把最终图像绑定到首尾槽位或明确指定图片职责，再粘贴成品提示词。画幅、可选时长和画质以该入口为准。验收人物身份、数量、建筑、动作方向、运镜连续性、结尾构图和声音。精确商标/字幕可在后期合成。",
  },
];
export const modelProfiles: Record<VideoModelId, ModelProfile> = {
  "omni-flash-1.1": {
    id: "omni-flash-1.1",
    name: "Gemini Omni 1.1 Flash",
    provider: "Google",
    apiModelId: "gemini-omni-1.1-flash",
    checkedAt: date,
    version: "2026-09-16.1",
    summary: "首尾状态 + 中间运动路径；明确连续镜头，分别指定素材用途。",
    language:
      "交付中文审阅版与英文投递版；英文优先是基于 API 文档的语言评估范围，不代表中文不可用。",
    sources: [
      {
        title: "Google Gemini API · Omni 视频生成与编辑",
        url: "https://ai.google.dev/gemini-api/docs/omni?hl=zh-cn",
        scope: "1.1 API、媒体标记、首尾帧及接口限制",
      },
      {
        title: "Google DeepMind · Gemini Omni 提示指南",
        url: "https://deepmind.google/models/gemini-omni/prompt-guide/",
        scope: "镜头设计、首尾路径与迭代编辑；部分内容适用于 Omni 系列",
      },
    ],
    officialNotes: [
      "API 模型标识为 gemini-omni-1.1-flash；支持首尾图片插值。<FIRST_FRAME> 和 <LAST_FRAME> 指定端点；尾帧标记需配合首帧。图片/视频参考使用从 0 起的 <IMAGE_REF_N> / <VIDEO_REF_N>。来源：Google API。",
      "API 当前不接受音频参考上传，不提供独立 negative-prompt 参数；约束写入普通提示词。英语已评估，其他语言未评估。不要把官网的多模态概览当作每个接口都已开放的能力。来源：Google API。",
      "官方提示指南建议说明景别、运动、场景、光线和动作；连续镜头应明确声明。首尾控制要交代起始画面、连接路径和最终揭示；局部编辑聚焦要改的部分。来源：DeepMind。",
      "延长功能按 10 秒追加，累计上限为 40 秒；这不等于支持一次直接生成 40 秒。产品功能因入口、地区及套餐而异。来源：DeepMind。",
    ],
    workflow: [
      "在导演台选 Omni 1.1 Flash，设计轨迹并导出交接包。",
      "助手按包内构图参考和用户外观素材生成最终首尾图，交付中英两版提示词。",
      "用户在实际使用的平台选相应模型与首尾帧工作流，核实可选时长后上传最终图片。",
      "有首尾槽位就使用槽位；支持官方媒体标记的入口可使用对应标记。仅输入文件名或标记不等于上传了图片。",
      "生成后先核对空间与运镜，再进行局部迭代；较长方案按平台支持的分段或续写流程处理。",
    ],
    sections: [
      {
        title: "本项目整理：五类任务别混写",
        text: "新建视频描述整段意图；首尾帧任务描述两端如何连接；参考生成只继承指定外观/风格；编辑任务指出修改范围；续写任务接续上一段结束状态。下面模板是本项目原创写作框架，不是官方固定语法或实测成功率结论。",
      },
      {
        title: "本项目模板：连续首尾帧",
        text: "绑定最终首图与尾图后：起始为角色 A 在画面左侧的中景。镜头沿角色前侧缓慢横移，A 保持站位，背景柱体产生自然视差。到结尾形成与尾图一致的三分之二侧面构图。单个连续镜头，保持人物外观和柱体布局。声音按用户要求另写。",
      },
      {
        title: "本项目模板：双人调度与建筑保真",
        text: "起始 A 在前景左侧，B 在后景右侧。A 沿走廊向 B 走近，镜头后退维持 A 的中景；B 留在原处。结束 A 停在 B 左前方。保留同一走廊的柱距、门洞和地面高差。用可见空间关系说明运动，避免把角色自身左侧写成画面左侧。",
      },
      {
        title: "本项目模板：局部编辑 / 续写",
        text: "局部编辑草稿：只把背景灯光调整成柔和暖光，人物身份、动作、机位、时长和其他内容沿用原片。续写草稿：从上一段停止的位置继续，A 先停步再回头，机位继续沿原方向横移，声音接续。续写的时间从新追加片段的起点说明，先核实所用平台是否开放该功能。",
      },
      {
        title: "本项目实践：声音单列",
        text: "为环境声、音乐、对白分别填具体意图。没有对白需求时不擅自编写对白；需要静音时明确注明。不要将参考视频里的声音自动当成声音参考，也不把原生声音生成和上传音频参考混为一谈。",
      },
      ...common,
    ],
    template:
      "【交付说明】这是待助手结合素材填写的模板，不是最终投递文本。\n【首尾绑定】使用最终首帧和最终尾帧；若入口支持标记：<FIRST_FRAME> <LAST_FRAME>。\n【镜头整体】单个连续镜头，{起始构图}。\n【动作与路径】从{起始状态}沿{相机路径}到达{最终状态}，主体执行{可见动作}。\n【节奏】{相对于选定区间起点的时间段与阶段结束状态}。\n【保持】{角色身份/建筑/道具/光线不变量}。\n【声音】{用户声音要求}。\n【英文投递版】助手将上述确定内容改写为自然英文；保留同样的素材绑定和时序，不新增表演或运镜。",
    assistantInstruction:
      "用户已选择 Gemini Omni 1.1 Flash，无需再询问模型。阅读包内指南，生成最终首尾图、中文审阅版与英文视频提示词；按实际平台绑定最终图，连续镜头意图明确，不能把参考图当成完成的生成图。",
  },
  "seedance-2.5": {
    id: "seedance-2.5",
    name: "Seedance 2.5",
    provider: "ByteDance",
    apiModelId: null,
    checkedAt: date,
    version: "2026-09-16.1",
    summary: "素材职责 + 空间关系 + 分段时间轴 + 运镜 + 声音 + 不变量。",
    language: "以中文制作稿和中文投递提示词为主；按用户平台需要再提供英文。",
    sources: [
      {
        title: "ByteDance Seed · Seedance 2.5 模型页",
        url: "https://seed.bytedance.com/zh/seedance2_5",
        scope: "30 秒、两次延长、白模控制和参考能力",
      },
      {
        title: "ByteDance Seed · Seedance 2.5 发布与案例",
        url: "https://seed.bytedance.com/en/blog/one-take-creation-flexible-referencing-introducing-seedance-2-5",
        scope: "素材职责、时间段案例与实际入口",
      },
      {
        title: "BytePlus · Seedance 2.5 Prompt Guide",
        url: "https://docs.byteplus.com/en/docs/ModelArk/2607689",
        scope: "本轮仅取得标题与更新时间，未将未读取正文作为规则依据",
      },
    ],
    officialNotes: [
      "模型页公布单段最长 30 秒、可延长两次，并列出白模控制等能力。能力上限不等同于任意平台都提供同样参数。来源：Seed 模型页。",
      "官方发布案例把白模运动/调度和最终外观素材分配不同职责；示例使用 @Image、@Video 等引用及时间区间组织镜头。即梦网页入口为视频生成后选择 Seedance 2.5。来源：Seed 发布页。",
    ],
    workflow: [
      "在导演台选 Seedance 2.5；时间轴保留完整运镜、角色位置与首尾构图。",
      "助手先制作最终首尾图，再把几何轨迹翻译成中文镜头语言与阶段状态。",
      "用户在即梦或实际使用的入口选择该模型和对应的首尾帧/参考模式，绑定最终素材。",
      "将提示词中的素材名称替换成该平台实际生成的引用标记；未上传的参考视频不要写入提示词。",
      "选择平台允许的时长和画幅，检查音频要求后生成；超过单段能力时由助手给出分段方案。",
    ],
    sections: [
      {
        title: "本项目归纳：保留此前导演写作框架",
        text: "素材各自负责什么 → 人物与建筑空间关系 → 按时间段描述动作 → 摄影机路径 → 声音 → 不变量。此前对话中的经验性写法作为工作方法保留，参数和上限仅采用本轮核验的官方资料。",
      },
      {
        title: "本项目模板：首尾帧与时间轴",
        text: "首图锁定初始站位、机位与画面边界，尾图锁定结束构图。每段说明开始状态、主体动作、摄影机和结束状态；下一段接续上一段。镜头从人物正前方横移到侧前方时，应写出路径与最终相对角度，不能只写“向左”。",
      },
      {
        title: "本项目模板：素材隔离",
        text: "角色参考只负责身份、服装、比例；场景参考负责建筑、材质和光线；运镜参考只负责机位、速度与调度。若后续补充白模视频，要显式说明不继承占位材质。当前导演台只导出静帧与数据，不能冒充已有白模视频。",
      },
      {
        title: "本项目模板：声音与连续性",
        text: "对白、环境声与音乐分别说明；有对白时标清说话者。写清可改变的是视角和动作，保持的是角色身份、人数、道具归属、建筑结构与空间轴线。精确字幕交给后期，不靠形容词堆叠保证。",
      },
      ...common,
    ],
    template:
      "【任务】依据最终首尾图生成{时长}秒的连续镜头。\n【素材职责】{平台内首图引用}=起始构图；{尾图引用}=结束构图；{角色/场景参考}=外观与空间依据。只列已经实际上传的素材。\n【空间关系】{人物相对位置、朝向、前后景}。\n【时间轴】0–{节点1}s：{主体动作、镜头路径、阶段结束状态}；接续至{总时长}s：{最终构图}。\n【摄影机】{起始机位、运动方向、景别变化、结束机位}。\n【声音】{环境声、音乐、对白要求}。\n【不变量】{人物身份、服装、道具归属、建筑、光照方向}。",
    assistantInstruction:
      "用户已选择 Seedance 2.5，无需再询问模型。按素材职责、空间关系、时间轴、摄影机、声音和不变量组织中文成品提示词，并制作最终首尾图。素材引用以实际平台槽位为准；不能凭空补入参考视频或未经核验的 API 参数。",
  },
};
export function isVideoModelId(value: unknown): value is VideoModelId {
  return value === "omni-flash-1.1" || value === "seedance-2.5";
}
export function modelGuide(profile: ModelProfile): string {
  return `# ${profile.name} · 导演台提示词与使用指南\n\n核验日期：${profile.checkedAt}；资料版本：${profile.version}\n\n${profile.summary}\n\n## 官方资料核验摘要\n\n${profile.officialNotes.map((n) => "- " + n).join("\n")}\n\n## 本项目推荐操作步骤\n\n${profile.workflow.map((n, i) => `${i + 1}. ${n}`).join("\n")}\n\n## 交付语言\n\n${profile.language}\n\n${profile.sections.map((s) => `## ${s.title}\n\n${s.text}`).join("\n\n")}\n\n## 原创制作模板\n\n${profile.template}\n\n## 交接给助手\n\n${profile.assistantInstruction}\n\n## 来源与证据范围\n\n${profile.sources.map((s) => `- [${s.title}](${s.url})：${s.scope}`).join("\n")}\n\n资料为上述日期的离线整理；不是全网穷尽收录，也没有把社区演示当成实测。本应用不调用视频生成 API。\n`;
}
