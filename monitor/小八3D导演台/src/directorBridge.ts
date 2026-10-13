// 与监看中心里另外两块打通：
// 1) 把当前镜头换算成「提示词导演台」参数面板认识的值，经同源的本地存储送过去（不走网络）；
// 2) 让监看中心后台用 Key 池里的模型，把纯净提示词写成所选视频模型的最终提示词。
import {
  cameraTarget,
  defaultLighting,
  sampledShot,
  type Project,
  type Shot,
} from "./core";
import { exportShot } from "./exportPlan";
import { cleanProductionPrompt } from "./handoff";
import { modelGuide, modelProfiles } from "./modelProfiles";

export const HANDOFF_KEY = "xiaoba.stage.handoff";

/** 提示词导演台「反推回填」入口接受的字段；值必须是它下拉框选项的内部取值，否则那一项会被忽略。 */
export type DirectorParams = {
  suggested_model?: string;
  scene_desc_en?: string;
  time_of_day?: string;
  lens_type: string;
  focal: string;
  aperture: string;
  shot_size?: string;
  cam_height: string;
  cam_angle: string;
  cam_move: string;
  stability: string;
  light_source?: string;
  light_ratio: string;
  light_dir: string;
  has_person: boolean;
  person?: { count?: string; action?: string };
};
export type DirectorHandoff = {
  at: number;
  project: string;
  shot: string;
  params: DirectorParams;
  notes: string[];
};

/** 提示词导演台下拉框：显示的文字 → 内部取值（抄自 director.html；它改了选项，这里和测试要跟着改）。 */
export const DIRECTOR_CODES: Record<string, Record<string, string>> = {
  "focal": {
    "14 mm": "14",
    "18 mm": "18",
    "24 mm": "24",
    "28 mm": "28",
    "35 mm": "35",
    "50 mm": "50",
    "70 mm": "70",
    "85 mm": "85",
    "100 mm": "100",
    "135 mm": "135",
    "200 mm": "200",
    "400 mm": "400"
  },
  "aperture": {
    "f/1.4": "f/1.4",
    "f/1.8": "f/1.8",
    "f/2.8": "f/2.8",
    "f/4": "f/4",
    "f/5.6": "f/5.6",
    "f/8": "f/8",
    "f/11": "f/11"
  },
  "shot_size": {
    "大远景(环境主体)": "extreme_wide",
    "远景(人物全身+环境)": "wide_shot",
    "全景(全身)": "full_shot",
    "中景(腰部以上)": "medium_shot",
    "近景(胸部以上)": "close_up",
    "特写(面部)": "extreme_close",
    "大特写(眼睛/材质)": "macro_close"
  },
  "cam_height": {
    "地面(贴地)": "ground",
    "低机位(膝高)": "low",
    "腰平(hip level)": "hip",
    "眼平": "eye",
    "高机位(俯视)": "high",
    "鸟瞰": "bird"
  },
  "cam_angle": {
    "平视": "level",
    "仰角(力量感)": "low_angle",
    "俯角(弱势/掌控)": "high_angle",
    "荷兰角(不安感)": "dutch"
  },
  "cam_move": {
    "固定机位": "static",
    "慢速推近": "push",
    "拉远": "pull",
    "横移": "truck",
    "摇镜": "pan",
    "环绕": "orbit",
    "手持跟随": "handheld_follow",
    "升降": "crane"
  },
  "stability": {
    "完全稳定(三脚架/滑轨)": "locked",
    "轻微手持(呼吸感)": "slight",
    "纪实手持(跟随感)": "docu_handheld"
  },
  "light_source": {
    "自然日光": "sun",
    "黄金时刻低角度太阳": "golden",
    "黄昏天光": "sunset",
    "夜景城市灯光": "night_urban",
    "室内照明(筒灯/吊灯)": "indoor",
    "逆光剪影(contre-jour)": "backlit",
    "阴天漫射": "overcast_soft",
    "月光": "moon"
  },
  "light_ratio": {
    "低光比(柔和商业感)": "low",
    "中光比(自然平衡)": "mid",
    "高光比(电影感强对比)": "high"
  },
  "light_dir": {
    "侧光(立体感)": "side",
    "顺光(平)": "front",
    "逆光/侧逆光(轮廓光)": "back",
    "顶光": "top"
  },
  "lens_type": {
    "超广角(107° 环境沉浸)": "ultrawide",
    "广角(84° 空间叙事)": "wide",
    "标准(47° 人眼自然)": "normal",
    "中长焦(29° 人像)": "shorttele",
    "长焦(18° 压缩特写)": "tele",
    "超长焦(8° 远处观察)": "supertele",
    "微距(材质细节)": "macro"
  },
  "time_of_day": {
    "清晨(冷调柔光)": "morning",
    "白天(自然光)": "day",
    "黄金时刻(低角度暖光)": "golden",
    "蓝调时刻(黄昏余晖+蓝色)": "bluehour",
    "夜晚(人工光源)": "night",
    "阴天(漫射柔光)": "overcast"
  },
  "count": {
    "1 人": "1",
    "2 人": "2",
    "3 人": "3"
  }
};
const code = (field: string, label: string) => DIRECTOR_CODES[field]?.[label] ?? label;

