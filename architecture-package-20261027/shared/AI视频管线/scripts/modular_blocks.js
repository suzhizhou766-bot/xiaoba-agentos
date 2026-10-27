/**
 * 模块化活动快剪 Block 系统 v2 — 多幕叙事架构
 * 
 * 核心理念：15s 是单高潮线性结构，更长时长需要「多幕叙事」
 * 不是多加 Block，而是用 Act（幕）组织 Block 的重复与变奏
 */

// ============================================================
// 一、Block 类型目录（同 v1，不变）
// ============================================================

const BLOCK_TYPES = {
  env_wide:   { id:"env_wide",   name:"环境全景",   cat:"建场", dur:2.5 },
  env_detail: { id:"env_detail", name:"环境细节",   cat:"建场", dur:2.0 },
  crowd_wide: { id:"crowd_wide", name:"人群热闹",   cat:"人气", dur:2.5 },
  crowd_reaction: { id:"crowd_reaction", name:"人群反应", cat:"人气", dur:1.5 },
  branding:   { id:"branding",   name:"品牌/签到",   cat:"信息", dur:2.0 },
  speech:     { id:"speech",     name:"致辞/讲话",   cat:"信息", dur:3.0 },
  highlight_action:  { id:"highlight_action",  name:"高光动作", cat:"高潮", dur:2.5 },
  highlight_emotion: { id:"highlight_emotion", name:"高光情绪", cat:"高潮", dur:1.5 },
  group_photo:{ id:"group_photo",name:"集体合影",   cat:"收束", dur:2.0 },
  logo_outro: { id:"logo_outro", name:"主海报落版", cat:"收束", dur:2.5 },
  bts_montage:{ id:"bts_montage",name:"幕后花絮",   cat:"收束", dur:2.5 },
};

// ============================================================
// 二、Act（幕）类型
// ============================================================

/**
 * Act 是 Block 的容器，定义了这一段落的叙事功能和情绪基调
 * 不同 Act 可以包含相同类型的 Block，但节奏和强调点不同
 */
const ACT_TYPES = {
  HOOK: {
    name: "钩子·建场",
    function: "抓住注意力，建立空间认知",
    energy: "低→缓升",
    pace: "slow",        // 慢节奏，给观众呼吸
    hold: "long",        // 每个 Block 多停留
    transition: "dissolve", // 柔过渡
    typical_blocks: ["env_detail", "env_wide"],
    duration_range: [4, 10],
  },
  BUILD: {
    name: "推进·展开",
    function: "展示活动过程，发展叙事线",
    energy: "中→升",
    pace: "medium",
    hold: "medium",
    transition: "crossfade",
    typical_blocks: ["crowd_wide", "branding", "speech", "crowd_reaction"],
    duration_range: [8, 16],
  },
  PEAK: {
    name: "高潮·引爆",
    function: "情绪与视觉的双重顶点",
    energy: "高→最高",
    pace: "fast",        // 快节奏卡点
    hold: "short",       // 快切
    transition: "hard_cut", // 硬切
    typical_blocks: ["highlight_action", "highlight_emotion", "crowd_reaction"],
    duration_range: [5, 12],
  },
  REFLECT: {
    name: "回落·沉淀",
    function: "高潮后的情绪消化，植入品牌记忆",
    energy: "降→静",
    pace: "slow",
    hold: "long",
    transition: "dissolve",
    typical_blocks: ["group_photo", "bts_montage", "logo_outro"],
    duration_range: [5, 12],
  },
  BRAND: {
    name: "品牌·落版",
    function: "纯粹的品牌时刻，让观众带走一个记忆点",
    energy: "静→收",
    pace: "static",
    hold: "very_long",
    transition: "fade",
    typical_blocks: ["logo_outro"],
    duration_range: [3, 8],
  },
};

// ============================================================
// 三、时长 → Act 序列 → Block 展开
// ============================================================

/**
 * 设计原则：
 * 
 * 15s = HOOK+PEAK（一段式：建场即高潮）
 * 35s = HOOK→BUILD→PEAK→REFLECT（两段式：起承转合）
 * 45s = HOOK→BUILD→PEAK→BUILD→REFLECT→BRAND（三段式：双波峰）
 * 55s = HOOK→BUILD→PEAK→BUILD→PEAK→REFLECT→BRAND（叙事长片：双高潮）
 */

