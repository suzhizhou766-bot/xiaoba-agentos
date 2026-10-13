import { exportShot } from "./exportPlan";
import { spatialReport, type SceneBoard } from "./spatial";
import {
  modelProfiles,
  modelGuide,
  isVideoModelId,
  type VideoModelId,
} from "./modelProfiles";
import {
  Box3,
  Vector3,
  Matrix4,
  Euler,
  Quaternion,
  PerspectiveCamera,
} from "three";
import { isPoseId, poseOf, type PoseId } from "./poses";
export type V3 = [number, number, number];
export type StageObject = {
  id: string;
  name: string;
  kind: "actor" | "model" | "prop";
  position: V3;
  rotation: V3;
  scale: number;
  visible: boolean;
  locked: boolean;
  color: string;
  assetId?: string;
  size: V3;
  center: V3;
  shape?: "box" | "cylinder" | "sphere" | "ramp";
  /** 角色占位模型的姿态（只对内置占位人有效）；缺省为站立，旧项目不变。 */
  pose?: PoseId;
};
export type Camera = {
  roll?: number;
  position: V3;
  target: V3;
  lookAt: string | null;
  focalLength: number;
  sensorWidth: number;
  aperture: number;
  focusDistance: number;
  dof: boolean;
  framing: string;
};
/** 运动曲线：描述从这个关键帧出发、到下一个关键帧之间怎么加减速。缺省为匀速（旧项目不变）。 */
export type Ease = "linear" | "easeIn" | "easeOut" | "easeInOut";
export const EASES: { id: Ease; label: string; describe: string }[] = [
  { id: "linear", label: "匀速", describe: "匀速" },
  { id: "easeIn", label: "缓入（起步慢）", describe: "缓慢起步后加速" },
  { id: "easeOut", label: "缓出（收尾慢）", describe: "逐渐减速停稳" },
  { id: "easeInOut", label: "缓入缓出", describe: "缓慢起步、平稳减速停下" },
];
/** 把线性进度 t（0–1）变成带加减速的进度；首尾仍是 0 和 1，中间单调。 */
export function easeProgress(ease: Ease | undefined, t: number): number {
  const x = Math.max(0, Math.min(1, t));
  if (ease === "easeIn") return x * x;
  if (ease === "easeOut") return 1 - (1 - x) * (1 - x);
  if (ease === "easeInOut") return x * x * (3 - 2 * x);
  return x;
}
export type Key = {
  id: string;
  target: string;
  time: number;
  position: V3;
  camera?: Camera;
  rotation?: V3;
  scale?: number;
  ease?: Ease;
};
export type SceneImage = { id: string; name: string; data: string; width: number; height: number };
export type LightingPresetId =
  | "classic-3-point"
  | "rembrandt"
  | "film-noir"
  | "soft-window"
  | "golden-hour"
  | "cool-night"
  | "custom";

export type LightingConfig = {
  preset: LightingPresetId;
  // 主光 (Key Light)
  keyAzimuth: number; // 方位角 0° ~ 360° (0° 正前, 45° 右前, 90° 正右, 180° 正背, 270° 正左, 315° 左前)
  keyElevation: number; // 俯仰角 10° ~ 85°
  keyIntensity: number; // 光强 0.1 ~ 5.0
  keyColor: string; // 颜色 hex, 如 "#fff4e6"
  keyDistance?: number; // 光源距离米，默认 8
  // 辅光 / 补光 (Fill Light)
  fillEnabled: boolean; // 是否启用
  fillAzimuth: number; // 方位角 0° ~ 360°
  fillElevation: number; // 俯仰角 10° ~ 80°
  fillIntensity: number; // 辅光强度 0.0 ~ 3.0
  fillColor: string; // 辅光颜色 hex
  // 轮廓光 (Rim / Back Light)
  rimEnabled: boolean; // 是否启用
  rimAzimuth: number; // 方位角 0° ~ 360°
  rimElevation: number; // 俯仰角 15° ~ 85°
  rimIntensity: number; // 轮廓光强度 0.0 ~ 4.0
  rimColor: string; // 轮廓光颜色 hex
  // 环境光 (Ambient Light)
  ambientIntensity: number; // 环境漫射光 0.05 ~ 1.5
  ambientColor: string; // 环境光色调 hex
  groundBounceIntensity?: number; // 半球/地面反射光 0.0 ~ 1.5
};

export type Shot = {
  exportRange?: "auto" | "manual";
  scaleMotion?: boolean;
  sceneBoard?: SceneBoard;
  sceneImageIds?: string[];
  backgroundImageId?: string | null;
  visualBrief?: string;
  videoModel?: VideoModelId;
  lighting?: LightingConfig;
  id: string;
  name: string;
  objects: StageObject[];
  camera: Camera;
  keys: Key[];
  duration: number;
  in: number;
  out: number;
};
export type Asset = {
  id: string;
  name: string;
  data: string;
  format: "glb" | "gltf";
};
export type Project = {
  sceneImages?: SceneImage[];
  version: 1;
  name: string;
  fps: 24;
  shots: Shot[];
  activeShot: string;
  assets: Asset[];
};
export const uuid = () => crypto.randomUUID();
export const copy = <T>(v: T): T => structuredClone(v);
export function actor(
  name: string,
  position: V3,
  color = "#c79f72",
): StageObject {
  return {
    id: uuid(),
    name,
    kind: "actor",
    position,
    rotation: [0, 0, 0],
    scale: 1,
    visible: true,
    locked: false,
    color,
    size: [0.52, 1.8, 0.4],
    center: [0, 0.9, 0],
  };
}
export type LightingPresetInfo = {
  id: LightingPresetId;
  name: string;
  tagline: string;
  description: string;
  config: Omit<LightingConfig, "preset">;
};

