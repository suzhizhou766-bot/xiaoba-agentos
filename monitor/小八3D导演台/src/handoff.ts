import { exportShot } from "./exportPlan";
import { compile, describeLighting, type Project, type Shot } from "./core";
import { modelProfiles, modelGuide } from "./modelProfiles";
// Shared by the UI and tests so all exported formats use the same model selection.
export function handoffTextFiles(
  project: Project,
  source: Shot,
  videoFileName: string | null = "camera-motion.mp4",
): Record<string, string> {
  const shot=exportShot(source);
  if (!shot.videoModel) throw Error("请先选择视频模型");
  if (shot.out <= shot.in) throw Error("尾帧必须晚于首帧，请先调整标记范围");
  const profile = modelProfiles[shot.videoModel];
  const result = compile(project, shot);
  return {
    "handoff.txt": result.text,
    "handoff.json": JSON.stringify(result.json, null, 2),
    "source-project.xbdirector.json": JSON.stringify(project, null, 2),
    "project.xbdirector.json": JSON.stringify({...project,shots:project.shots.map(s=>s.id===shot.id?shot:s)}, null, 2),
    "video-prompt.zh.txt": videoPrompt(project, source, videoFileName),
    "export-review.json": JSON.stringify({range:[shot.in,shot.out],duration:shot.out-shot.in,scalePolicy:shot.scaleMotion?"允许尺寸动画":"固定首键尺寸",sourceScaleChanges:source.objects.flatMap(o=>{const keys=source.keys.filter(k=>k.target===o.id&&k.scale!==undefined);return new Set(keys.map(k=>k.scale)).size>1?[{name:o.name,keys:keys.map(k=>({time:k.time,scale:k.scale}))}]:[];}),spatial:result.json.空间导演记录},null,2),
    "model-guide.md": modelGuide(profile),
    "model-profile.json": JSON.stringify(profile, null, 2),
    "sources.json": JSON.stringify(
      {
        checkedAt: profile.checkedAt,
        version: profile.version,
        sources: profile.sources,
      },
      null,
      2,
    ),
    "prompt-writing-task.txt": `${profile.assistantInstruction}\n\n${profile.template}\n\n镜头：${shot.name}\n选定区间：${shot.in}–${shot.out}s。请把投递提示词时间归零，以选定区间首帧为 0s；依据 handoff.json 中的运镜和站位采样填写，而非编造运动。\n原始镜头时长不是视频平台参数。超出实际入口限制时给出分段方案，不擅自加速或裁掉用户设计。\n这是交给助手的制作任务，不是已完成的视频提示词。`,
    "READ-ME.txt": `已选视频模型：${profile.name}\n用户已在导演台指定模型，不要再次要求用户说明。\n先读 handoff.txt、model-guide.md 与 prompt-writing-task.txt。场景原图位于 scene-images/，逐张用途见 handoff.json 的场景图片字段；背景板固定在世界空间，内部景物未自动重建。空间导演记录包含投影占比、入画出画、遮挡候选和背景板越界区间；不要把越界区域当作已知场景。motion-1/2/3.png 是区间四分点的中间构图，时间见 intermediate-frames.json；不保证捕获所有短暂事件。\nfirst-frame-blocking.png 和 last-frame-blocking.png 是三维构图参考，不是最终生成首尾帧。\n由助手结合用户外观素材生成 final-first.png、final-last.png 和该模型的视频提示词，由用户提交视频平台。\n模型指南版本：${profile.version}；核验日期：${profile.checkedAt}。模型文档与第三方平台能力可能不同；不臆造平台专用标记。\n本包没有自动发送到任何平台。`,
  };
}

