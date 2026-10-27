// demo/film.mjs — 6 秒小样片：示范「一个持续存在的世界 + 一台摄像机」怎么写。
// 内容是随手编的（喝水），只为演示引擎用法；不要照搬它的构思。
// 正片里时间要用 words() 锚在配音的字上，这里没有配音所以写了秒数。
//
// 0.0  桌上一杯水；0.4「一杯」砸进来
// 1.4  这杯水缩进 30 格日历的一格，同时镜头拉远 → 原来它只是一个月里的一天（接力：缩进 + 拉远）
// 3.0  500 ml 和 2000 ml 两个水位：2000 那个用 500 当尺子，按 4 倍长出来（比例检查）
// 4.4  镜头推进右下的便签，它变成下一个场景（接力：推进）；5.0 落地震动
import { W, H, C, F, E, prog, txt, rrect, slamText } from './engine/lib.mjs';
import { World, carry } from './engine/world.mjs';

export const DURATION = 6;
export const WORLD = new World({ bg: '#EEF2F3' });
const ink = '#18222A', water = '#4C9BD6', accent = '#E8572A';

const glass = (c, o, level = 0.7) => {
  c.fillStyle = 'rgba(255,255,255,0.9)'; rrect(c, 0, 0, o.w, o.h, o.w * 0.08); c.fill();
  c.fillStyle = water; c.fillRect(o.w * 0.06, o.h * (1 - level), o.w * 0.88, o.h * level - o.h * 0.04);
  c.strokeStyle = ink; c.lineWidth = Math.max(2, o.w * 0.02); rrect(c, 0, 0, o.w, o.h, o.w * 0.08); c.stroke();
};

WORLD.add('month', { x: -1100, y: -1200, w: 3280, h: 3760, z: 0, alpha: 0, draw: (c) => {
  for (let i = 0; i < 30; i++) { const cx = (i % 5) * 656, cy = Math.floor(i / 5) * 627; c.strokeStyle = 'rgba(24,34,42,0.15)'; c.lineWidth = 6; c.strokeRect(cx + 20, cy + 20, 616, 587);
    txt(c, String(i + 1), cx + 50, cy + 110, { size: 80, color: 'rgba(24,34,42,0.35)', fam: F.din }); }
} });
WORLD.add('cup', { x: 290, y: 420, w: 500, h: 760, z: 2, draw: (c, o) => glass(c, o) });
WORLD.add('label', { x: 140, y: 1260, w: 800, h: 260, z: 3, draw: (c, o, t) => {
  rrect(c, 0, 0, o.w, o.h, 30); c.fillStyle = accent; c.fill();
  slamText(c, '一杯。', 60, 180, t, 0.4, { size: 140, weight: 800, color: '#fff' });
} });
WORLD.add('ml500', { x: 160, y: 1000, w: 300, h: 150, z: 2, alpha: 0, draw: (c, o) => { glass(c, o, 0.9); txt(c, '500 ml', 0, -24, { size: 52, color: ink, weight: 700 }); } });
WORLD.add('ml2000', { x: 600, y: 1000, w: 300, h: 150, z: 2, alpha: 0, draw: (c, o) => { glass(c, o, 0.9); txt(c, '2000 ml', 0, -24, { size: 52, color: accent, weight: 700 }); } });
WORLD.add('note', { x: 620, y: 1520, w: 300, h: 380, z: 4, alpha: 0, draw: (c, o) => {
  rrect(c, 0, 0, o.w, o.h, 16); c.fillStyle = '#FFF6C9'; c.fill(); c.strokeStyle = ink; c.lineWidth = 4; c.stroke();
  txt(c, '还差 3 杯', 28, 90, { size: 44, color: ink, weight: 800 });
  for (let i = 0; i < 3; i++) { c.strokeStyle = water; c.lineWidth = 6; c.strokeRect(28 + i * 86, 150, 64, 100); }
} });

// ---- 时间线
WORLD.hold(0.6, 1.3);                                        // 「一杯」之后停顿：镜头完全不动
WORLD.to('label', 1.05, 0.25, { alpha: 0 }, E.inC);
WORLD.set('month', 1.3, { alpha: 1 });
carry.shrinkInto(WORLD, 'cup', 1.4, { x: 420, y: 1432, w: 240, h: 380 }, 0.7);  // 缩进第 23 格
carry.pullOut(WORLD, 1.4, 0.3, { x: 540, y: 960 }, 0.9);                               // 同时拉远
WORLD.camTo(2.6, 0.5, { zoom: 1, x: 540, y: 960 }, E.inOut);
WORLD.to('month', 2.6, 0.4, { alpha: 0 }, E.inC);
WORLD.to('cup', 2.6, 0.3, { alpha: 0 }, E.inC);
WORLD.to('ml500', 2.8, 0.3, { alpha: 1 }, E.outC);
WORLD.to('ml2000', 3.0, 0.3, { alpha: 1 }, E.outC);
WORLD.to('ml2000', 3.1, 0.8, { y: 1000 - 450 }, E.outQuint);                           // 往上长，底边不动
carry.scaleAs(WORLD, 'ml2000', 'ml500', 3.1, 4, 'h', 0.8);                             // 用 500 当尺子
WORLD.to('note', 4.0, 0.3, { alpha: 1 }, E.outC);
carry.pushInto(WORLD, 'note', 4.4, 0.6);                                               // 推进便签，它成为下一个场景
WORLD.hit(5.0, 14);

export const QUANTITIES = [{ label: '2000 ml vs 500 ml', t: 4.0, id: 'ml2000', ref: 'ml500', ratio: 4, axis: 'h' }];
export const TIMELINE = [{ text: '一杯。', in: 0.4, out: 1.3, kind: 'word' }];
export function drawFrame(ctx, t) { WORLD.render(ctx, t); }