export const lightingPresets: Record<LightingPresetId, LightingPresetInfo> = {
  "classic-3-point": {
    id: "classic-3-point",
    name: "经典三点布光",
    tagline: "电影工业基石 · 光比 1:3",
    description: "主光右前侧45°暖白塑形，左侧冷调柔和辅光提亮暗部，斜后方轮廓光分离主体与背景。",
    config: {
      keyAzimuth: 45,
      keyElevation: 45,
      keyIntensity: 2.6,
      keyColor: "#fff4e6",
      keyDistance: 8,
      fillEnabled: true,
      fillAzimuth: 315,
      fillElevation: 30,
      fillIntensity: 0.9,
      fillColor: "#b8d5f8",
      rimEnabled: true,
      rimAzimuth: 200,
      rimElevation: 40,
      rimIntensity: 1.8,
      rimColor: "#ffffff",
      ambientIntensity: 0.5,
      ambientColor: "#586270",
      groundBounceIntensity: 0.4,
    },
  },
  "rembrandt": {
    id: "rembrandt",
    name: "伦勃朗光",
    tagline: "面颊三角光斑 · 光比 1:6",
    description: "55°高角度侧光在暗部面颊形成标志性倒三角光斑，低补光强化人物性格与戏剧张力。",
    config: {
      keyAzimuth: 55,
      keyElevation: 50,
      keyIntensity: 3.0,
      keyColor: "#ffe8cc",
      keyDistance: 8,
      fillEnabled: true,
      fillAzimuth: 305,
      fillElevation: 25,
      fillIntensity: 0.5,
      fillColor: "#9cb8d9",
      rimEnabled: true,
      rimAzimuth: 210,
      rimElevation: 45,
      rimIntensity: 1.4,
      rimColor: "#ffffff",
      ambientIntensity: 0.35,
      ambientColor: "#485260",
      groundBounceIntensity: 0.3,
    },
  },
  "film-noir": {
    id: "film-noir",
    name: "黑色电影侧光",
    tagline: "极致硬朗剪影 · 光比 1:16",
    description: "正侧90°强光切分明暗半脸，极弱辅光保留深邃阴影，高光正后轮廓勾勒锋利边缘。",
    config: {
      keyAzimuth: 90,
      keyElevation: 35,
      keyIntensity: 3.6,
      keyColor: "#f0f5ff",
      keyDistance: 8,
      fillEnabled: true,
      fillAzimuth: 270,
      fillElevation: 20,
      fillIntensity: 0.22,
      fillColor: "#4a6080",
      rimEnabled: true,
      rimAzimuth: 180,
      rimElevation: 50,
      rimIntensity: 2.2,
      rimColor: "#ffffff",
      ambientIntensity: 0.2,
      ambientColor: "#2a3442",
      groundBounceIntensity: 0.15,
    },
  },
  "soft-window": {
    id: "soft-window",
    name: "柔和自然窗光",
    tagline: "大面积漫射 · 光比 1:2",
    description: "侧前方大面积漫射柔和天光，充沛的环境反射与低反差阴影，通透自然无死黑。",
    config: {
      keyAzimuth: 300,
      keyElevation: 40,
      keyIntensity: 2.2,
      keyColor: "#fbfdff",
      keyDistance: 8,
      fillEnabled: true,
      fillAzimuth: 60,
      fillElevation: 35,
      fillIntensity: 1.2,
      fillColor: "#e4edfc",
      rimEnabled: true,
      rimAzimuth: 150,
      rimElevation: 30,
      rimIntensity: 0.6,
      rimColor: "#f0f4fa",
      ambientIntensity: 0.8,
      ambientColor: "#6c7888",
      groundBounceIntensity: 0.55,
    },
  },
  "golden-hour": {
    id: "golden-hour",
    name: "黄昏暖金时刻",
    tagline: "低角度斜阳 · 暖金与暮蓝对冲",
    description: "18°超低角度暖金逆侧光拉出修长地面阴影，辅以暮色青蓝环境光与浓郁金色发丝光晕。",
    config: {
      keyAzimuth: 250,
      keyElevation: 18,
      keyIntensity: 3.3,
      keyColor: "#ff9d42",
      keyDistance: 8,
      fillEnabled: true,
      fillAzimuth: 70,
      fillElevation: 40,
      fillIntensity: 0.7,
      fillColor: "#7c8eb5",
      rimEnabled: true,
      rimAzimuth: 230,
      rimElevation: 22,
      rimIntensity: 2.8,
      rimColor: "#ffb469",
      ambientIntensity: 0.4,
      ambientColor: "#5b5066",
      groundBounceIntensity: 0.35,
    },
  },
  "cool-night": {
    id: "cool-night",
    name: "冷调电影月光",
    tagline: "高位银蓝清辉 · 幽静电影感",
    description: "高俯角银青色月光冷冽洒下，微弱深钢蓝辅光，亮银发丝轮廓光强化夜戏神秘质感。",
    config: {
      keyAzimuth: 135,
      keyElevation: 65,
      keyIntensity: 2.0,
      keyColor: "#a8d2f7",
      keyDistance: 8,
      fillEnabled: true,
      fillAzimuth: 315,
      fillElevation: 25,
      fillIntensity: 0.4,
      fillColor: "#3a4e66",
      rimEnabled: true,
      rimAzimuth: 160,
      rimElevation: 70,
      rimIntensity: 2.0,
      rimColor: "#d8ebff",
      ambientIntensity: 0.25,
      ambientColor: "#223040",
      groundBounceIntensity: 0.2,
    },
  },
  "custom": {
    id: "custom",
    name: "自定义布光",
    tagline: "自由微调光位参数",
    description: "用户手动精调主光方位、高度、辅光比及轮廓光强度。",
    config: {
      keyAzimuth: 45,
      keyElevation: 45,
      keyIntensity: 2.5,
      keyColor: "#ffffff",
      keyDistance: 8,
      fillEnabled: true,
      fillAzimuth: 315,
      fillElevation: 30,
      fillIntensity: 0.8,
      fillColor: "#d0e0f5",
      rimEnabled: true,
      rimAzimuth: 195,
      rimElevation: 40,
      rimIntensity: 1.5,
      rimColor: "#ffffff",
      ambientIntensity: 0.5,
      ambientColor: "#505a66",
      groundBounceIntensity: 0.35,
    },
  },
};