export function cleanProductionPrompt(
  project: Project,
  source: Shot,
  videoFileName: string | null = "camera-motion.mp4",
): string {
  const shot = exportShot(source),
    doc = compile(project, source).json;
  const spatial = doc.空间导演记录;
  const isSeedance = shot.videoModel === "seedance-2.5";
  const durationStr = (shot.out - shot.in).toFixed(3);
  const brief =
    shot.visualBrief?.trim() || "人物外观依参考素材保持一致，动作自然舒展";
  const briefEn =
    shot.visualBrief?.trim() ||
    "Characters consistent with reference images, natural motion";

  const macroCamZh =
    spatial.宏观运镜总结 ||
    "真实三维空间物理连续单镜头运镜，景物自然视差透视";
  const macroCamEn =
    spatial.宏观运镜总结En ||
    "Smooth continuous single shot with realistic spatial perspective";

  const subjectsZh = (
    spatial.影视主体动态Zh && spatial.影视主体动态Zh.length > 0
      ? spatial.影视主体动态Zh
      : spatial.宏观主体动态 || []
  ).join("；");
  const subjectsEn = (
    spatial.影视主体动态En && spatial.影视主体动态En.length > 0
      ? spatial.影视主体动态En
      : spatial.宏观主体动态 || []
  ).join(". ");

  const lightingDesc = describeLighting(shot.lighting);

  if (isSeedance) {
    return [
      `【Seedance 2.5 成品提示词（可直接复制投递）】`,
      `════════════════════════════════════════════════════════════════`,
      `【工作流 A：首尾帧生视频模式 (First & Last Frame)】`,
      `单个连续镜头，设计时长 ${durationStr} 秒。`,
      `@Image1 为起始构图，@Image2 为结尾构图。`,
      `[机位运镜] ${macroCamZh}。真实三维物理透视，背景与物体产生自然视差。`,
      `[光影与布光] ${lightingDesc.detailZh}`,
      `[主体与构图] ${brief}。${subjectsZh}。`,
      `[连续性与不变量] ${shot.scaleMotion ? "按指定尺寸动画执行。" : "主体保持真实人体比例，大小变化由机位与物理透视决定。"}面部、服装、光照与场景结构全局锁定，不切镜，无画面撕裂与多余停顿。`,
      ``,
      `【工作流 B：单图/多参考图生视频模式 (Image-to-Video)】`,
      `单个连续镜头，设计时长 ${durationStr} 秒。`,
      `以 @Image1 为起始画面与主体外观参考，画面按指定摄影机轨迹平滑展开。`,
      `[机位运镜] ${macroCamZh}。真实三维物理透视，背景与物体产生自然视差。`,
      `[光影与布光] ${lightingDesc.detailZh}`,
      `[主体与构图] ${brief}。${subjectsZh}。`,
      `[连续性与不变量] 主体面部、服装、光照与场景结构全局锁定，严禁跳切。`,
      ``,
      ...(videoFileName ? [
      `【工作流 C：相机运动视频引导模式 (Video-to-Video / Camera Guidance)】`,
      `以随包导出的 ${videoFileName} 作为精确摄影机轨迹参考，@Image1 为角色与场景外观基准。`,
      `[机位运镜] 严格遵循参考视频运镜轨迹（${macroCamZh}）。`,
      `[光影与布光] ${lightingDesc.detailZh}`,
      `[主体与构图] ${brief}。${subjectsZh}。`,
      `[连续性与不变量] 全局锁定角色特征与光影，严禁跳切。`,
      ] : ["未生成机位视频，相机运动视频引导不可用；请使用首尾帧或图片参考工作流。"]),
    ].join("\n");
  }

  return [
    `【Gemini Omni 1.1 Flash 成品提示词（可直接复制投递）】`,
    `════════════════════════════════════════════════════════════════`,
    `【Workflow 1: First & Last Frame Interpolation】`,
    `<FIRST_FRAME> to <LAST_FRAME> smooth continuous single shot (${durationStr}s).`,
    `Camera Movement: ${macroCamEn} with realistic spatial perspective and natural parallax.`,
    `Lighting & Atmosphere: ${lightingDesc.detailEn}`,
    `Subjects & Performance: ${briefEn}. ${subjectsEn}.`,
    `Invariants: ${shot.scaleMotion ? "Execute designated scaling." : "Maintain constant physical body proportions without artificial scaling."} Consistent character identities, environment geometry and lighting without jump cuts.`,
    ``,
    `【Workflow 2: Single Image / Multi-Reference to Video】`,
    `<IMAGE_REF_0> as initial frame and primary visual anchor, smooth continuous single shot (${durationStr}s).`,
    `Camera Movement: ${macroCamEn} with realistic spatial perspective and natural parallax.`,
    `Lighting & Atmosphere: ${lightingDesc.detailEn}`,
    `Subjects & Performance: ${briefEn}. ${subjectsEn}.`,
    `Invariants: Consistent character identities, environment geometry and lighting without jump cuts.`,
    ``,
    ...(videoFileName ? [
    `【Workflow 3: Camera Motion Video Guided (with ${videoFileName})】`,
    `<VIDEO_REF_0> (${videoFileName}) for exact camera trajectory, <IMAGE_REF_0> for character and scene appearance. Smooth continuous single shot (${durationStr}s).`,
    `Camera Movement: Follow the exact physical camera motion from reference video (${macroCamEn}).`,
    `Lighting & Atmosphere: ${lightingDesc.detailEn}`,
    `Subjects & Performance: ${briefEn}. ${subjectsEn}.`,
    `Invariants: Consistent character identities, environment geometry and lighting without jump cuts.`,
    ] : ["Camera guidance unavailable: no motion video was generated. Use the frame or image workflows above."]),
  ].join("\n");
}

