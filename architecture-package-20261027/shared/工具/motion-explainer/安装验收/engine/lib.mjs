// engine/lib.mjs — 通用画面引擎。
// 约定：每一帧都是时间的纯函数 drawFrame(ctx, t)。同一个 t 永远画出同一帧。
import { Canvas, CanvasRenderingContext2D } from 'skia-canvas';
import fs from 'node:fs';

// Skia 遇到接近 0 的 scale 会把整帧丢掉（整屏变黑）。从 0 弹出的动画必须经过这个保护。
{
  const P = CanvasRenderingContext2D.prototype, orig = P.scale;
  const fix = v => (Number.isFinite(v) ? (Math.abs(v) < 1e-3 ? (v < 0 ? -1e-3 : 1e-3) : v) : 1);
  P.scale = function (x, y = x) { return orig.call(this, fix(x), fix(y)); };
}

// ---------------------------------------------------------------- 画布与安全区
// 画幅开关：ASPECT=9:16（默认，竖版 1080×1920）或 16:9（横版 1920×1080）。
// 读取顺序：环境变量 ASPECT → 项目根目录的 aspect.txt（new_project.sh 建项目时写好）→ 默认 9:16。
// 渲染、拼板、体检都从这里取 W / H / SAFE，所以全项目只有这一个开关。
const ASPECT = (process.env.ASPECT || (() => {
  try { return fs.readFileSync(new URL('../aspect.txt', import.meta.url), 'utf8').trim(); } catch { return '9:16'; }
})());
if (ASPECT !== '9:16' && ASPECT !== '16:9') throw new Error(`ASPECT 只能是 9:16 或 16:9，现在是「${ASPECT}」`);
export const LANDSCAPE = ASPECT === '16:9';
export const W = LANDSCAPE ? 1920 : 1080, H = LANDSCAPE ? 1080 : 1920, FPS = 30;
// 安全区：关键内容只放在这里面。
// 竖版（抖音）：右侧 920 以外是点赞栏，1480 以下是标题/按钮区，220 以上是顶栏。
// 横版：没有平台遮挡，四边各留 5%。
export const SAFE = LANDSCAPE
  ? { l: Math.round(W * 0.05), r: Math.round(W * 0.95), t: Math.round(H * 0.05), b: Math.round(H * 0.95) }
  : { l: 90, r: 920, t: 220, b: 1480 };
export const C = {
  ink: '#07080C', ink2: '#11131A', panel: '#13151C', paper: '#F4F2EC',
  dim: 'rgba(244,242,236,0.55)', faint: 'rgba(244,242,236,0.18)',
  accent: '#FFD43B', line: 'rgba(255,255,255,0.14)',
};
export const F = {
  cjk: '"PingFang SC"', kai: '"Kaiti SC"', song: '"Songti SC"',
  cond: '"Avenir Next Condensed"', din: '"DIN Condensed"', mono: 'Menlo', helv: '"Helvetica Neue"',
};

// ---------------------------------------------------------------- 数学与缓动
export const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
export const lerp = (a, b, t) => a + (b - a) * t;
export const prog = (t, a, b) => clamp((t - a) / (b - a)); // t 在 [a,b] 内从 0 走到 1
export const E = {
  lin: t => t,
  inC: t => t * t * t,                       // 出场 / 加速离开
  outC: t => 1 - (1 - t) ** 3,
  outQ: t => 1 - (1 - t) ** 4,
  outQuint: t => 1 - (1 - t) ** 5,           // 进场首选：快进、慢停
  inOut: t => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2), // 镜头运动
  outExpo: t => (t >= 1 ? 1 : 1 - 2 ** (-10 * t)), // 高亮扫过、数字锁定
  inExpo: t => (t <= 0 ? 0 : 2 ** (10 * t - 10)),
  outBack: (t, s = 1.70158) => (t <= 0 ? 0 : t >= 1 ? 1 : 1 + (s + 1) * (t - 1) ** 3 + s * (t - 1) ** 2), // 弹出，s ≤ 1.8
};
export const lerpRect = (a, b, u) => ({ x: lerp(a.x, b.x, u), y: lerp(a.y, b.y, u), w: lerp(a.w, b.w, u), h: lerp(a.h, b.h, u) });