export function defaultLighting(): LightingConfig {
  return {
    preset: "classic-3-point",
    ...lightingPresets["classic-3-point"].config,
  };
}

export function azimuthToDirectionLabel(azimuth: number): { zh: string; en: string } {
  const norm = ((azimuth % 360) + 360) % 360;
  if (norm >= 337.5 || norm < 22.5) return { zh: "正前方", en: "front" };
  if (norm >= 22.5 && norm < 67.5) return { zh: "右前侧 45°", en: "front-right 45°" };
  if (norm >= 67.5 && norm < 112.5) return { zh: "正右侧 90°", en: "right 90°" };
  if (norm >= 112.5 && norm < 157.5) return { zh: "右后侧 135°", en: "rear-right 135°" };
  if (norm >= 157.5 && norm < 202.5) return { zh: "正后方逆光 180°", en: "backlight 180°" };
  if (norm >= 202.5 && norm < 247.5) return { zh: "左后侧 225°", en: "rear-left 225°" };
  if (norm >= 247.5 && norm < 292.5) return { zh: "正左侧 270°", en: "left 270°" };
  return { zh: "左前侧 315°", en: "front-left 315°" };
}

export function lightingToCartesian(
  azimuthDeg: number,
  elevationDeg: number,
  distance = 8,
  targetY = 1,
): [number, number, number] {
  const theta = (azimuthDeg * Math.PI) / 180;
  const phi = (elevationDeg * Math.PI) / 180;
  const cosPhi = Math.cos(phi);
  const x = distance * cosPhi * Math.sin(theta);
  const y = targetY + distance * Math.sin(phi);
  const z = distance * cosPhi * Math.cos(theta);
  return [Number(x.toFixed(3)), Number(y.toFixed(3)), Number(z.toFixed(3))];
}

export type LightingDescription = {
  presetName: string;
  ratio: string;
  summaryZh: string;
  detailZh: string;
  detailEn: string;
};

export function describeLighting(config?: LightingConfig): LightingDescription {
  const cfg = config ?? defaultLighting();
  const preset = lightingPresets[cfg.preset] ?? lightingPresets["classic-3-point"];
  const keyDir = azimuthToDirectionLabel(cfg.keyAzimuth);
  const fillDir = azimuthToDirectionLabel(cfg.fillAzimuth);
  const rimDir = azimuthToDirectionLabel(cfg.rimAzimuth);

  const ratioVal = cfg.fillEnabled && cfg.fillIntensity > 0
    ? (cfg.keyIntensity / cfg.fillIntensity).toFixed(1)
    : "高反差";

  const ratioStr = cfg.fillEnabled && cfg.fillIntensity > 0
    ? `${ratioVal}:1`
    : "极高反差(剪影)";

  const fillTextZh = cfg.fillEnabled
    ? `位于${fillDir.zh}的柔和辅光（强度 ${cfg.fillIntensity.toFixed(1)}）提亮暗部细节，避免死黑`
    : "未设补光，保留浓重阴影与剧烈明暗剪影";

  const rimTextZh = cfg.rimEnabled
    ? `斜后方${rimDir.zh}的轮廓光（强度 ${cfg.rimIntensity.toFixed(1)}）清晰勾勒角色发丝与肩颈边缘，产生空间剥离感`
    : "无轮廓光";

  const ambientTextZh = `环境漫射光（强度 ${cfg.ambientIntensity.toFixed(2)}）维持场景整体影调层次`;

  const detailZh = `[光影与布光] ${preset.name}（主辅参数光强比约 ${ratioStr}）。主光来自角色${keyDir.zh}（${cfg.keyElevation}° 俯角，强度 ${cfg.keyIntensity.toFixed(1)}），在面部与躯干塑造鲜明立体感与柔和投影；${fillTextZh}；${rimTextZh}；${ambientTextZh}。物理阴影真实，层次分明。`;

  const fillTextEn = cfg.fillEnabled
    ? `diffuse fill light from ${fillDir.en} (intensity ${cfg.fillIntensity.toFixed(1)}) gently lifts facial shadows and dark details without wash-out`
    : "no fill light, maintaining deep dramatic shadows";

  const rimTextEn = cfg.rimEnabled
    ? `crisp rear rim light from ${rimDir.en} (intensity ${cfg.rimIntensity.toFixed(1)}) cleanly outlines subject hair and shoulders for cinematic depth separation`
    : "no rim light";

  const detailEn = `Cinematic lighting: ${cfg.preset} setup (approx. ${ratioStr} key-to-fill parameter intensity ratio). Key light from ${keyDir.en} at ${cfg.keyElevation}° elevation (intensity ${cfg.keyIntensity.toFixed(1)}), sculpting distinct facial contours and realistic floor shadows; ${fillTextEn}; ${rimTextEn}; ambient illumination (intensity ${cfg.ambientIntensity.toFixed(2)}) maintaining scene depth.`;

  return {
    presetName: preset.name,
    ratio: ratioStr,
    summaryZh: `${preset.name} · 光比 ${ratioStr}`,
    detailZh,
    detailEn,
  };
}