const FOCALS = [14, 18, 24, 28, 35, 50, 70, 85, 100, 135, 200, 400];
const APERTURES = [1.4, 1.8, 2.8, 4, 5.6, 8, 11];
const nearest = (list: number[], value: number) =>
  list.reduce((best, x) => (Math.abs(x - value) < Math.abs(best - value) ? x : best));

function lensType(focal: number) {
  if (focal <= 16) return "超广角(107° 环境沉浸)";
  if (focal <= 30) return "广角(84° 空间叙事)";
  if (focal <= 60) return "标准(47° 人眼自然)";
  if (focal <= 100) return "中长焦(29° 人像)";
  if (focal <= 250) return "长焦(18° 压缩特写)";
  return "超长焦(8° 远处观察)";
}
const SHOT_SIZES: Record<string, string> = {
  ELS: "大远景(环境主体)",
  LS: "远景(人物全身+环境)",
  FS: "全景(全身)",
  MWS: "全景(全身)",
  MS: "中景(腰部以上)",
  MCU: "近景(胸部以上)",
  CU: "特写(面部)",
  ECU: "大特写(眼睛/材质)",
};
function camHeight(y: number) {
  if (y < 0.25) return "地面(贴地)";
  if (y < 0.75) return "低机位(膝高)";
  if (y < 1.25) return "腰平(hip level)";
  if (y < 1.85) return "眼平";
  if (y < 4) return "高机位(俯视)";
  return "鸟瞰";
}
const VIDEO_MODELS: Record<string, string> = {
  "omni-flash-1.1": "geminiomniflash",
  "seedance-2.5": "seedance2",
};

/** 首尾两个机位之间是哪种运镜。只看起点和终点，中间的轨迹细节仍在交接包里。 */
function camMove(shot: Shot) {
  const a = sampledShot(shot, shot.in),
    b = sampledShot(shot, shot.out);
  const pa = a.camera.position,
    pb = b.camera.position;
  const ta = cameraTarget(a.camera, a.objects),
    tb = cameraTarget(b.camera, b.objects);
  const dist = (p: number[], q: number[]) => Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]);
  const moved = dist(pa, pb),
    targetMoved = dist(ta, tb);
  if (moved < 0.1) return targetMoved < 0.1 ? "固定机位" : "摇镜";
  const da = dist(pa, ta),
    db = dist(pb, tb);
  const horizontal = Math.hypot(pb[0] - pa[0], pb[2] - pa[2]);
  if (Math.abs(pb[1] - pa[1]) > horizontal) return "升降";
  const angle = (p: number[], t: number[]) => Math.atan2(p[0] - t[0], p[2] - t[2]);
  let swing = Math.abs(angle(pb, tb) - angle(pa, ta));
  if (swing > Math.PI) swing = 2 * Math.PI - swing;
  const change = (db - da) / Math.max(da, 0.01);
  if (Math.abs(change) < 0.15 && swing > Math.PI / 12 && targetMoved < moved / 2) return "环绕";
  if (change <= -0.15) return "慢速推近";
  if (change >= 0.15) return "拉远";
  return targetMoved > moved / 2 ? "手持跟随" : "横移";
}