// 确定性随机（禁止 Math.random，否则每次渲染不一样）
export function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
export function noise1(x, seed = 0) {
  const h = n => { const s = Math.sin(n * 127.1 + seed * 311.7) * 43758.5453; return s - Math.floor(s); };
  const i = Math.floor(x), f = x - i, u = f * f * (3 - 2 * f);
  return lerp(h(i), h(i + 1), u) * 2 - 1;
}

// ---------------------------------------------------------------- 画布池
// 复用离屏画布前必须整块 clearRect，否则 skia 的绘制历史会越积越多、越渲越慢。
export function canvas(w = W, h = H) { return new Canvas(w, h); }
export function fresh(cv) {
  const c = cv.getContext('2d');
  c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, cv.width, cv.height);
  c.globalAlpha = 1; c.filter = 'none'; c.globalCompositeOperation = 'source-over';
  return c;
}

// ---------------------------------------------------------------- 文字
export function setFont(ctx, size, weight = 600, fam = F.cjk) { ctx.font = `${weight} ${size}px ${fam}`; }
// 画一行字，返回宽度。o: size, weight, fam, color, align, alpha, spacing, maxW（超宽自动缩小）
export function txt(ctx, s, x, y, o = {}) {
  const { size = 48, weight = 600, fam = F.cjk, color = C.paper, align = 'left', alpha = 1, spacing = 0, maxW, base = 'alphabetic' } = o;
  let sz = size;
  ctx.save();
  ctx.globalAlpha *= alpha;
  ctx.letterSpacing = `${spacing}px`;
  setFont(ctx, sz, weight, fam);
  if (maxW) { const w = ctx.measureText(s).width; if (w > maxW) { sz = sz * maxW / w; setFont(ctx, sz, weight, fam); } }
  ctx.fillStyle = color; ctx.textAlign = align; ctx.textBaseline = base;
  ctx.fillText(s, x, y);
  const w = ctx.measureText(s).width;
  if (globalThis.__TEXTLOG && ctx.globalAlpha > 0.5) {   // 体检用：记录这段字在屏幕上的位置
    const m = ctx.getTransform(), x0 = align === 'center' ? x - w / 2 : align === 'right' ? x - w : x, y0 = y - sz * 0.85;
    const pts = [[x0, y0], [x0 + w, y0], [x0, y0 + sz], [x0 + w, y0 + sz]].map(([px, py]) => [m.a * px + m.c * py + m.e, m.b * px + m.d * py + m.f]);
    const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
    globalThis.__TEXTLOG.push({ s, x0: Math.min(...xs), y0: Math.min(...ys), x1: Math.max(...xs), y1: Math.max(...ys) });
  }
  ctx.restore();
  return w;
}
export function measure(ctx, s, size, weight = 600, fam = F.cjk, spacing = 0) {
  ctx.save(); ctx.letterSpacing = `${spacing}px`; setFont(ctx, size, weight, fam);
  const w = ctx.measureText(s).width; ctx.restore(); return w;
}
export function rrect(ctx, x, y, w, h, r) { ctx.beginPath(); ctx.roundRect(x, y, w, h, r); }

