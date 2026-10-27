// 角色占位模型的姿态：一套很简单的关节骨架（躯干、头、两臂、两腿）加几种预设。
// 只为构图和站位服务——告诉摄影机“这个人是坐着还是在跑”，不是动画，也不代表最终动作细节。

/** 编号与「提示词导演台」人物动作下拉框的取值一致，送过去时可以直接用。 */
export type PoseId =
  | "stand"
  | "walk"
  | "run"
  | "lookback"
  | "lean"
  | "sit"
  | "stretch"
  | "hold"
  | "wave"
  | "squat"
  | "jump";

/** 一侧手臂：前抬（度，正数向前）、外展（度，正数离开身体）、屈肘（度）。 */
export type Arm = { raise: number; out: number; elbow: number };
/** 一条腿：屈髋（度，正数大腿向前抬）、屈膝（度）。 */
export type Leg = { hip: number; knee: number };
export type Pose = {
  label: string;
  lean: number; // 躯干前倾（度，负数后仰）
  twist: number; // 躯干左右转（度）
  headYaw: number; // 转头（度）
  headPitch: number; // 低头为正（度）
  left: { arm: Arm; leg: Leg };
  right: { arm: Arm; leg: Leg };
  lift: number; // 双脚离地高度（米）
};

const arm = (raise = 0, out = 7, elbow = 0): Arm => ({ raise, out, elbow });
const leg = (hip = 0, knee = 0): Leg => ({ hip, knee });
const pose = (label: string, patch: Partial<Pose> = {}): Pose => ({
  label,
  lean: 0,
  twist: 0,
  headYaw: 0,
  headPitch: 0,
  left: { arm: arm(), leg: leg() },
  right: { arm: arm(), leg: leg() },
  lift: 0,
  ...patch,
});

export const POSES: Record<PoseId, Pose> = {
  stand: pose("站立"),
  walk: pose("行走", {
    lean: 3,
    left: { arm: arm(-22, 6, 15), leg: leg(24, 8) },
    right: { arm: arm(22, 6, 20), leg: leg(-18, 22) },
  }),
  run: pose("奔跑", {
    lean: 14,
    left: { arm: arm(-45, 8, 90), leg: leg(55, 75) },
    right: { arm: arm(50, 8, 90), leg: leg(-28, 55) },
  }),
  lookback: pose("回眸", {
    twist: 22,
    headYaw: 68,
    left: { arm: arm(-6, 8, 8), leg: leg() },
    right: { arm: arm(8, 8, 12), leg: leg(6, 8) },
  }),
  lean: pose("倚靠", {
    lean: -9,
    left: { arm: arm(25, 4, 95), leg: leg(-6, 0) },
    right: { arm: arm(25, 4, 95), leg: leg(28, 55) },
  }),
  sit: pose("坐下", {
    lean: 4,
    left: { arm: arm(32, 6, 62), leg: leg(90, 90) },
    right: { arm: arm(32, 6, 62), leg: leg(90, 90) },
  }),
  stretch: pose("伸展身体", {
    lean: -6,
    headPitch: -12,
    left: { arm: arm(168, 14, 8), leg: leg() },
    right: { arm: arm(168, 14, 8), leg: leg() },
  }),
  hold: pose("手持物品", {
    headPitch: 12,
    left: { arm: arm(0, 7, 5), leg: leg() },
    right: { arm: arm(42, 4, 82), leg: leg() },
  }),
  wave: pose("挥手", {
    left: { arm: arm(0, 7, 5), leg: leg() },
    right: { arm: arm(0, 142, 38), leg: leg() },
  }),
  squat: pose("蹲下", {
    lean: 26,
    left: { arm: arm(48, 8, 55), leg: leg(112, 142) },
    right: { arm: arm(48, 8, 55), leg: leg(112, 142) },
  }),
  jump: pose("跳跃", {
    lean: 5,
    lift: 0.35,
    left: { arm: arm(150, 20, 15), leg: leg(22, 55) },
    right: { arm: arm(150, 20, 15), leg: leg(8, 40) },
  }),
};
export const POSE_IDS = Object.keys(POSES) as PoseId[];
export const isPoseId = (value: unknown): value is PoseId =>
  typeof value === "string" && Object.prototype.hasOwnProperty.call(POSES, value);
export const poseOf = (id: PoseId | undefined): Pose => POSES[id ?? "stand"] ?? POSES.stand;

// ---- 骨架尺寸（米），合起来站直是 1.8 米 ----
export const RIG = {
  thigh: 0.43,
  shin: 0.47,
  torso: 0.52, // 髋到肩
  shoulderX: 0.27,
  hipX: 0.12,
  upperArm: 0.3,
  forearm: 0.28,
  neck: 0.1,
  head: 0.16, // 头的半径
};
const rad = (deg: number) => (deg * Math.PI) / 180;

/** 一条腿从髋到脚底向下伸出多少（米）。 */
function legReach(l: Leg) {
  return RIG.thigh * Math.cos(rad(l.hip)) + RIG.shin * Math.cos(rad(l.hip - l.knee));
}
/** 髋部离地高度：踩得最低的那只脚落在地面上，再加上离地高度。 */
export function hipHeight(p: Pose) {
  return Math.max(0.2, legReach(p.left.leg), legReach(p.right.leg)) + p.lift;
}
/** 这个姿态的大致包围盒（宽、高、深）和中心，供自动构图和空间检查使用。 */
export function poseBounds(id: PoseId | undefined): { size: [number, number, number]; center: [number, number, number] } {
  // 站立沿用占位人一直以来的尺寸，旧项目的自动构图不变
  if ((id ?? "stand") === "stand") return { size: [0.52, 1.8, 0.4], center: [0, 0.9, 0] };
  const p = poseOf(id);
  const hip = hipHeight(p);
  const lean = rad(p.lean);
  const spine = RIG.torso + RIG.neck + RIG.head * 1.6;
  let top = hip + spine * Math.cos(lean);
  let depth = 0.4 + Math.abs(spine * Math.sin(lean));
  let width = 0.52;
  for (const side of [p.left, p.right]) {
    const a = side.arm;
    const up = RIG.torso * Math.cos(lean) - (RIG.upperArm + RIG.forearm) * Math.cos(rad(Math.max(a.raise, a.out)));
    top = Math.max(top, hip + up);
    width = Math.max(width, 2 * (RIG.shoulderX + (RIG.upperArm + RIG.forearm) * Math.sin(rad(Math.min(a.out, 90)))));
    depth = Math.max(depth, 0.2 + RIG.thigh * Math.abs(Math.sin(rad(side.leg.hip))) + 0.2);
  }
  const round = (n: number) => Math.round(n * 100) / 100;
  const bottom = p.lift;
  const height = round(top - bottom);
  return { size: [round(width), height, round(depth)], center: [0, round(bottom + height / 2), 0] };
}