export function toDirectorParams(project: Project, source: Shot, now = Date.now()): DirectorHandoff {
  const shot = exportShot(source);
  const first = sampledShot(shot, shot.in);
  const cam = first.camera;
  const notes: string[] = [];
  const focal = nearest(FOCALS, cam.focalLength),
    aperture = nearest(APERTURES, cam.aperture);
  if (focal !== Math.round(cam.focalLength * 10) / 10)
    notes.push(`焦距 ${Math.round(cam.focalLength * 10) / 10}mm 取最近的 ${focal}mm 档`);
  if (aperture !== cam.aperture) notes.push(`光圈 f/${cam.aperture} 取最近的 f/${aperture} 档`);

  const target = cameraTarget(cam, first.objects);
  const flat = Math.hypot(target[0] - cam.position[0], target[2] - cam.position[2]);
  const pitch = (Math.atan2(target[1] - cam.position[1], Math.max(flat, 0.001)) * 180) / Math.PI;
  const roll = Math.abs(((cam.roll ?? 0) * 180) / Math.PI);
  const camAngle =
    roll > 5 ? "荷兰角(不安感)" : pitch > 10 ? "仰角(力量感)" : pitch < -10 ? "俯角(弱势/掌控)" : "平视";

  const light = shot.lighting ?? defaultLighting();
  const azimuth = ((light.keyAzimuth % 360) + 360) % 360;
  const fromFront = Math.min(azimuth, 360 - azimuth); // 0 正前，180 正背
  const lightDir =
    light.keyElevation >= 70
      ? "顶光"
      : fromFront < 35
        ? "顺光(平)"
        : fromFront > 125
          ? "逆光/侧逆光(轮廓光)"
          : "侧光(立体感)";
  const ratio = light.fillEnabled && light.fillIntensity > 0 ? light.keyIntensity / light.fillIntensity : 99;
  const lightRatio =
    ratio < 2 ? "低光比(柔和商业感)" : ratio < 4 ? "中光比(自然平衡)" : "高光比(电影感强对比)";
  const lightSource: Partial<Record<string, string>> = {
    "golden-hour": "黄金时刻低角度太阳",
    "cool-night": "夜景城市灯光",
    "soft-window": "自然日光",
    "film-noir": "室内照明(筒灯/吊灯)",
  };
  const timeOfDay: Partial<Record<string, string>> = {
    "golden-hour": "黄金时刻(低角度暖光)",
    "cool-night": "夜晚(人工光源)",
  };

  const actors = first.objects.filter((o) => o.kind === "actor" && o.visible).length;
  if (actors > 3) notes.push(`画面里有 ${actors} 个角色，参数面板最多选 3 人，人数没有填`);
  if (!SHOT_SIZES[cam.framing]) notes.push("这个镜头没有用景别预设，景别没有填");
  if (shot.videoModel === "seedance-2.5") notes.push("提示词导演台里没有 Seedance 2.5，先选了 Seedance 2.0");
  notes.push("关键帧轨迹、空间越界检查、角色外观不在参数面板里，仍以交接包为准");

  const params: DirectorParams = {
    lens_type: lensType(cam.focalLength),
    focal: `${focal} mm`,
    aperture: `f/${aperture}`,
    cam_height: camHeight(cam.position[1]),
    cam_angle: camAngle,
    cam_move: camMove(shot),
    stability: "完全稳定(三脚架/滑轨)",
    light_ratio: lightRatio,
    light_dir: lightDir,
    has_person: actors > 0,
  };
  if (SHOT_SIZES[cam.framing]) params.shot_size = SHOT_SIZES[cam.framing];
  if (shot.videoModel && VIDEO_MODELS[shot.videoModel]) params.suggested_model = VIDEO_MODELS[shot.videoModel];
  if (shot.visualBrief?.trim()) params.scene_desc_en = shot.visualBrief.trim();
  if (lightSource[light.preset]) params.light_source = lightSource[light.preset];
  if (timeOfDay[light.preset]) params.time_of_day = timeOfDay[light.preset];
  if (actors >= 1 && actors <= 3) params.person = { count: `${actors} 人` };
  // 人物动作：取第一个可见占位角色的姿态（姿态编号与参数面板的取值一致）
  const posed = first.objects.find((o) => o.kind === "actor" && o.visible && !o.assetId);
  if (posed) params.person = { ...params.person, action: posed.pose ?? "stand" };
  // 上面按显示文字选好档位，这里换成参数面板的内部取值
  const coded = params as unknown as Record<string, unknown>;
  for (const field of Object.keys(DIRECTOR_CODES))
    if (typeof coded[field] === "string") coded[field] = code(field, coded[field] as string);
  if (params.person?.count) params.person.count = code("count", params.person.count);
  return { at: now, project: project.name, shot: shot.name, params, notes };
}