const DURATION_PRESETS = {
  "15s": {
    label: "15秒快剪（微短片）",
    description: "一闪而过，只给观众一个「这个活动很棒」的印象",
    acts: [
      { type:"HOOK",   pct:[0,25],   blocks:["env_wide","crowd_wide","branding"] },
      { type:"PEAK",   pct:[25,75],  blocks:["highlight_action","highlight_emotion","crowd_reaction"] },
      { type:"BRAND",  pct:[75,100], blocks:["logo_outro"] },
    ],
    energy_curve: "低↗中↗高↘收",
  },

  "35s": {
    label: "35秒活动短片",
    description: "有叙事感，给建场和情绪各留呼吸空间",
    acts: [
      { type:"HOOK",    pct:[0,20],   blocks:["env_detail","env_wide"] },
      { type:"BUILD",   pct:[20,50],  blocks:["crowd_wide","branding","speech"] },
      { type:"PEAK",    pct:[50,75],  blocks:["highlight_action","highlight_emotion","crowd_reaction"] },
      { type:"REFLECT", pct:[75,100], blocks:["group_photo","logo_outro"] },
    ],
    energy_curve: "低↗中↗高↘静",
  },

  "45s": {
    label: "45秒品牌活动片",
    description: "三段式双波峰结构：展示→高潮→沉淀→再升→品牌",
    acts: [
      { type:"HOOK",    pct:[0,18],   blocks:["env_detail","env_wide"] },
      { type:"BUILD",   pct:[18,42],  blocks:["crowd_wide","branding","speech","crowd_reaction"] },
      { type:"PEAK",    pct:[42,62],  blocks:["highlight_action","highlight_emotion"] },
      { type:"BUILD",   pct:[62,78],  blocks:["crowd_reaction","bts_montage"] },
      { type:"REFLECT", pct:[78,92],  blocks:["group_photo"] },
      { type:"BRAND",   pct:[92,100], blocks:["logo_outro"] },
    ],
    energy_curve: "低↗中↗高↘中↗高↘静",
  },

  "55s": {
    label: "55秒品牌叙事片",
    description: "完整叙事弧线：世界→故事→第一次高潮→转折→更大高潮→余韵→品牌",
    acts: [
      { type:"HOOK",    pct:[0,16],   blocks:["env_detail","env_wide"] },
      { type:"BUILD",   pct:[16,36],  blocks:["crowd_wide","branding","speech"] },
      { type:"PEAK",    pct:[36,52],  blocks:["highlight_action","crowd_reaction"] },
      { type:"BUILD",   pct:[52,70],  blocks:["highlight_emotion","crowd_wide","speech"] },
      { type:"PEAK",    pct:[70,84],  blocks:["highlight_action","highlight_emotion","crowd_reaction"] },
      { type:"REFLECT", pct:[84,95],  blocks:["bts_montage","group_photo"] },
      { type:"BRAND",   pct:[95,100], blocks:["logo_outro"] },
    ],
    energy_curve: "低↗中↗高↘中↗更高↘静↘收",
  },
};

// ============================================================
// 四、展开引擎：Act 序列 → 精确时间轴
// ============================================================

