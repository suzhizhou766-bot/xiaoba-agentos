// 缩略图拼板：自己看画面的主要方式（不用每次渲染整片）。
// 用法：node engine/sheet.mjs out/sheet.png 0 0.5 1.2 3      （指定时刻）
//       node engine/sheet.mjs out/sheet.png --every 1        （全片每 1 秒一张）
//       node engine/sheet.mjs out/sheet.png --range 4 6 0.1  （4–6 秒每 0.1 秒一张，看动作细节）
import { Canvas } from 'skia-canvas';
import { W, H, SAFE, LANDSCAPE } from './lib.mjs';
const { drawFrame, DURATION } = await import(new URL('../film.mjs', import.meta.url));
const [out, ...rest] = process.argv.slice(2);
let times;
if (rest[0] === '--every') { const s = +rest[1]; times = []; for (let t = 0; t < DURATION; t += s) times.push(+t.toFixed(3)); }
else if (rest[0] === '--range') { const [a, b, s] = rest.slice(1).map(Number); times = []; for (let t = a; t <= b + 1e-9; t += s) times.push(+t.toFixed(3)); }
else times = rest.map(Number);
const cols = Math.min(LANDSCAPE ? 4 : 6, times.length), cw = W / 4, ch = H / 4, rows = Math.ceil(times.length / cols);
const sheet = new Canvas(cols * cw, rows * (ch + 30)), sx = sheet.getContext('2d');
sx.fillStyle = '#333'; sx.fillRect(0, 0, sheet.width, sheet.height);
const fr = new Canvas(W, H), fx = fr.getContext('2d');
for (let i = 0; i < times.length; i++) {
  fx.setTransform(1, 0, 0, 1, 0, 0); fx.clearRect(0, 0, W, H);
  drawFrame(fx, times[i]);
  const x = (i % cols) * cw, y = Math.floor(i / cols) * (ch + 30);
  sx.drawImage(fr, x, y + 30, cw, ch);
  // 安全区参考线（只画在拼板上，不进成片）
  sx.strokeStyle = 'rgba(255,80,80,0.6)'; sx.lineWidth = 1;
  sx.strokeRect(x + SAFE.l / 4, y + 30 + SAFE.t / 4, (SAFE.r - SAFE.l) / 4, (SAFE.b - SAFE.t) / 4);
  sx.fillStyle = '#fff'; sx.font = '600 20px Menlo'; sx.fillText(`t=${times[i]}`, x + 8, y + 22);
}
await sheet.toFile(out);
console.log('wrote', out, times.length, 'frames');
