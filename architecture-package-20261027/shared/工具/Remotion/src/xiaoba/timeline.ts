// 小八 AgentOS 成长历程：全片的唯一时间表。画面和声音都按这里的帧号对齐（30 fps）。
// 事实出处：私人助理/第二大脑/历史版本记录、Playbook、配置/环境索引.md（2026-10-14 读取）。

export const FPS = 30;
export const DURATION = 2250; // 75 秒
export const Y = 540; // 主线所在高度（世界坐标）

export const COLORS = {
  paper: "#F7FAFF", // 微微偏蓝的白
  ink: "#2F4266", // 深藏青：线和字
  soft: "#BCD3F2", // 浅蓝：块面
  softer: "#E3EDFB",
  accent: "#3F6FC2", // 深蓝：只给用户裁决的印章
  mute: "#7C8BA6",
};

// 一段线：d 为世界坐标路径，在 [from, to] 帧内画完；erase 给出时在该区间内收回。
export type Stroke = {
  id: string;
  d: string;
  from: number;
  to: number;
  erase?: [number, number];
  width?: number;
  color?: string;
  fill?: string;
  fillFrom?: number; // 填色出现的帧
};

const up = 440;
const down = 640;

export const STROKES: Stroke[] = [
  // 09.04 v1.1：主线、任务队列、两路并行、裁判
  { id: "spine1", d: `M600 ${Y} L1450 ${Y}`, from: 180, to: 300 },
  ...[1130, 1240, 1350].map((x, i) => ({
    id: `queue${i}`,
    d: `M${x + 16} 505 H${x + 54} Q${x + 70} 505 ${x + 70} 521 V559 Q${x + 70} 575 ${x + 54} 575 H${x + 16} Q${x} 575 ${x} 559 V521 Q${x} 505 ${x + 16} 505 Z`,
    from: 238 + i * 18,
    to: 262 + i * 18,
    fill: COLORS.soft,
    fillFrom: 262 + i * 18,
  })),
  { id: "split1Up", d: `M1450 ${Y} C1505 ${Y} 1505 ${up} 1560 ${up} L2330 ${up} C2395 ${up} 2395 ${Y} 2448 ${Y}`, from: 300, to: 408 },
  { id: "split1Down", d: `M1450 ${Y} C1505 ${Y} 1505 ${down} 1560 ${down} L2330 ${down} C2395 ${down} 2395 ${Y} 2448 ${Y}`, from: 300, to: 408 },
  {
    id: "judge",
    d: `M2470 ${Y - 22} A22 22 0 1 1 2469.9 ${Y - 22} Z`,
    from: 405,
    to: 425,
    fill: COLORS.soft,
    fillFrom: 422,
  },
  // 09.07 v1.3 之后主线继续
  { id: "spine3", d: `M2492 ${Y} L2700 ${Y}`, from: 480, to: 540 },
  // 09.09 v1.4：一方断开、另一方冻结、修复
  { id: "split2UpA", d: `M2700 ${Y} C2755 ${Y} 2755 ${up} 2810 ${up} L3150 ${up}`, from: 600, to: 650 },
  { id: "split2Down", d: `M2700 ${Y} C2755 ${Y} 2755 ${down} 2810 ${down} L3700 ${down}`, from: 600, to: 700 },
  { id: "break", d: `M3150 ${up} l14 -14 l12 22 l12 -18`, from: 650, to: 662, erase: [748, 760] },
  { id: "bracketL", d: `M3030 ${down - 38} h-18 v76 h18`, from: 680, to: 694, erase: [800, 812] },
  { id: "bracketR", d: `M3470 ${down - 38} h18 v76 h-18`, from: 684, to: 698, erase: [800, 812] },
  { id: "split2UpB", d: `M3150 ${up} L3700 ${up}`, from: 752, to: 792 },
  // 09.26 v1.4.2：线继续，换了后台
  { id: "pairUp", d: `M3700 ${up} L4700 ${up} C4765 ${up} 4765 ${Y} 4800 ${Y}`, from: 810, to: 960 },
  { id: "pairDown", d: `M3700 ${down} L4700 ${down} C4765 ${down} 4765 ${Y} 4800 ${Y}`, from: 810, to: 960 },
  {
    id: "judge2",
    d: `M4822 ${Y - 22} A22 22 0 1 1 4821.9 ${Y - 22} Z`,
    from: 958,
    to: 978,
    fill: COLORS.soft,
    fillFrom: 975,
  },
  // 10.05 监看中心：一只眼睛
  { id: "spine5", d: `M4844 ${Y} L5900 ${Y}`, from: 1000, to: 1100 },
  { id: "eye", d: `M5150 ${Y} Q5450 ${Y - 250} 5750 ${Y} Q5450 ${Y + 250} 5150 ${Y} Z`, from: 1040, to: 1120 },
  // 10.08 v1.5.0：先有三个中间层，再把它们收回
  { id: "spine6", d: `M5900 ${Y} L7000 ${Y}`, from: 1200, to: 1290 },
  ...[6150, 6420, 6690].map((x, i) => ({
    id: `middle${i}`,
    d: `M${x + 18} 505 H${x + 152} Q${x + 170} 505 ${x + 170} 523 V557 Q${x + 170} 575 ${x + 152} 575 H${x + 18} Q${x} 575 ${x} 557 V523 Q${x} 505 ${x + 18} 505 Z`,
    from: 1212 + i * 22,
    to: 1240 + i * 22,
    erase: [1322 + i * 16, 1350 + i * 16] as [number, number],
    fill: COLORS.softer,
    fillFrom: 1240 + i * 22,
  })),
  // 10.10 — 10.13：长出第二大脑，再搬出发布包
  { id: "spine7", d: `M7000 ${Y} L8100 ${Y}`, from: 1500, to: 1580 },
  { id: "stem", d: `M7550 ${Y} L7550 640`, from: 1560, to: 1580, erase: [1690, 1712] },
  { id: "branchL", d: `M7550 640 C7550 700 7400 690 7400 760 C7400 800 7330 800 7320 850`, from: 1580, to: 1630 },
  { id: "branchL2", d: `M7400 760 C7400 800 7470 800 7480 850`, from: 1600, to: 1636 },
  { id: "branchR", d: `M7550 640 C7550 700 7700 690 7700 760 C7700 800 7630 800 7620 850`, from: 1584, to: 1634 },
  { id: "branchR2", d: `M7700 760 C7700 800 7770 800 7790 850`, from: 1604, to: 1640 },
  { id: "ground", d: `M7230 968 Q7550 958 7870 968`, from: 1700, to: 1732 },
  // 10.14：环境搬进「配置」
  { id: "spine8", d: `M8100 ${Y} L8700 ${Y}`, from: 1740, to: 1780 },
  {
    id: "peizhi",
    d: `M8730 470 H8930 Q8960 470 8960 500 V580 Q8960 610 8930 610 H8730 Q8700 610 8700 580 V500 Q8700 470 8730 470 Z`,
    from: 1772,
    to: 1812,
    fill: COLORS.soft,
    fillFrom: 1808,
  },
  { id: "spine8b", d: `M8960 ${Y} L9300 ${Y}`, from: 1840, to: 1872 },
  // 结尾：一笔写成「8」
  {
    id: "eight",
    d: `M9300 ${Y} C9390 ${Y} 9450 552 9500 ${Y} C9600 500 9612 338 9500 334 C9388 330 9392 492 9500 ${Y} C9622 602 9632 792 9500 796 C9368 800 9378 600 9500 ${Y}`,
    from: 1890,
    to: 2010,
    width: 7,
  },
];