function expandTimeline(durationKey, totalSeconds = null) {
  const preset = DURATION_PRESETS[durationKey];
  if (!preset) throw new Error(`未知时长: ${durationKey}`);

  const total = totalSeconds || parseInt(durationKey);
  const sections = [];
  let t = 0;

  for (let i = 0; i < preset.acts.length; i++) {
    const act = preset.acts[i];
    const actInfo = ACT_TYPES[act.type];
    const [pctStart, pctEnd] = act.pct;
    const actStart = (pctStart / 100) * total;
    const actEnd = (pctEnd / 100) * total;
    const actDuration = actEnd - actStart;

    // 在 Act 内均匀分配 Block
    const blocks = act.blocks;
    const rawTotal = blocks.reduce((s, id) => s + (BLOCK_TYPES[id]?.dur || 2), 0);
    const scale = actDuration / rawTotal;

    let actT = actStart;
    for (const blockId of blocks) {
      const block = BLOCK_TYPES[blockId];
      const rawDur = block?.dur || 2;
      const dur = rawDur * scale;
      const end = actT + dur;

      sections.push({
        act: i + 1,
        act_type: act.type,
        act_name: actInfo.name,
        block_id: blockId,
        block_name: block?.name || blockId,
        block_cat: block?.cat || "?",
        start: actT.toFixed(1),
        end: end.toFixed(1),
        duration: dur.toFixed(1),
        pace: actInfo.pace,
        transition: i === sections.length - 1 ? "fade_out" : actInfo.transition,
        energy: actInfo.energy.split("→")[0],
        bpm_cue: ["intro","build","drop","release","outro"][Math.min(i, 4)],
      });

      actT = end;
    }

    // Act 间留 0.3s 呼吸缝（最后一个 act 不留）
    if (i < preset.acts.length - 1) {
      actT += 0.3;
    }
  }

  return {
    preset: preset.label,
    description: preset.description,
    total_duration: total,
    energy_curve: preset.energy_curve,
    act_count: preset.acts.length,
    section_count: sections.length,
    sections,
    act_summary: preset.acts.map((a, i) => ({
      act: i + 1,
      type: a.type,
      name: ACT_TYPES[a.type].name,
      function: ACT_TYPES[a.type].function,
      blocks: a.blocks.map(id => BLOCK_TYPES[id]?.name || id),
      pct: `${a.pct[0]}%-${a.pct[1]}%`,
    })),
  };
}

// ============================================================
// 五、BGM 五段结构映射
// ============================================================

function mapBGMStructure(durationKey, totalSeconds) {
  const timeline = expandTimeline(durationKey, totalSeconds);
  const total = timeline.total_duration;

  // 传统 BGM 五段：铺垫→推进→高潮→卸力→收束
  const bgmStages = [
    { stage:"铺垫", pct:[0,20],  desc:"轻柔氛围进入，不给满，留悬念" },
    { stage:"推进", pct:[20,45], desc:"节奏感逐步建立，引入打击/律动元素" },
    { stage:"高潮", pct:[45,72], desc:"主旋律全开，能量最高点，可叠加人声" },
    { stage:"卸力", pct:[72,90], desc:"能量回落，旋律收窄，留情绪空间" },
    { stage:"收束", pct:[90,100],desc:"尾音干净收尾，不拖泥带水" },
  ];

  return bgmStages.map(s => ({
    stage: s.stage,
    time: `${(s.pct[0]/100*total).toFixed(1)}s-${(s.pct[1]/100*total).toFixed(1)}s`,
    covers_acts: timeline.act_summary
      .filter(a => {
        const aStart = parseInt(a.pct);
        const aEnd = parseInt(a.pct.split("-")[1]);
        return !(aEnd <= s.pct[0] || aStart >= s.pct[1]);
      })
      .map(a => `${a.type}-${a.name}`),
    desc: s.desc,
  }));
}

// ============================================================
// 六、可视化输出
// ============================================================

function visualize(timeline) {
  const lines = [];
  const total = timeline.total_duration;
  const width = 60; // 字符宽度

  lines.push(`\n🎬 ${timeline.preset}`);
  lines.push(`   ${timeline.description}`);
  lines.push(`   能量曲线: ${timeline.energy_curve}`);
  lines.push(`   总时长: ${total}s | ${timeline.act_count}幕 | ${timeline.section_count}个卡点`);
  lines.push("");

  // 能量曲线可视化
  lines.push("  能量 ┃");
  const energyLevels = { HOOK:1, BUILD:2, PEAK:3, REFLECT:1, BRAND:0 };
  const bar = timeline.sections.map(s => {
    const lvl = energyLevels[s.act_type] || 1;
    return lvl === 3 ? "█" : lvl === 2 ? "▓" : lvl === 1 ? "▒" : "░";
  }).join("");

  // 按比例填充
  let viz = "";
  for (const s of timeline.sections) {
    const w = Math.max(1, Math.round(parseFloat(s.duration) / total * width));
    const lvl = energyLevels[s.act_type] || 1;
    const ch = lvl === 3 ? "█" : lvl === 2 ? "▓" : lvl === 1 ? "▒" : "░";
    viz += ch.repeat(w);
  }
  lines.push(`       ┃${viz}`);
  lines.push("");

  // Act 分隔
  let lastAct = 0;
  for (const s of timeline.sections) {
    if (s.act !== lastAct) {
      const actName = ACT_TYPES[s.act_type]?.name || "";
      lines.push(`  ┌─ 第${s.act}幕: ${actName} (${s.act_type}) ─────────────────────┐`);
      lastAct = s.act;
    }
    const durBar = "●".repeat(Math.max(1, Math.round(parseFloat(s.duration))));
    lines.push(`  │  ${s.start.padStart(5)}s ${durBar} ${s.block_name.padEnd(8)} ${s.end.padStart(5)}s  [${s.pace}]`);
  }
  lines.push(`  └──────────────────────────────────────────────────────┘`);
  lines.push("");

  return lines.join("\n");
}