/** 写进同源的本地存储；提示词导演台那一页收到后自己填。嵌在监看中心里时顺便切到「导演台」标签。 */
export function sendToDirector(handoff: DirectorHandoff): "switched" | "stored" {
  localStorage.setItem(HANDOFF_KEY, JSON.stringify(handoff));
  try {
    if (window.self !== window.top && window.parent.location.origin === window.location.origin) {
      window.parent.location.hash = "#director";
      return "switched";
    }
  } catch {
    /* 不在监看中心里：只存不切 */
  }
  return "stored";
}

// ---- 用 Key 池模型写最终提示词 ----
export type ComposeInfo = { available: boolean; models: string[]; followsClient: boolean; reason?: string };
export type ComposeResult = { text: string; model: string; ms: number };

export function composeMessages(project: Project, shot: Shot) {
  const profile = shot.videoModel ? modelProfiles[shot.videoModel] : null;
  if (!profile) throw new Error("请先勾选本镜头使用的视频模型");
  const system = [
    `你是视频生成提示词写手，为 ${profile.name} 写一条可以直接投递的最终提示词。`,
    "严格依据下面的《模型使用指南》的写法和《镜头数据》的内容。",
    "规则：",
    "1. 只输出提示词本身，不要解释、不要标题、不要寒暄。",
    "2. 先给中文审阅版，再给英文投递版，两版之间空一行，各自以「中文审阅版：」「English:」开头。",
    "3. 不得编造镜头数据里没有的角色外观、服装、场景细节；没有给出的就写“依参考素材保持一致”。",
    "4. 不要把三维坐标、UUID、秒数表格原样写进提示词，把它们翻译成画面里看得见的结果。",
    "5. 时长、画幅、分辨率属于平台参数，不写进提示词。",
  ].join("\n");
  const user = [
    "《模型使用指南》",
    modelGuide(profile),
    "",
    "《镜头数据》（导演台根据三维预演整理出的纯净提示词）",
    cleanProductionPrompt(project, shot),
  ].join("\n");
  return [
    { role: "system", content: system },
    { role: "user", content: user },
  ];
}

const API = "/stage/api";
export async function composeInfo(fetchImpl: typeof fetch = fetch): Promise<ComposeInfo> {
  try {
    const res = await fetchImpl(`${API}/compose`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return (await res.json()) as ComposeInfo;
  } catch (err) {
    return { available: false, models: [], followsClient: false, reason: "监看中心没有响应：" + (err as Error).message };
  }
}
export async function compose(
  project: Project,
  shot: Shot,
  model: string,
  fetchImpl: typeof fetch = fetch,
): Promise<ComposeResult> {
  const res = await fetchImpl(`${API}/compose`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model, messages: composeMessages(project, shot) }),
  });
  const data = (await res.json().catch(() => ({}))) as { ok?: boolean; error?: string } & Partial<ComposeResult>;
  if (!res.ok || !data.ok || !data.text) throw new Error(data.error || `HTTP ${res.status}`);
  return { text: data.text, model: data.model || model, ms: data.ms || 0 };
}