// 第二大脑的叶子：34 个点（经验 34 条）
export const LEAVES = Array.from({ length: 34 }, (_, i) => {
  const cols = [7320, 7480, 7620, 7790];
  const col = cols[i % 4];
  const row = Math.floor(i / 4);
  const jitterX = ((i * 37) % 23) - 11;
  const jitterY = ((i * 53) % 17) - 8;
  return {
    x: col + jitterX + (row % 2 === 0 ? -14 : 14),
    y: 862 + row * 8 + jitterY,
    r: 7 + ((i * 7) % 4),
    at: 1610 + i * 2,
  };
});

// 世界里的标注字：出现与消失帧
export type Label = {
  text: string;
  x: number;
  y: number;
  from: number;
  to?: number;
  size?: number;
  color?: string;
};

export const LABELS: Label[] = [
  { text: "排队的任务", x: 1275, y: 625, from: 270 },
  { text: "Codex", x: 1940, y: 405, from: 330 },
  { text: "Claude", x: 1940, y: 692, from: 330 },
  { text: "裁判", x: 2470, y: 610, from: 422, to: 446 },
  { text: "冻结保存", x: 3250, y: 712, from: 694, to: 806 },
  { text: "Codex", x: 4200, y: 405, from: 820, to: 905 },
  { text: "Claude", x: 4200, y: 692, from: 820, to: 905 },
  { text: "AGY", x: 4200, y: 405, from: 910 },
  { text: "DSH", x: 4200, y: 692, from: 910 },
  { text: "Headroom", x: 6235, y: 548, from: 1236, to: 1328, size: 26 },
  { text: "CC Switch", x: 6505, y: 548, from: 1258, to: 1344, size: 26 },
  { text: "8766 网关", x: 6775, y: 548, from: 1280, to: 1360, size: 26 },
  { text: "第二大脑 · 技能", x: 7550, y: 1016, from: 1716 },
  { text: "配置/", x: 8830, y: 552, from: 1806, size: 40, color: "#2F4266" },
];