// ============================================================
// 七、事件类型 × 时长组合
// ============================================================

const EVENT_VARIANTS = {
  corporate: {
    "15s": { blocks_override: null }, // 用默认
    "35s": { blocks_override: null },
    "45s": {
      // 企业活动45s：加重致辞和品牌
      acts_override: [
        { type:"HOOK",   pct:[0,15],   blocks:["env_wide","branding"] },
        { type:"BUILD",  pct:[15,40],  blocks:["crowd_wide","speech","crowd_reaction"] },
        { type:"PEAK",   pct:[40,58],  blocks:["highlight_action","highlight_emotion"] },
        { type:"BUILD",  pct:[58,75],  blocks:["crowd_reaction","speech"] },
        { type:"REFLECT",pct:[75,90],  blocks:["group_photo"] },
        { type:"BRAND",  pct:[90,100], blocks:["logo_outro"] },
      ],
    },
    "55s": {
      acts_override: [
        { type:"HOOK",   pct:[0,14],   blocks:["env_detail","env_wide"] },
        { type:"BUILD",  pct:[14,32],  blocks:["crowd_wide","branding"] },
        { type:"BUILD",  pct:[32,50],  blocks:["speech","crowd_reaction"] },
        { type:"PEAK",   pct:[50,66],  blocks:["highlight_action","highlight_emotion"] },
        { type:"BUILD",  pct:[66,80],  blocks:["crowd_reaction","speech"] },
        { type:"REFLECT",pct:[80,93],  blocks:["group_photo","bts_montage"] },
        { type:"BRAND",  pct:[93,100], blocks:["logo_outro"] },
      ],
    },
  },
  kids: {
    "35s": {
      // 儿童活动：更快节奏，去掉致辞，加重花絮
      acts_override: [
        { type:"HOOK",   pct:[0,18],   blocks:["env_detail","env_wide"] },
        { type:"BUILD",  pct:[18,42],  blocks:["crowd_wide","highlight_action"] },
        { type:"PEAK",   pct:[42,65],  blocks:["highlight_emotion","crowd_reaction"] },
        { type:"BUILD",  pct:[65,82],  blocks:["highlight_action","bts_montage"] },
        { type:"REFLECT",pct:[82,100], blocks:["group_photo","logo_outro"] },
      ],
    },
    "45s": {
      acts_override: [
        { type:"HOOK",   pct:[0,16],   blocks:["env_detail","env_wide"] },
        { type:"BUILD",  pct:[16,38],  blocks:["crowd_wide","branding","highlight_action"] },
        { type:"PEAK",   pct:[38,60],  blocks:["highlight_action","highlight_emotion","crowd_reaction"] },
        { type:"BUILD",  pct:[60,78],  blocks:["bts_montage","crowd_reaction"] },
        { type:"REFLECT",pct:[78,100], blocks:["group_photo","logo_outro"] },
      ],
    },
    "55s": {
      acts_override: [
        { type:"HOOK",   pct:[0,14],   blocks:["env_detail","env_wide"] },
        { type:"BUILD",  pct:[14,30],  blocks:["crowd_wide","highlight_action"] },
        { type:"PEAK",   pct:[30,48],  blocks:["highlight_action","crowd_reaction"] },
        { type:"BUILD",  pct:[48,65],  blocks:["highlight_emotion","crowd_wide"] },
        { type:"PEAK",   pct:[65,80],  blocks:["highlight_action","highlight_emotion"] },
        { type:"REFLECT",pct:[80,95],  blocks:["bts_montage","group_photo"] },
        { type:"BRAND",  pct:[95,100], blocks:["logo_outro"] },
      ],
    },
  },
  wedding: {
    "35s": {
      acts_override: [
        { type:"HOOK",   pct:[0,20],   blocks:["env_detail","env_wide"] },
        { type:"BUILD",  pct:[20,45],  blocks:["crowd_wide","crowd_reaction"] },
        { type:"PEAK",   pct:[45,70],  blocks:["highlight_action","highlight_emotion"] },
        { type:"REFLECT",pct:[70,100], blocks:["crowd_reaction","group_photo","logo_outro"] },
      ],
    },
    "45s": {
      acts_override: [
        { type:"HOOK",   pct:[0,16],   blocks:["env_detail","env_wide"] },
        { type:"BUILD",  pct:[16,36],  blocks:["crowd_wide","crowd_reaction"] },
        { type:"PEAK",   pct:[36,56],  blocks:["highlight_action","highlight_emotion"] },
        { type:"BUILD",  pct:[56,75],  blocks:["crowd_reaction","highlight_emotion"] },
        { type:"REFLECT",pct:[75,100], blocks:["bts_montage","group_photo","logo_outro"] },
      ],
    },
    "55s": {
      acts_override: [
        { type:"HOOK",   pct:[0,14],   blocks:["env_detail","env_wide"] },
        { type:"BUILD",  pct:[14,32],  blocks:["crowd_wide","crowd_reaction"] },
        { type:"PEAK",   pct:[32,50],  blocks:["highlight_action","highlight_emotion"] },
        { type:"BUILD",  pct:[50,68],  blocks:["crowd_reaction","highlight_emotion"] },
        { type:"PEAK",   pct:[68,82],  blocks:["highlight_action","crowd_reaction"] },
        { type:"REFLECT",pct:[82,95],  blocks:["bts_montage","group_photo"] },
        { type:"BRAND",  pct:[95,100], blocks:["logo_outro"] },
      ],
    },
  },
};