export function initial(): Project {
  const a = actor("角色 A · 主角", [-0.8, 0, 0]);
  const b = actor("角色 B · 对手", [1, 0, -0.7], "#829bba");
  const shot: Shot = {
    id: uuid(),
    name: "初见 · 双人中景",
    objects: [a, b],
    camera: {
      position: [3, 1.6, 6],
      target: [0, 1, 0],
      lookAt: null,
      focalLength: 35,
      sensorWidth: 36,
      aperture: 2.8,
      focusDistance: 6,
      dof: false,
      framing: "MS",
    },
    lighting: defaultLighting(),
    keys: [],
    duration: 30,
    in: 0,
    out: 30,
  };
  return {
    version: 1,
    name: "未命名导演项目",
    fps: 24,
    shots: [shot],
    activeShot: shot.id,
    assets: [],
  };
}
export function sample(
  keys: Key[],
  target: string,
  time: number,
  fallback: V3,
): V3 {
  const track = keys
    .filter((k) => k.target === target)
    .sort((a, b) => a.time - b.time);
  if (!track.length) return fallback;
  if (time <= track[0].time) return track[0].position;
  for (let i = 1; i < track.length; i++) {
    const b = track[i],
      a = track[i - 1];
    if (time <= b.time) {
      const t = easeProgress(a.ease, (time - a.time) / (b.time - a.time));
      return a.position.map((x, j) => x + (b.position[j] - x) * t) as V3;
    }
  }
  return track.at(-1)!.position;
}
export function sampleCamera(
  keys: Key[],
  time: number,
  fallback: Camera,
): Camera {
  const track = keys
    .filter((k) => k.target === "camera" && k.camera)
    .sort((a, b) => a.time - b.time);
  const result = copy(fallback);
  result.position = copy(sample(keys, "camera", time, fallback.position));
  if (!track.length) return result;
  let a = track[0],
    b = track[0];
  if (time >= track.at(-1)!.time) a = b = track.at(-1)!;
  else if (time > track[0].time) {
    for (let i = 1; i < track.length; i++)
      if (time <= track[i].time) {
        a = track[i - 1];
        b = track[i];
        break;
      }
  }
  const t = a === b ? 0 : easeProgress(a.ease, (time - a.time) / (b.time - a.time)),
    ca = a.camera!,
    cb = b.camera!;
  const blend = (x: number, y: number) => x + (y - x) * t;
  return {
    ...copy(t < 1 ? ca : cb),
    position: result.position,
    roll: blend(ca.roll ?? 0, cb.roll ?? 0),
    target: ca.target.map((v, i) => blend(v, cb.target[i])) as V3,
    focalLength: blend(ca.focalLength, cb.focalLength),
    sensorWidth: blend(ca.sensorWidth, cb.sensorWidth),
    aperture: blend(ca.aperture, cb.aperture),
    focusDistance: blend(ca.focusDistance, cb.focusDistance),
  };
}
export function cameraTarget(camera: Camera, objects: StageObject[]): V3 {
  const o = objects.find((o) => o.id === camera.lookAt);
  if (!o) return camera.target;
  return new Vector3(...o.center)
    .multiplyScalar(o.scale)
    .applyEuler(new Euler(...o.rotation))
    .add(new Vector3(...o.position))
    .toArray() as V3;
}
export function applyCamera(
  cam: PerspectiveCamera,
  c: Camera,
  objects: StageObject[],
) {
  cam.aspect = 16 / 9;
  cam.near = 0.03;
  cam.far = 1000;
  cam.position.set(...c.position);
  cam.filmGauge = c.sensorWidth;
  cam.setFocalLength(c.focalLength);
  const target = new Vector3(...cameraTarget(c, objects));
  if (target.distanceToSquared(cam.position) < 1e-10) target.z -= 0.001;
  cam.up.set(0, 1, 0);
  cam.lookAt(target);
  cam.rotateZ(c.roll ?? 0);
  cam.updateProjectionMatrix();
  cam.updateMatrixWorld(true);
  return cam;
}
export function sampleTransform(keys:Key[],id:string,time:number,fallback:StageObject) {
  const rich=keys.filter(k=>k.target===id&&k.rotation&&k.scale!==undefined).sort((a,b)=>a.time-b.time);
  if(!rich.length)return {rotation:fallback.rotation,scale:fallback.scale};
  let a=rich[0],b=a;
  for(const key of rich){if(key.time<=time)a=key; if(key.time>=time){b=key;break;} b=key;}
  const t=a===b?0:easeProgress(a.ease,(time-a.time)/(b.time-a.time));
  const rotation=a.rotation!.map((v,i)=>v+(b.rotation![i]-v)*t) as V3;
  return {rotation,scale:a.scale!+(b.scale!-a.scale!)*t};
}
export function sampledShot(shot: Shot, time: number): Shot {
  return {
    ...shot,
    camera: sampleCamera(shot.keys, time, shot.camera),
    objects: shot.objects.map((o) => ({
      ...o,
      position: copy(sample(shot.keys, o.id, time, o.position)),
      ...sampleTransform(shot.keys,o.id,time,o),
    })),
  };
}
export function viewCone(camera: Camera, objects: StageObject[]) {
  const cam = applyCamera(new PerspectiveCamera(), camera, objects);
  const origin = cam.position.clone();
  const depth = Math.max(
    0.5,
    Math.min(
      30,
      origin.distanceTo(new Vector3(...cameraTarget(camera, objects))),
    ),
  );
  const h = Math.tan((cam.fov * Math.PI) / 360) * depth,
    w = h * cam.aspect;
  const corners = [
    [-w, -h, -depth],
    [w, -h, -depth],
    [w, h, -depth],
    [-w, h, -depth],
  ].map(
    (v) =>
      new Vector3(...(v as V3)).applyMatrix4(cam.matrixWorld).toArray() as V3,
  );
  return { origin: origin.toArray() as V3, corners, depth };
}
export const frames: Record<
  string,
  { portion: number; anchor: number; label: string; desc: string }