// 画面左上角的说明字：日期、标题、补充
export type Caption = { from: number; to: number; date: string; title: string; sub?: string };

export const CAPTIONS: Caption[] = [
  { from: 214, to: 412, date: "2026.09.04 · v1.1", title: "任务开始排队", sub: "两个 Agent 做同一件事，裁判择优；至少两个成功才交付" },
  { from: 430, to: 592, date: "2026.09.07 · v1.3", title: "立下规矩：用户是唯一的最终裁决者", sub: "那一版有 104 项测试" },
  { from: 610, to: 802, date: "2026.09.09 · v1.4", title: "一方倒下，另一方的成果先冻结保存", sub: "修好、探测通过之后，再由用户解冻" },
  { from: 820, to: 1010, date: "2026.09.26 · v1.4.2", title: "换了两位经理，线没有断", sub: "后台从 Codex / Claude 换成 AGY / DSH" },
  { from: 1030, to: 1192, date: "2026.10.05", title: "有了监看中心", sub: "看得见，才管得住" },
  { from: 1218, to: 1402, date: "2026.10.08 · v1.5.0", title: "做减法", sub: "拿掉三个中间层，两个后台直连" },
  { from: 1412, to: 1496, date: "2026.10.08", title: "同一天，我有了名字：小克" },
  { from: 1512, to: 1736, date: "2026.10.10 — 10.13 · v1.6", title: "经验长成了第二大脑", sub: "34 条经验，34 条踩坑；规则和技能搬出发布包，改它们不用再发版" },
  { from: 1752, to: 1886, date: "2026.10.14", title: "所有运行环境搬进「配置」", sub: "883 项测试" },
];

// 镜头：笔尖所在的世界 x（镜头把它放在画面中央）
export const CAMERA: [number, number][] = [
  [0, 600],
  [186, 600],
  [300, 1420],
  [420, 2470],
  [470, 2470],
  [560, 2700],
  [720, 3330],
  [800, 3600],
  [1000, 4720],
  [1040, 5150],
  [1130, 5450],
  [1200, 5880],
  [1300, 6560],
  [1410, 6900],
  [1500, 7050],
  [1600, 7550],
  [1740, 7700],
  [1830, 8830],
  [1890, 9150],
  [2040, 9500],
];

// 结尾拉远：从笔尖拉到整条线
export const PULLBACK = { from: 2044, to: 2134, centerX: 5110, centerY: 600, scale: 0.18 };