export function videoPrompt(
  project: Project,
  source: Shot,
  videoFileName: string | null = "camera-motion.mp4",
) {
  const shot = exportShot(source),
    doc = compile(project, source).json;
  const spatial = doc.空间导演记录;
  const clean = cleanProductionPrompt(project, source, videoFileName);
  const warnings = [
    ...spatial.越界区间.map(
      (r) =>
        `原始参考覆盖不足：${(r.from - shot.in).toFixed(3)}–${(r.to - shot.in).toFixed(3)}秒；不要将灰黑边缘生成成真实布景，需先补齐参考或调整取景。`,
    ),
    ...doc.尾帧反推.主体构图
      .filter((o) => o.构图提示 !== "画内主体")
      .map(
        (o) =>
          `尾帧 ${o.名称}：${o.构图提示}，按导演构图保留裁切，不强行塞入全身。`,
      ),
  ];

  return [
    clean,
    `\n════════════════════════════════════════════════════════════════`,
    `【导演台安全与空间审计（供创作者核对，请勿直接填入生成框）】`,
    `════════════════════════════════════════════════════════════════`,
    `目标模型：${doc.视频模型.名称}`,
    `镜头设计时长：${(shot.out - shot.in).toFixed(3)} 秒。平台可选时长需另行匹配，不自动补成 8 秒。`,
    `\n【素材职责与使用约束】`,
    `场景原图用于环境、建筑、材质与光照；白模图仅用于站位和构图，不复制白模外观。先用一致人物外观参考制作最终首尾帧，再绑定到视频平台的首尾槽位。中间图按 intermediate-frames.json 的时间辅助说明运动。`,
    `\n【细粒度分段运镜记录 (每0.5秒)】`,
    spatial.运镜与画面描述
      .map(
        (r) =>
          `${(r.开始秒 - shot.in).toFixed(2)}–${(r.结束秒 - shot.in).toFixed(2)}秒：${r.描述}`,
      )
      .join("\n"),
    `\n【制作前需核对的安全警告】`,
    warnings.join("\n") ||
      "未检测到背景板覆盖缺口；这不证明图片内部深度已经重建。",
    `\n以上是完整导演交接提示词说明文档；若有背景越界或未定外观，需先处理后再执行生成。`,
  ].join("\n");
}