> = {
  ECU: { portion: 0.13, anchor: 0.91, label: "大特写", desc: "局部细节 / 极特写" },
  CU: { portion: 0.29, anchor: 0.84, label: "特写", desc: "面部表情" },
  MCU: { portion: 0.46, anchor: 0.76, label: "近景", desc: "胸部以上" },
  MS: { portion: 0.65, anchor: 0.67, label: "中景", desc: "半身景 / 腰部以上" },
  MWS: { portion: 0.85, anchor: 0.57, label: "中全景", desc: "膝盖以上 / 牛仔景" },
  FS: { portion: 1.15, anchor: 0.5, label: "全景", desc: "完整全身" },
  LS: { portion: 1.9, anchor: 0.5, label: "远景", desc: "人物与周围环境" },
  ELS: { portion: 3.8, anchor: 0.5, label: "大远景", desc: "宏大场景 / 极远景" },
};
export function frameCamera(
  cam: Camera,
  o: StageObject,
  framing: string,
): Camera {
  const f = frames[framing];
  const box = new Box3()
    .setFromCenterAndSize(new Vector3(...o.center), new Vector3(...o.size))
    .applyMatrix4(
      new Matrix4().compose(
        new Vector3(...o.position),
        new Quaternion().setFromEuler(new Euler(...o.rotation)),
        new Vector3(o.scale, o.scale, o.scale),
      ),
    );
  const size = box.getSize(new Vector3()),
    center = box.getCenter(new Vector3());
  const h = Math.max(0.01, size.y);
  const target: V3 = [center.x, box.min.y + h * f.anchor, center.z];
  const vfov =
    2 * Math.atan(cam.sensorWidth / (16 / 9) / (2 * cam.focalLength));
  const distance = Math.max(
    (h * f.portion) / (2 * Math.tan(vfov / 2)),
    (Math.max(size.x, size.z) / ((2 * Math.tan(vfov / 2) * 16) / 9)) * 1.1,
  );
  const dx = cam.position[0] - cam.target[0],
    dz = cam.position[2] - cam.target[2],
    len = Math.hypot(dx, dz) || 1;
  return {
    ...cam,
    framing,
    lookAt: null,
    target,
    position: [
      target[0] + (dx / len) * distance,
      target[1],
      target[2] + (len === 1 && dx === 0 && dz === 0 ? 1 : dz / len) * distance,
    ],
    focusDistance: distance,
  };
}
// Project the sampled scene into camera space; never infer appearance from placeholders.
export function frameDescription(s: Shot, time: number) {
  const pose = sampledShot(s, time);
  const position = pose.camera.position;
  const target = cameraTarget(pose.camera, pose.objects);
  const camera = applyCamera(
    new PerspectiveCamera(),
    pose.camera,
    pose.objects,
  );
  return {
    时间秒: time,
    摄影机位置米: position,
    焦距毫米: pose.camera.focalLength,
    视线目标米: target,
    主体构图: pose.objects
      .filter((o) => o.visible)
      .map((o) => {
        const pos = sample(s.keys, o.id, time, o.position);
        const matrix = new Matrix4().compose(
          new Vector3(...pos),
          new Quaternion().setFromEuler(new Euler(...o.rotation)),
          new Vector3(o.scale, o.scale, o.scale),
        );
        const box = new Box3()
          .setFromCenterAndSize(
            new Vector3(...o.center),
            new Vector3(...o.size),
          )
          .applyMatrix4(matrix);
        const center = box.getCenter(new Vector3());
        const ndc = center.clone().project(camera);
        const corners = [box.min.x, box.max.x].flatMap((x) =>
          [box.min.y, box.max.y].flatMap((y) =>
            [box.min.z, box.max.z].map((z) => new Vector3(x, y, z)),
          ),
        );
        const behind = corners.some(
          (v) =>
            v.clone().applyMatrix4(camera.matrixWorldInverse).z >= -camera.near,
        );
        const projected = corners.map((v) => v.project(camera));
        const left = (Math.min(...projected.map((v) => v.x)) + 1) / 2,
          right = (Math.max(...projected.map((v) => v.x)) + 1) / 2;
        const top = (1 - Math.max(...projected.map((v) => v.y))) / 2,
          bottom = (1 - Math.min(...projected.map((v) => v.y))) / 2;
        const round = (n: number) => Math.round(n * 1000) / 1000;
        return {
          UUID: o.id,
          名称: o.name,
          世界位置米: pos,
          旋转弧度: o.rotation,
          等比缩放: o.scale,
          画面区域: behind
            ? "跨越近裁切面或位于相机后方，请以预演图检查"
            : `${ndc.x < -0.33 ? "左" : ndc.x > 0.33 ? "右" : "中"}侧 / ${ndc.y > 0.33 ? "上" : ndc.y < -0.33 ? "下" : "中"}部`,
          归一化包围框: behind
            ? null
            : {
                左: round(left),
                上: round(top),
                右: round(right),
                下: round(bottom),
              },
          构图提示: behind
            ? "投影不可可靠使用"
            : right < 0 || left > 1 || bottom < 0 || top > 1
              ? "画外主体"
              : left < 0 || right > 1 || top < 0 || bottom > 1
                ? "部分出画 / 裁切"
                : "画内主体",
        };
      }),
  };
}
export function compile(p: Project, source: Shot) {
  const s=exportShot(source);
  const first = sampledShot(s, s.in);
  const profile = s.videoModel ? modelProfiles[s.videoModel] : null;
  const selectedName = profile?.name ?? "未选择：请在导出前勾选视频模型";
  const doc = {
    编译器: "小八 Director Handoff · 0.7",
    目标: "交给对话助手反推制作提示词与生成最终首尾帧；不是视频模型 API 请求",
    视频模型: {
      id: profile?.id ?? null,
      名称: selectedName,
      厂商: profile?.provider ?? null,
      官方API标识: profile?.apiModelId ?? null,
      选择来源: profile ? "用户在导演台按镜头选择" : "尚未选择",
      资料版本: profile?.version ?? null,
      核验日期: profile?.checkedAt ?? null,
    },
    模型制作指令:
      profile?.assistantInstruction ?? "请在导演台选择视频模型后导出。",
    模型使用指南: profile ? modelGuide(profile) : null,
    用户视觉说明:
      s.visualBrief?.trim() ||
      "未填写：需结合用户补充的角色、场景、风格与参考素材，不能把占位模型当作最终外观",
    交接流程: [
      "用户设计站位、构图、机位与运动轨迹",
      "导演台导出反推说明、场景数据、首尾构图参考图",
      "用户在导演台勾选视频模型，交接包自动携带所选模型和指南，直接发给助手",
      "助手结合参考素材生成最终首帧、尾帧与该模型适用的视频提示词",
      "用户自行将最终首尾帧和提示词提交到指定平台生成视频",
    ],
    助手制作要求: [
      "先读取 handoff.json 与首尾预演图，核对取景范围与主体遮挡；数值投影是包围盒近似",
      "首尾预演图是构图控制参考，不是最终生成图；保留站位、透视、景别和画面裁切",
      "为首帧、尾帧分别编写图像提示词，使用一致的角色、场景、材质与光照参考，并生成两张最终图像",
      "若没有视觉设定或角色外观参考，先向用户补齐，不凭空从占位模型推断身份与服装",
      "读取包内视频模型字段与 model-guide.md，按已经选定的模型制作；不要重复询问用户使用什么模型",
      "交付最终首帧图、尾帧图、图像制作提示词和可复制的视频提示词，由用户提交平台",
    ],
    首帧反推: frameDescription(s, s.in),
    尾帧反推: frameDescription(s, s.out),
    运镜与调度采样: [
      ...new Set([
        s.in,
        ...s.keys.map((k) => k.time).filter((t) => t > s.in && t < s.out),
        s.out,
      ]),
    ]
      .sort((a, b) => a - b)
      .map((t) => frameDescription(s, t)),
    项目: p.name,
    镜头: s.name,
    场景图片: (p.sceneImages ?? []).filter(image => s.sceneImageIds?.includes(image.id)).map(image => ({
      UUID: image.id, 名称: image.name,
      文件: `scene-images/${image.id}.${image.data.startsWith("data:image/png") ? "png" : "jpg"}`,
      用途: image.id === s.backgroundImageId ? "场景外观参考 + 世界空间有限背景板" : "场景外观参考",
      尺寸: [image.width,image.height],
      约束: "保留参考中的建筑、材质、光照；背景板在世界中固定，内部景物仍是平面；以用户校准的地面和代理物体表达空间，不声称自动三维重建",
    })),
    空间导演记录: spatialReport(s),
    素材职责: s.objects.map((o) => ({
      UUID: o.id,
      名称: o.name,
      职责:
        o.kind === "actor"
          ? "表演主体"
          : o.kind === "model"
            ? "导入模型 / 场景参考"
            : "道具",
      素材: p.assets.find((a) => a.id === o.assetId)?.name ?? "三维占位参考",
      ...(o.kind === "actor" && !o.assetId
        ? { 占位姿态: `${poseOf(o.pose).label}（只表示大致动作，不代表最终动作细节）` }
        : {}),
      可见: o.visible,
    })),
    导出规则: {范围:s.exportRange=== "manual"?"手动首尾区间":"自动截止至最后关键帧",人物尺寸:s.scaleMotion?"明确允许实际缩放动画":"固定各对象首个缩放键的尺寸；透视大小由机位与站位决定",原始时间轴时长:source.duration},
    时间轴: {
      帧率: p.fps,
      时长: s.duration,
      首帧: s.in,
      尾帧: s.out,
      插值: "位置、视线目标、焦距及数值摄影参数按各关键帧的运动曲线插值（未设置为匀速）；首末键外保持；LookAt 与开关按关键帧切换",
      关键帧: s.keys.map((k) => ({
        秒: k.time,
        对象:
          k.target === "camera"
            ? "主摄影机"
            : s.objects.find((o) => o.id === k.target)?.name,
        位置米: k.position,
        摄影机状态: k.camera ?? null,
        对象旋转: k.rotation ?? null,
        对象缩放: k.scale ?? null,
        到下一键的运动曲线: EASES.find((e) => e.id === (k.ease ?? "linear"))!.describe,
      })),
    },
    摄影机: {
      状态时间秒: s.in,
      画面滚转弧度: first.camera.roll ?? 0,
      景别: first.camera.framing,
      焦距毫米: first.camera.focalLength,
      传感器宽毫米: first.camera.sensorWidth,
      机位米: first.camera.position,
      视线目标: first.camera.lookAt
        ? s.objects.find((o) => o.id === first.camera.lookAt)?.name
        : first.camera.target,
      光圈: first.camera.aperture,
      对焦距离米: first.camera.focusDistance,
      景深意图: first.camera.dof,
    },
    光影布光: describeLighting(s.lighting ?? defaultLighting()),
    动作: s.objects.map((o) => ({
      名称: o.name,
      起始位置: sample(s.keys, o.id, s.in, o.position),
      旋转弧度: o.rotation,
      等比缩放: o.scale,
      动作说明: s.keys.some((k) => k.target === o.id)
        ? "按关键帧位置连续移动，未定义骨骼动作"
        : "保持站位，未指定表演动作",
    })),
    连续性: [
      "保持角色身份、服装、道具和空间关系一致",
      "机位沿已定义轨迹连续运动，不擅自切镜",
      "三维预演仅约束构图、站位及镜头，外观以用户提供参考素材为准",
    ],
    不变量: [
      "保持人物数量与模型比例",
      "不增加或删除主体",
      "保持场景布局，不镜像翻转",
      "未指定的动作、表情与光照不自行强化",
    ],
  };
  return {
    json: doc,
    text:
      `【交接任务】请依据「${s.name}」的运镜、站位与构图制作最终首尾帧及视频提示词。选定区间 ${s.in}–${s.out} 秒，共 ${(s.out - s.in).toFixed(3)} 秒，24fps，16:9。目标视频模型：${selectedName}。已选定时无需再次询问模型。\n\n` +
      Object.entries(doc)
        .map(
          ([k, v]) =>
            `【${k}】\n${typeof v === "string" ? v : JSON.stringify(v, null, 2)}`,
        )
        .join("\n\n"),
  };
}
// Validate all project data before committing it to the store.
export function parseProject(raw: unknown): Project {
  const p = raw as Project;
  const finite = (n: unknown) => typeof n === "number" && Number.isFinite(n);
  const vec = (v: unknown) =>
    Array.isArray(v) && v.length === 3 && v.every(finite);
  const check = (ok: unknown, msg: string) => {
    if (!ok) throw Error("项目格式错误：" + msg);
  };
  check(
    p && p.version === 1 && p.fps === 24 && typeof p.name === "string",
    "版本或项目名称",
  );
  check(
    Array.isArray(p.shots) && p.shots.length > 0 && Array.isArray(p.assets),
    "镜头 / 素材",
  );
  const ids = new Set<string>();
  for (const a of p.assets) {
    check(
      typeof a.id === "string" &&
        !ids.has(a.id) &&
        typeof a.name === "string" &&
        typeof a.data === "string" &&
        ["glb", "gltf"].includes(a.format),
      "素材",
    );
    ids.add(a.id);
  }
  check(p.sceneImages === undefined || Array.isArray(p.sceneImages), "场景图片列表");
  const imageIds = new Set<string>();
  for (const image of p.sceneImages ?? []) {
    check(image && typeof image.id === "string" && /^[a-zA-Z0-9-]+$/.test(image.id) && !imageIds.has(image.id) && typeof image.name === "string" && typeof image.data === "string" && /^data:image\/(png|jpeg);base64,[A-Za-z0-9+/]+={0,2}$/.test(image.data) && image.data.length <= 28 * 1024 * 1024 && finite(image.width) && finite(image.height) && image.width > 0 && image.height > 0 && image.width <= 8192 && image.height <= 8192, "场景图片");
    imageIds.add(image.id);
  }
  const shots = new Set<string>();
  for (const s of p.shots) {
    check(
      typeof s.id === "string" &&
        !shots.has(s.id) &&
        typeof s.name === "string",
      "镜头 ID",
    );
    check(
      s.videoModel === undefined || isVideoModelId(s.videoModel),
      "视频模型",
    );
    check(
      s.visualBrief === undefined || typeof s.visualBrief === "string",
      "视觉说明",
    );
    check(s.sceneImageIds === undefined || (Array.isArray(s.sceneImageIds) && new Set(s.sceneImageIds).size === s.sceneImageIds.length && s.sceneImageIds.every(id => imageIds.has(id))), "场景图片引用");
    check(!s.backgroundImageId || s.sceneImageIds?.includes(s.backgroundImageId), "场景背景引用");
    if(s.sceneBoard){const b=s.sceneBoard;check(vec(b.position)&&finite(b.width)&&b.width>0&&b.width<=1000&&finite(b.height)&&b.height>0&&b.height<=1000&&finite(b.yaw)&&(b.pitch===undefined||finite(b.pitch))&&(b.horizonRatio===undefined||(finite(b.horizonRatio)&&b.horizonRatio>=0&&b.horizonRatio<=1))&&typeof b.confirmed==="boolean","背景板空间");}
    check(s.exportRange===undefined||["auto","manual"].includes(s.exportRange),"导出范围");
    check(s.scaleMotion===undefined||typeof s.scaleMotion==="boolean","尺寸动画");
    if (s.lighting !== undefined) {
      const l = s.lighting;
      check(
        l &&
          typeof l.preset === "string" &&
          finite(l.keyAzimuth) &&
          l.keyAzimuth >= 0 &&
          l.keyAzimuth <= 360 &&
          finite(l.keyElevation) &&
          l.keyElevation >= 0 &&
          l.keyElevation <= 90 &&
          finite(l.keyIntensity) &&
          l.keyIntensity >= 0 &&
          typeof l.keyColor === "string" &&
          typeof l.fillEnabled === "boolean" &&
          finite(l.fillAzimuth) &&
          finite(l.fillElevation) &&
          finite(l.fillIntensity) &&
          typeof l.fillColor === "string" &&
          typeof l.rimEnabled === "boolean" &&
          finite(l.rimAzimuth) &&
          finite(l.rimElevation) &&
          finite(l.rimIntensity) &&
          typeof l.rimColor === "string" &&
          finite(l.ambientIntensity) &&
          typeof l.ambientColor === "string",
        "布光配置",
      );
    }
    shots.add(s.id);
    check(
      finite(s.duration) &&
        s.duration > 0 &&
        s.duration <= 600 &&
        finite(s.in) &&
        finite(s.out) &&
        s.in >= 0 &&
        s.in <= s.out &&
        s.out <= s.duration,
      "时间范围",
    );
    check(Array.isArray(s.objects) && Array.isArray(s.keys), "对象 / 关键帧");
    const objects = new Set<string>();
    for (const o of s.objects) {
      check(
        typeof o.id === "string" &&
          !objects.has(o.id) &&
          typeof o.name === "string" &&
          ["actor", "model", "prop"].includes(o.kind) &&
          vec(o.position) &&
          vec(o.rotation) &&
          vec(o.size) &&
          o.size.every((n) => n >= 0) &&
          vec(o.center) &&
          finite(o.scale) &&
          o.scale > 0 &&
          typeof o.visible === "boolean" &&
          typeof o.locked === "boolean" &&
          typeof o.color === "string" &&
          (o.shape === undefined || ["box", "cylinder", "sphere", "ramp"].includes(o.shape)) &&
          (o.pose === undefined || isPoseId(o.pose)),
        "场景对象",
      );
      check(
        (o.kind !== "model" || !!o.assetId) &&
          (!o.assetId || ids.has(o.assetId)),
        "素材引用",
      );
      objects.add(o.id);
    }
    const c = s.camera;
    check(
      c &&
        vec(c.position) &&
        vec(c.target) &&
        (c.roll === undefined || finite(c.roll)) &&
        finite(c.focalLength) &&
        c.focalLength >= 8 &&
        c.focalLength <= 300 &&
        finite(c.sensorWidth) &&
        c.sensorWidth >= 8 &&
        c.sensorWidth <= 70 &&
        finite(c.aperture) &&
        c.aperture > 0 &&
        finite(c.focusDistance) &&
        c.focusDistance > 0 &&
        typeof c.dof === "boolean" &&
        c.framing in frames &&
        (c.lookAt === null || objects.has(c.lookAt)),
      "摄影机",
    );
    const times = new Set<string>();
    for (const k of s.keys) {
      const t = k.target + ":" + k.time;
      check(
        typeof k.id === "string" &&
          (k.target === "camera" || objects.has(k.target)) &&
          finite(k.time) &&
          k.time >= 0 &&
          k.time <= s.duration &&
          vec(k.position) &&
          !times.has(t),
        "关键帧",
      );
      check(k.rotation===undefined||vec(k.rotation),"旋转关键帧");
      check(k.scale===undefined||(finite(k.scale)&&k.scale>0),"缩放关键帧");
      check(k.ease===undefined||EASES.some((e)=>e.id===k.ease),"关键帧运动曲线");
      if (k.camera) {
        const c = k.camera;
        check(
          k.target === "camera" &&
            vec(c.position) &&
            vec(c.target) &&
            (c.roll === undefined || finite(c.roll)) &&
            finite(c.focalLength) &&
            c.focalLength >= 8 &&
            c.focalLength <= 300 &&
            finite(c.sensorWidth) &&
            c.sensorWidth >= 8 &&
            c.sensorWidth <= 70 &&
            finite(c.aperture) &&
            c.aperture > 0 &&
            finite(c.focusDistance) &&
            c.focusDistance > 0 &&
            typeof c.dof === "boolean" &&
            c.framing in frames &&
            (c.lookAt === null || objects.has(c.lookAt)),
          "摄影机关键帧",
        );
      }
      times.add(t);
    }
  }
  check(shots.has(p.activeShot), "活动镜头");
  return copy(p);
}