// Slam 进场：从 from 倍缩到 1，透明度 0→1，模糊 blur→0。0.3 秒，outQuint。大字、关键句专用。
export function slamText(ctx, s, x, y, t, t0, o = {}) {
  const { dur = 0.3, from = 1.3, blur = 14, ...style } = o;
  const k = E.outQuint(prog(t, t0, t0 + dur));
  if (k <= 0) return 0;
  ctx.save();
  ctx.translate(x, y); const sc = lerp(from, 1, k); ctx.scale(sc, sc);
  if (k < 1 && blur) ctx.filter = `blur(${(1 - k) * blur}px)`;
  const w = txt(ctx, s, 0, 0, { ...style, alpha: (style.alpha ?? 1) * k });
  ctx.restore();
  return w;
}
// 轻进场：从下方 dy 像素滑入 + 淡入，0.25 秒。说明文字、次要信息用。
export function riseText(ctx, s, x, y, t, t0, o = {}) {
  const { dur = 0.25, dy = 28, ...style } = o;
  const k = E.outQuint(prog(t, t0, t0 + dur));
  if (k <= 0) return 0;
  return txt(ctx, s, x, y + (1 - k) * dy, { ...style, alpha: (style.alpha ?? 1) * k });
}
// 出场系数：t1 开始，dur 内从 1 掉到 0（inC，出场比进场快）
export const exitK = (t, t1, dur = 0.2) => 1 - E.inC(prog(t, t1, t1 + dur));

// 高亮条扫过（outExpo），画在文字下面。
export function highlight(ctx, x, y, w, h, t, t0, color = C.accent, dur = 0.3, r = 12) {
  const k = E.outExpo(prog(t, t0, t0 + dur));
  if (k <= 0) return;
  ctx.save(); rrect(ctx, x, y, w * k, h, r); ctx.fillStyle = color; ctx.fill(); ctx.restore();
}
// 数字滚动计数：t0→t1 从 0 滚到 value（outQ）
export const countUp = (value, t, t0, t1) => Math.round(value * E.outQ(prog(t, t0, t1)));
// 锁定弹一下：tLock 时放大 1+amp，指数回落
export const lockPop = (t, tLock, amp = 0.25) => (t >= tLock ? 1 + amp * Math.exp(-(t - tLock) * 9) : 1);

// ---------------------------------------------------------------- 底色与压暗
export function scrim(ctx, y0, y1, rgb = '5,6,9', a = 0.85) {
  const g = ctx.createLinearGradient(0, y0, 0, y1);
  g.addColorStop(0, `rgba(${rgb},${a})`); g.addColorStop(0.7, `rgba(${rgb},${a * 0.85})`); g.addColorStop(1, `rgba(${rgb},0)`);
  ctx.fillStyle = g; ctx.fillRect(0, Math.min(y0, y1), W, Math.abs(y1 - y0));
}
export function vignette(ctx, amt = 0.3) {
  const g = ctx.createRadialGradient(W / 2, H / 2, H * 0.25, W / 2, H / 2, H * 0.75);
  g.addColorStop(0, 'rgba(0,0,0,0)'); g.addColorStop(1, `rgba(0,0,0,${amt})`);
  ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
}
export function flash(ctx, t, t0, amt = 0.6, decay = 12) {
  if (t < t0) return; const a = amt * Math.exp(-(t - t0) * decay);
  if (a > 0.01) { ctx.fillStyle = `rgba(255,255,255,${a})`; ctx.fillRect(0, 0, W, H); }
}
export function drawStar(ctx, cx, cy, r, color) {
  ctx.save(); ctx.beginPath();
  for (let i = 0; i < 10; i++) { const a = -Math.PI / 2 + i * Math.PI / 5, rr = i % 2 ? r * 0.45 : r; ctx.lineTo(cx + Math.cos(a) * rr, cy + Math.sin(a) * rr); }
  ctx.closePath(); ctx.fillStyle = color; ctx.fill(); ctx.restore();
}