function getVariant(eventType, durationKey) {
  const variant = EVENT_VARIANTS[eventType]?.[durationKey];
  const preset = DURATION_PRESETS[durationKey];
  if (!preset) throw new Error(`未知时长: ${durationKey}`);

  if (variant?.acts_override) {
    // 使用事件特化版本
    const total = parseInt(durationKey);
    const customPreset = { ...preset, acts: variant.acts_override };
    // 临时替换
    const orig = DURATION_PRESETS[durationKey];
    DURATION_PRESETS[durationKey] = customPreset;
    const result = expandTimeline(durationKey, total);
    DURATION_PRESETS[durationKey] = orig;
    return result;
  }
  return expandTimeline(durationKey);
}

// ============================================================
// 八、导出
// ============================================================

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    BLOCK_TYPES, ACT_TYPES, DURATION_PRESETS, EVENT_VARIANTS,
    expandTimeline, mapBGMStructure, visualize, getVariant,
  };
}

// CLI
if (import.meta.url === `file://${process.argv[1]}` || process.argv[1]?.endsWith("modular_blocks.js")) {
  const duration = process.argv[2] || "35s";
  const eventType = process.argv[3] || null;
  const totalSec = process.argv[4] ? parseInt(process.argv[4]) : null;

  let timeline;
  if (eventType && EVENT_VARIANTS[eventType]?.[duration]) {
    timeline = getVariant(eventType, duration);
  } else {
    timeline = expandTimeline(duration, totalSec);
  }

  console.log(visualize(timeline));

  if (process.argv.includes("--json")) {
    console.log(JSON.stringify(timeline, null, 2));
  }

  if (process.argv.includes("--bgm")) {
    const bgm = mapBGMStructure(duration, timeline.total_duration);
    console.log("\n🎵 BGM 五段结构映射:");
    bgm.forEach(s => {
      console.log(`   ${s.stage} [${s.time}] → ${s.covers_acts.join(", ")}`);
      console.log(`      ${s.desc}`);
    });
  }
}
