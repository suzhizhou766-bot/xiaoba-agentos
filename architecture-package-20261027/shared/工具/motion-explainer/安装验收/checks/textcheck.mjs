// checks/textcheck.mjs — 字叠字、半截字。node checks/textcheck.mjs [间隔秒，默认 0.2]
// 原理：txt() 画字时记录它在屏幕上的位置；逐帧检查两段字是否重叠、字是否被画面边缘切掉一半。
import { Canvas } from 'skia-canvas';
import { W, H } from '../engine/lib.mjs';
const { drawFrame, DURATION } = await import(new URL('../film.mjs', import.meta.url));
const step = +(process.argv[2] || 0.2);
const c = new Canvas(W, H), x = c.getContext('2d');
const overlaps = [], clipped = [];
for (let t = 0; t < DURATION; t += step) {
  globalThis.__TEXTLOG = [];
  x.setTransform(1, 0, 0, 1, 0, 0); x.clearRect(0, 0, W, H); drawFrame(x, t);
  const L = globalThis.__TEXTLOG.filter(b => b.s.trim() && b.x1 - b.x0 > 4);
  for (let i = 0; i < L.length; i++) {
    const a = L[i];
    const out = Math.max(0, -a.x0) + Math.max(0, a.x1 - W), wid = a.x1 - a.x0;
    const outY = Math.max(0, -a.y0) + Math.max(0, a.y1 - H), hei = a.y1 - a.y0;
    if ((out > 0.1 * wid && out < 0.9 * wid) || (outY > 0.2 * hei && outY < 0.9 * hei)) clipped.push([+t.toFixed(2), a.s.slice(0, 12)]);
    for (let j = i + 1; j < L.length; j++) {
      const b = L[j]; if (a.s === b.s) continue;
      const ix = Math.max(0, Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0)), iy = Math.max(0, Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0));
      const small = Math.min((a.x1 - a.x0) * (a.y1 - a.y0), (b.x1 - b.x0) * (b.y1 - b.y0));
      if (ix * iy > 0.25 * small) overlaps.push([+t.toFixed(2), a.s.slice(0, 10), b.s.slice(0, 10)]);
    }
  }
}
globalThis.__TEXTLOG = null;
const uniq = a => [...new Map(a.map(v => [v.slice(1).join('|'), v])).values()];
const o = uniq(overlaps), k = uniq(clipped);
console.log(`${o.length ? 'FAIL' : 'PASS'}  字叠字：${o.length} 处 ${JSON.stringify(o.slice(0, 6))}`);
console.log(`${k.length ? 'WARN' : 'PASS'}  半截字（被画面边缘切掉一部分）：${k.length} 处 ${JSON.stringify(k.slice(0, 6))}（故意出画的大字可以忽略）`);
