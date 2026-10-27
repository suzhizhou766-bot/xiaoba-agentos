#!/bin/sh
':' //; exec "$(cd "$(dirname "$0")/../../.runtime/node/bin" && pwd)/node" "$0" "$@"
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

// Remotion is intentionally installed once under the shared runtime. Resolve
// packages from that runtime so this entry point never falls back to a project
// local node_modules directory.
const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const runtimeRemotionRoot = path.resolve(
  process.env.XIAOBA_REMOTION_ROOT || path.join(scriptDir, "../../.runtime/remotion"),
);
const requireFromRuntime = createRequire(path.join(runtimeRemotionRoot, "package.json"));
const { bundle } = requireFromRuntime("@remotion/bundler");
const { renderMedia, selectComposition } = requireFromRuntime("@remotion/renderer");

const BROWSER = process.env.REMOTION_BROWSER_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

async function main() {
  const args = process.argv.slice(2);
  const entryFile = args[0];
  const compositionId = args[1];
  const outputFile = args[2];
  const concurrencyArg = args.find(a => a.startsWith("--concurrency="));
  const concurrency = concurrencyArg ? parseInt(concurrencyArg.split("=")[1]) : Math.max(1, 3);

  if (!entryFile || !compositionId || !outputFile) {
    console.log("用法: node scripts/remotion_render.mjs <entry.tsx> <composition> <output.mp4> [--concurrency=N]");
    process.exit(1);
  }

  const entryPath = path.resolve(entryFile);
  const outputPath = path.resolve(outputFile);
  const runtimeWork = path.resolve(runtimeRemotionRoot, "../work");
  if (!outputPath.startsWith(runtimeWork + path.sep)) {
    throw new Error(`Remotion 直接渲染只能写入统一运行时 work 目录：${runtimeWork}`);
  }

  console.log(`🎬 Remotion | 合成:${compositionId} | 并发:${concurrency}`);

  // Bundle
  process.stdout.write("📦 ");
  const bundleLocation = await bundle({ entryPoint: entryPath });
  console.log(`ok (${bundleLocation})`);

  // Select composition (with browser path + download prevention)
  process.stdout.write("🎞️  ");
  const composition = await selectComposition({
    serveUrl: bundleLocation,
    id: compositionId,
    browserExecutable: BROWSER,
    onBrowserDownload: () => { throw new Error("跳过Chrome下载"); },
  });
  console.log(`${composition.durationInFrames}frames @ ${composition.fps}fps`);

  // Render
  process.stdout.write("🎥 ");
  const start = Date.now();
  await renderMedia({
    composition,
    serveUrl: bundleLocation,
    codec: "h264",
    outputLocation: outputPath,
    browserExecutable: BROWSER,
    concurrency,
  });
  console.log(`done (${((Date.now() - start) / 1000).toFixed(1)}s)`);
  console.log(`📁 ${outputPath}`);
}

main().catch(err => { console.error("❌", err.message); process.exit(1); });
