// engine/world.mjs — 「一个持续存在的世界 + 一台摄像机」（v1.0）
//
// 为什么：「场景表、一次只画一个场景」在结构上就是幻灯片，上一段的东西没法带到下一段。
// 这里所有东西都是持续存在的物体，有自己的位置和尺寸（世界坐标），随时间按关键帧变化；
// 一台摄像机在世界里推、拉、跟、穿越。段落之间靠「物体变形 / 缩进 / 让位」和「镜头运动」接力，而不是换页。
//
// 用法：
//   import { World, words } from './engine/world.mjs';
//   const T = words(new URL('./whisper/narration.json', import.meta.url));  // 时间锚在配音的字上
//   export const WORLD = new World();
//   WORLD.add('cup', { x: 90, y: 300, w: 900, h: 1200, z: 1, draw: (c, o, t) => { ... 在 (0,0,o.w,o.h) 里画 ... } });
//   WORLD.to('cup', T.at('一个月'), 0.6, { x: 700, y: 400, w: 120, h: 160 });      // 缩进日历格
//   WORLD.camTo(T.at('一个月'), 0.8, { zoom: 0.4 }, E.inOut);                       // 同时拉远
//   export function drawFrame(ctx, t) { WORLD.render(ctx, t); }
import fs from 'node:fs';
import { W, H, E, clamp, lerp, prog, canvas, fresh, noise1 } from './lib.mjs';

const PROPS = ['x', 'y', 'w', 'h', 'alpha', 'rot', 'scale'];

export class World {
  constructor(o = {}) {
    this.objs = new Map();
    this.camKeys = [{ t: 0, x: W / 2, y: H / 2, zoom: 1, rot: 0 }];
    this.holds = []; this.hits = []; this.hand = o.hand ?? 2.5;
    this.bg = o.bg ?? '#07080C';
    this.ground = o.ground ?? null;        // (ctx, t, cam) 画在世界里、所有物体之下（纸纹、网格）
    this.overlay = o.overlay ?? null;      // (ctx, t) 画在屏幕坐标里（字幕、角落注释）
  }

  // ---- 物体
  add(id, o) {
    const base = { x: 0, y: 0, w: 100, h: 100, alpha: 1, rot: 0, scale: 1, z: 0, born: -Infinity, die: Infinity, ...o };
    base.keys = PROPS.map(p => [p, [{ t: -Infinity, v: base[p], ease: E.lin }]]);
    base.keys = Object.fromEntries(base.keys);
    this.objs.set(id, base);
    return this;
  }
  // 从 t0 开始、dur 秒内，把物体的属性变到 props（用 ease）。可以连续调用，排成一条时间线。
  to(id, t0, dur, props, ease = E.inOut) {
    const o = this.objs.get(id); if (!o) throw new Error(`没有物体 ${id}`);
    for (const [p, v] of Object.entries(props)) {
      if (!PROPS.includes(p)) continue;
      const k = o.keys[p];
      const from = this._val(o, p, t0);
      k.push({ t: t0, v: from, ease: E.lin }, { t: t0 + dur, v, ease });
      k.sort((a, b) => a.t - b.t);
    }
    return this;
  }
  set(id, t0, props) { return this.to(id, t0, 1e-4, props, E.lin); }
  life(id, born, die = Infinity) { const o = this.objs.get(id); o.born = born; o.die = die; return this; }
  _val(o, p, t) {
    const k = o.keys[p]; let v = k[0].v;
    for (let i = 1; i < k.length; i++) {
      const a = k[i - 1], b = k[i];
      if (t >= b.t) { v = b.v; continue; }
      if (t > a.t) v = isFinite(a.t) ? lerp(a.v, b.v, b.ease(prog(t, a.t, b.t))) : a.v;
      break;
    }
    return v;
  }
  state(id, t) {
    const o = this.objs.get(id), s = {};
    for (const p of PROPS) s[p] = this._val(o, p, t);
    return s;
  }
  // 物体在世界里的矩形（含 scale）。检查脚本用它量比例。
  rectAt(id, t) {
    const s = this.state(id, t), w = s.w * s.scale, h = s.h * s.scale;
    return { x: s.x + (s.w - w) / 2, y: s.y + (s.h - h) / 2, w, h };
  }