// ---------------------------------------------------------------- 节拍（画面读配乐的时间表）
export const BPM = 120, BEAT = 0.5, BAR = 2;
export function loadCues(url = new URL('../cues.json', import.meta.url)) {
  try { return JSON.parse(fs.readFileSync(url)); } catch { return { kicks: [], beats: [], events: {} }; }
}
// 每个 kick 画面整体推一下（0.08 秒指数衰减）。用法：const p = 1 + kickPunch(KICKS, t)
export function kickPunch(kicks, t, amt = 0.012) {
  let lo = 0, hi = kicks.length - 1, k = -1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (kicks[m] <= t) { k = m; lo = m + 1; } else hi = m - 1; }
  if (k < 0) return 0;
  const d = t - kicks[k];
  return d < 0.4 ? amt * Math.exp(-d / 0.08) : 0;
}

// ---------------------------------------------------------------- 镜头
// 停留时的慢推：整段从 1 推到 1+amt（默认 2%），只推近、不横向漂移（漂移会把贴边内容推出手机屏）。
export function holdPush(ctx, lt, dur, drawFn, amt = 0.02, punch = 0) {
  const s = 1 + amt * E.inOut(prog(lt, 0, dur)) + punch;
  ctx.save(); ctx.translate(W / 2, H / 2); ctx.scale(s, s); ctx.translate(-W / 2, -H / 2);
  drawFn(ctx); ctx.restore();
}

// ---------------------------------------------------------------- 转场：画布甩镜
// 实测曲线：0.7 秒，慢-快-慢，切点（中点）最快。
const PAN_CURVE = [0, 0.01, 0.03, 0.05, 0.08, 0.12, 0.17, 0.24, 0.33, 0.44, 0.56, 0.67, 0.76, 0.83, 0.89, 0.93, 0.96, 0.98, 0.99, 1, 1];
export function panCurve(u) {
  u = clamp(u) * (PAN_CURVE.length - 1);
  const i = Math.floor(u), f = u - i;
  return lerp(PAN_CURVE[i], PAN_CURVE[Math.min(i + 1, PAN_CURVE.length - 1)], f);
}
export const WHIP = 0.35; // 半长：切点前后各 0.35 秒
const OFF = {};
const off = k => (OFF[k] ||= canvas());
// 两个场景之间甩镜。cut = 切点时间（落在拍点上）；dir = [1,0] 新画面从右边来 / [-1,0] 从左 / [0,1] 从下 / [0,-1] 从上。
// drawA(c, t) / drawB(c, t) 把各自场景画满整屏（自己负责背景色）。带 6 个子帧的运动模糊。
// 用法：if (Math.abs(t - cut) < WHIP) return whip(ctx, t, cut, [1,0], sceneA, sceneB);
export function whip(ctx, t, cut, dir, drawA, drawB, o = {}) {
  const { gap = 70, bg = '#040507', samples = 6, shutter = 0.5 / FPS } = o;
  const ca = fresh(off('a')), cb = fresh(off('b'));
  drawA(ca, t); drawB(cb, t);
  const span = dir[0] ? W + gap : H + gap;
  const tc = off('tmp');
  ctx.save(); ctx.setTransform(1, 0, 0, 1, 0, 0);
  for (let k = 0; k < samples; k++) {
    const ts = t + (k / (samples - 1) - 0.5) * shutter;
    const p = panCurve((ts - (cut - WHIP)) / (2 * WHIP));
    const c = fresh(tc);
    c.fillStyle = bg; c.fillRect(0, 0, W, H);
    c.drawImage(off('a'), -dir[0] * p * span, -dir[1] * p * span);
    c.drawImage(off('b'), dir[0] * (1 - p) * span, dir[1] * (1 - p) * span);
    ctx.globalAlpha = 1 / (k + 1); ctx.drawImage(tc, 0, 0); // 逐个平均
  }
  ctx.restore();
}
// 放大穿越：把场景 B 从矩形 from 放大到全屏（u: 0→1，建议 inC，0.5 秒，结束落在拍点）。
export function zoomThrough(ctx, u, from, drawB, t, r = 18) {
  const cb = fresh(off('b')); drawB(cb, t);
  const full = { x: 0, y: 0, w: W, h: H }, dst = lerpRect(from, full, u);
  ctx.save(); rrect(ctx, dst.x, dst.y, dst.w, dst.h, r * (1 - u)); ctx.clip();
  ctx.drawImage(off('b'), 0, 0, W, H, dst.x, dst.y, dst.w, dst.h); ctx.restore();
}

