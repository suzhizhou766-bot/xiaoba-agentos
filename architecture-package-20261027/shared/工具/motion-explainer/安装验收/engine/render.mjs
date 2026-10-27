// node engine/render.mjs <开始帧> <结束帧> <输出.mp4>   （一般不直接用，run.sh 会并行调用）
import { Canvas } from 'skia-canvas';
import { spawn } from 'node:child_process';
import { W, H, FPS } from './lib.mjs';
const { drawFrame } = await import(new URL('../film.mjs', import.meta.url));
const [a, b, out] = process.argv.slice(2); const A = +a, B = +b;
const ff = spawn('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgba', '-s', `${W}x${H}`, '-r', String(FPS), '-i', '-',
  '-c:v', 'libx264', '-preset', 'medium', '-crf', '16', '-pix_fmt', 'yuv420p', '-threads', '2', out], { stdio: ['pipe', 'inherit', 'inherit'] });
const c = new Canvas(W, H), x = c.getContext('2d');
const t0 = Date.now();
for (let f = A; f < B; f++) {
  x.setTransform(1, 0, 0, 1, 0, 0); x.clearRect(0, 0, W, H); // 每帧整块清空，防止越渲越慢
  drawFrame(x, f / FPS);
  const buf = await c.toBuffer('raw');
  if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
  if ((f - A) % 60 === 0) process.stderr.write(`[${A}-${B}] ${f - A}/${B - A} ${((Date.now() - t0) / 1000).toFixed(0)}s\n`);
}
ff.stdin.end();
await new Promise(r => ff.on('close', r));
console.log('done', out, ((Date.now() - t0) / 1000).toFixed(1) + 's');
