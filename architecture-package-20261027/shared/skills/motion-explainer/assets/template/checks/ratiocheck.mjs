// checks/ratiocheck.mjs — 画面比例要和台词里的数字对得上。
// film.mjs 里 export QUANTITIES = [{ label: '2000 ml vs 500 ml', t: 4.0, id: 'ml2000', ref: 'ml500', ratio: 4, axis: 'h' }]
// axis: 'h' 高度比、'w' 宽度比、'area' 面积比。偏差超过 25% 判 FAIL。需要 film.mjs 同时 export WORLD。
const m = await import(new URL('../film.mjs', import.meta.url));
if (!m.QUANTITIES || !m.WORLD) { console.log('INFO  比例检查：film.mjs 没有 export QUANTITIES 和 WORLD，跳过（台词里有数字对比时必须写）'); process.exit(0); }
let bad = 0;
for (const q of m.QUANTITIES) {
  const a = m.WORLD.rectAt(q.id, q.t), b = m.WORLD.rectAt(q.ref, q.t);
  const got = q.axis === 'w' ? a.w / b.w : q.axis === 'area' ? (a.w * a.h) / (b.w * b.h) : a.h / b.h;
  const dev = Math.abs(got / q.ratio - 1); const ok = dev <= 0.25; bad += !ok;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${q.label}：画面 ${got.toFixed(2)} 倍 / 台词 ${q.ratio} 倍（偏差 ${(dev * 100).toFixed(0)}%）`);
}
if (!m.QUANTITIES.length) console.log('INFO  QUANTITIES 是空的');