  // ---- 摄像机（世界坐标：x/y 是画面中心对准的点）
  camTo(t0, dur, props, ease = E.inOut) {
    const prev = this.camAt(t0, true);
    this.camKeys.push({ t: t0, ...prev, ease: E.lin }, { t: t0 + dur, ...prev, ...props, ease });
    this.camKeys.sort((a, b) => a.t - b.t);
    return this;
  }
  hold(t0, t1) { this.holds.push([t0, t1]); return this; }        // 静止段：镜头完全不动
  hit(t, amp = 12) { this.hits.push([t, amp]); return this; }      // 落地 / 撞击震动
  camAt(t, raw = false) {
    const k = this.camKeys; let c = { ...k[0] };
    for (let i = 1; i < k.length; i++) {
      const a = k[i - 1], b = k[i];
      if (t >= b.t) { c = { ...b }; continue; }
      if (t > a.t) { const u = b.ease(prog(t, a.t, b.t)); c = { x: lerp(a.x, b.x, u), y: lerp(a.y, b.y, u), zoom: lerp(a.zoom, b.zoom, u), rot: lerp(a.rot ?? 0, b.rot ?? 0, u) }; }
      break;
    }
    if (raw) return { x: c.x, y: c.y, zoom: c.zoom, rot: c.rot ?? 0 };
    let calm = 1;
    for (const [a, b] of this.holds) calm = Math.min(calm, 1 - prog(t, a - 0.25, a) + prog(t, b, b + 0.25));
    calm = clamp(calm);
    c.x += calm * this.hand * (Math.sin(1.6 * t) + 0.5 * Math.sin(2.7 * t + 1.3)) / c.zoom;
    c.y += calm * this.hand * (Math.sin(1.2 * t + 2.1) + 0.5 * Math.sin(3.1 * t)) / c.zoom;
    for (const [th, amp] of this.hits) {
      const d = t - th; if (d < 0 || d > 0.5) continue;
      const k2 = amp * Math.exp(-d * 11);
      c.x += k2 * noise1(d * 40, th * 7) / c.zoom; c.y += k2 * noise1(d * 40 + 50, th * 7) / c.zoom;
    }
    return c;
  }
  // 世界坐标 → 屏幕坐标（检查脚本和字幕定位用）
  toScreen(t, x, y) {
    const c = this.camAt(t), cs = Math.cos(c.rot ?? 0), sn = Math.sin(c.rot ?? 0);
    const dx = (x - c.x) * c.zoom, dy = (y - c.y) * c.zoom;
    return { x: W / 2 + dx * cs - dy * sn, y: H / 2 + dx * sn + dy * cs };
  }

  // ---- 画一帧
  render(ctx, t) {
    ctx.save(); ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = typeof this.bg === 'function' ? this.bg(t) : this.bg; ctx.fillRect(0, 0, W, H);
    const c = this.camAt(t);
    ctx.translate(W / 2, H / 2); ctx.rotate(c.rot ?? 0); ctx.scale(c.zoom, c.zoom); ctx.translate(-c.x, -c.y);
    if (this.ground) this.ground(ctx, t, c);
    const list = [...this.objs.entries()].filter(([, o]) => t >= o.born && t < o.die).sort((a, b) => a[1].z - b[1].z);
    for (const [id, o] of list) {
      const s = this.state(id, t); if (s.alpha <= 0.001) continue;
      ctx.save();
      ctx.globalAlpha *= clamp(s.alpha);
      ctx.translate(s.x + s.w / 2, s.y + s.h / 2); ctx.rotate(s.rot); ctx.scale(s.scale, s.scale); ctx.translate(-s.w / 2, -s.h / 2);
      o.draw(ctx, { ...s, id }, t);
      ctx.restore();
    }
    ctx.restore();
    if (this.overlay) { ctx.save(); this.overlay(ctx, t); ctx.restore(); }
  }
}