export interface AxisHint {
  axis: string;
  title: string;
  desc: string;
  color: string;
}

export function getAxisHint(
  axis: string | null | undefined,
  mode: string,
  selected: string,
): AxisHint | null {
  if (!axis) return null;
  const isCamera = selected === "camera";
  const isCameraTarget = selected === "camera-target";

  // Translate Mode
  if (mode === "translate") {
    if (isCamera) {
      if (axis === "X") {
        return {
          axis: "X",
          title: "水平移动 (左右横移 Truck)",
          desc: "左右平移摄像机机位，保持高度与视线不变",
          color: "#f55656",
        };
      }
      if (axis === "Y") {
        return {
          axis: "Y",
          title: "俯仰移动 (垂直升降 Pedestal)",
          desc: "垂直上下升降机位高度，调整摄影机视点高低",
          color: "#3ddc84",
        };
      }
      if (axis === "Z") {
        return {
          axis: "Z",
          title: "前后移动 (推拉运镜 Dolly)",
          desc: "沿镜头视线前后推进或拉远机位，调整景别与景深",
          color: "#4d9eff",
        };
      }
      if (axis === "XY") {
        return {
          axis: "XY",
          title: "立面自由平移 (横向/高度)",
          desc: "在横向与高低平面内平移摄像机",
          color: "#ffb84d",
        };
      }
      if (axis === "XZ") {
        return {
          axis: "XZ",
          title: "地面水平平移 (前后/左右)",
          desc: "在水平地面二维平面内移动摄像机位置",
          color: "#ffb84d",
        };
      }
      if (axis === "YZ") {
        return {
          axis: "YZ",
          title: "纵深垂直平移 (推拉/升降)",
          desc: "在纵深推拉与垂直高度平面内平移机位",
          color: "#ffb84d",
        };
      }
    } else if (isCameraTarget) {
      if (axis === "X") {
        return {
          axis: "X",
          title: "目标点水平移动 (左右对焦瞄准)",
          desc: "左右调整摄像机瞄准的焦点位置",
          color: "#f55656",
        };
      }
      if (axis === "Y") {
        return {
          axis: "Y",
          title: "目标点俯仰升降 (上下视点中心)",
          desc: "上下调整摄像机瞄准的高低视角中心",
          color: "#3ddc84",
        };
      }
      if (axis === "Z") {
        return {
          axis: "Z",
          title: "目标点前后进深 (景深对焦位置)",
          desc: "前后调整对焦目标的景深位置",
          color: "#4d9eff",
        };
      }
    } else {
      // Scene object
      if (axis === "X") {
        return {
          axis: "X",
          title: "水平位移 (X 轴)",
          desc: "左右平移选中的物体",
          color: "#f55656",
        };
      }
      if (axis === "Y") {
        return {
          axis: "Y",
          title: "垂直升降 (Y 轴)",
          desc: "上下调整物体的高度",
          color: "#3ddc84",
        };
      }
      if (axis === "Z") {
        return {
          axis: "Z",
          title: "前后纵深 (Z 轴)",
          desc: "沿空间进深前后移动物体",
          color: "#4d9eff",
        };
      }
      if (axis === "XZ") {
        return {
          axis: "XZ",
          title: "地表平面平移",
          desc: "在水平面上前后左右拖拽物体",
          color: "#ffb84d",
        };
      }
    }
  }

  // Rotate Mode
  if (mode === "rotate") {
    if (isCamera) {
      if (axis === "X") {
        return {
          axis: "X",
          title: "俯仰旋转 (上下抬头/低头 Tilt)",
          desc: "向上仰拍或向下俯拍，调整摄影机垂直俯仰角度",
          color: "#f55656",
        };
      }
      if (axis === "Y") {
        return {
          axis: "Y",
          title: "水平摇镜 (左右转向 Pan)",
          desc: "向左或向右水平转动机位镜头视角",
          color: "#3ddc84",
        };
      }
      if (axis === "Z") {
        return {
          axis: "Z",
          title: "画面滚转 (镜头倾斜 Roll)",
          desc: "顺时针或逆时针倾斜镜头画面 (荷兰角)",
          color: "#4d9eff",
        };
      }
      if (axis === "E") {
        return {
          axis: "E",
          title: "屏幕空间旋转",
          desc: "沿当前屏幕法线方向旋转镜头视角",
          color: "#ffb84d",
        };
      }
    } else {
      if (axis === "X") return { axis: "X", title: "俯仰倾斜 (Pitch)", desc: "前后倾斜旋转物体", color: "#f55656" };
      if (axis === "Y") return { axis: "Y", title: "水平朝向 (Yaw)", desc: "调整物体的水平旋转朝向", color: "#3ddc84" };
      if (axis === "Z") return { axis: "Z", title: "翻滚倾斜 (Roll)", desc: "左右侧翻物体", color: "#4d9eff" };
    }
  }

  // Scale Mode
  if (mode === "scale") {
    return {
      axis: axis,
      title: "等比缩放 (Scale)",
      desc: "沿所选方向拖拽放大或缩小物体尺寸",
      color: "#ffb84d",
    };
  }

  return {
    axis,
    title: `${axis} 轴操控`,
    desc: `操控 ${axis} 方向`,
    color: "#d8b878",
  };
}