// ---------------------------------------------------------------- 有「摄影师」的镜头（v0.9）
// 关键帧：[{ t, x, y, zoom, rot, ease }]，x/y 是镜头对准的画面坐标（默认画面中心 W/2, H/2：竖版 540,960，横版 960,540）。
// 跟随主体时，把关键帧放在主体「将要」到达的位置、比主体早约 0.1 秒到位；快速对准用 E.inOut 或 E.outExpo。
export function camKeys(t, keys) {
  if (t <= keys[0].t) return { ...keys[0] };
  for (let i = 1; i < keys.length; i++) {
    const a = keys[i - 1], b = keys[i];
    if (t <= b.t) {
      const u = (b.ease || E.inOut)(prog(t, a.t, b.t));
      return { x: lerp(a.x, b.x, u), y: lerp(a.y, b.y, u), zoom: lerp(a.zoom ?? 1, b.zoom ?? 1, u), rot: lerp(a.rot ?? 0, b.rot ?? 0, u) };
    }
  }
  return { ...keys[keys.length - 1] };
}
// 完整镜头：关键帧 + 手持漂浮（静止段为 0）+ 落地震动。
//   holds: [[t0,t1], ...] 静止段，镜头完全不动；hits: [[t, 强度px], ...] 落地/撞击时间
export function camera(t, keys, o = {}) {
  const { holds = [], hits = [], hand = 3 } = o;
  const c = camKeys(t, keys);
  let calm = 1;
  for (const [a, b] of holds) calm = Math.min(calm, 1 - prog(t, a - 0.25, a) + prog(t, b, b + 0.25));
  calm = clamp(calm);
  const z = c.zoom ?? 1;
  c.x += calm * hand * (Math.sin(1.6 * t) + 0.5 * Math.sin(2.7 * t + 1.3)) / z;
  c.y += calm * hand * (Math.sin(1.2 * t + 2.1) + 0.5 * Math.sin(3.1 * t)) / z;
  for (const [th, amp] of hits) {
    const d = t - th; if (d < 0 || d > 0.5) continue;
    const k = amp * Math.exp(-d * 11);
    c.x += k * noise1(d * 40, th * 7) / z; c.y += k * noise1(d * 40 + 50, th * 7) / z; c.rot = (c.rot ?? 0) + k * 0.0006 * noise1(d * 30 + 9, th);
  }
  return c;
}
// 把镜头套到画布上：之后画的东西都在「世界坐标」里。用完 ctx.restore()。
export function applyCamera(ctx, c) {
  ctx.save();
  ctx.translate(W / 2, H / 2); ctx.rotate(c.rot ?? 0); ctx.scale(c.zoom ?? 1, c.zoom ?? 1); ctx.translate(-(c.x ?? W / 2), -(c.y ?? H / 2));
}

// ---------------------------------------------------------------- 快门运动模糊（v0.9）
// 画面里有东西一帧移动超过约 80px 时用：在快门打开的时间里多画几次再平均，避免出现一串分身。
// draw(c, ts) 必须把整帧画满（含背景）。静止段不用开，省时间。
export function withShutter(ctx, t, draw, o = {}) {
  const { samples = 5, open = 0.5 / FPS } = o;
  const tc = off('shutter');
  ctx.save(); ctx.setTransform(1, 0, 0, 1, 0, 0);
  for (let k = 0; k < samples; k++) {
    const ts = t + (samples === 1 ? 0 : (k / (samples - 1) - 0.5) * open);
    const c = fresh(tc); draw(c, ts);
    ctx.globalAlpha = 1 / (k + 1); ctx.drawImage(tc, 0, 0);
  }
  ctx.restore();
}