// ---------------------------------------------------------------- 接力动作（段落之间用这些，不用换页）
// 每个函数只是往世界里写关键帧，返回动作结束的时间。
export const carry = {
  // 物体缩进一个格子 / 记录 / 图标（如：一杯水缩进日历的一格）
  shrinkInto(w, id, t0, slot, dur = 0.6) { w.to(id, t0, dur, slot, E.inOut); return t0 + dur; },
  // 物体长成下一个场景（如：一张卡片展开成整页网页）
  growInto(w, id, t0, rect, dur = 0.6) { w.to(id, t0, dur, rect, E.outQuint); return t0 + dur; },
  // 镜头拉远，揭示「原来前面是一个大东西的局部」
  pullOut(w, t0, zoom = 0.4, at = null, dur = 0.9) { w.camTo(t0, dur, { zoom, ...(at || {}) }, E.inOut); return t0 + dur; },
  // 镜头推进一个物体，直到它填满画面、成为下一个场景
  pushInto(w, id, t0, dur = 0.7, fill = 0.92) {
    const r = w.rectAt(id, t0 + dur);
    const zoom = Math.min(W / r.w, H / r.h) * fill;
    w.camTo(t0, dur, { x: r.x + r.w / 2, y: r.y + r.h / 2, zoom }, E.inC); return t0 + dur;
  },
  // 镜头快速对准一个将要落地的点（追，不是跟）
  snapTo(w, t0, x, y, zoom, dur = 0.4) { w.camTo(t0, dur, { x, y, zoom }, E.outExpo); w.hit(t0 + dur, 10); return t0 + dur; },
  // 其他物体依次让位（往两侧滑开 + 变淡），给主角腾地方
  makeWay(w, ids, t0, dx = 900, stagger = 0.06, dur = 0.45) {
    ids.forEach((id, i) => { const s = w.state(id, t0); w.to(id, t0 + i * stagger, dur, { x: s.x + (i % 2 ? dx : -dx), alpha: 0 }, E.inC); });
    return t0 + ids.length * stagger + dur;
  },
  // 按比例重画：用前面的物体当尺子（如：2000 ml 的水位 = 500 ml 的 4 倍高）
  scaleAs(w, id, refId, t0, ratio, axis = 'h', dur = 0.8) {
    const r = w.state(refId, t0); w.to(id, t0, dur, axis === 'h' ? { h: r.h * ratio } : { w: r.w * ratio }, E.outQuint); return t0 + dur;
  },
};

// ---------------------------------------------------------------- 时间锚在配音的字上
// whisper 的逐词 JSON → T.at('关键词', 第几次=1) 返回这个词开始的秒数；T.end('关键词') 返回结束时间。
// 不要在代码里写死秒数：配音一改，所有画面跟着走。
export function words(url) {
  const d = JSON.parse(fs.readFileSync(url));
  const ws = [];
  for (const s of d.segments || []) for (const x of s.words || []) ws.push({ w: x.word.trim(), s: x.start, e: x.end });
  const text = ws.map(x => x.w).join('');
  const find = (q, n = 1) => {
    q = q.replace(/\s/g, '');
    let from = -1;
    for (let i = 0; i < n; i++) { from = text.indexOf(q, from + 1); if (from < 0) throw new Error(`配音里找不到「${q}」第 ${n} 次`); }
    let acc = 0, si = 0, ei = 0;
    for (let i = 0; i < ws.length; i++) { if (acc <= from) si = i; acc += ws[i].w.length; if (acc >= from + q.length) { ei = i; break; } }
    return [ws[si].s, ws[ei].e];
  };
  return { at: (q, n) => find(q, n)[0], end: (q, n) => find(q, n)[1], list: ws };
}
