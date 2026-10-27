import { Muxer, ArrayBufferTarget } from "mp4-muxer";
import { viewportBridge } from "./viewportBridge";
import type { Shot } from "./core";

export type VideoExportOptions = {
  fps?: number;
  width?: number;
  height?: number;
  bitrate?: number;
  signal?: AbortSignal;
  onProgress?: (progress: number, status: string) => void;
};

export type VideoExportResult = {
  bytes: Uint8Array;
  mimeType: string;
  extension: "mp4" | "webm";
  width: number;
  height: number;
  codec: string;
  mode: "webcodecs" | "mediarecorder";
};

/**
 * 逐帧渲染当前镜头的画面并编码为标准视频（优先 WebCodecs H.264 MP4，降级 MediaRecorder）
 */
export async function renderShotVideo(
  shot: Shot,
  options: VideoExportOptions = {},
): Promise<VideoExportResult> {
  const fps = options.fps ?? 24;
  const duration = Math.max(0.1, shot.out - shot.in);
  const totalFrames = Math.max(1, Math.round(duration * fps));
  const onProgress = options.onProgress;
  const signal = options.signal;

  if (signal?.aborted) {
    throw new Error("导出操作已取消");
  }

  if (!viewportBridge.renderFrame || !viewportBridge.getCanvas) {
    throw new Error("三维监看视口尚未就绪，请展开监看面板后重试");
  }

  const width = options.width ?? 1280;
  const height = options.height ?? 720;
  const bitrate = options.bitrate ?? 4_500_000;

  // 1. 设置独立目标渲染尺寸并在结束时精准恢复监看画布
  const restoreCanvas = viewportBridge.prepareExport
    ? viewportBridge.prepareExport(width, height)
    : null;

  try {
    const canvas = viewportBridge.getCanvas();
    if (!canvas) {
      throw new Error("未能获取监看画布 Canvas");
    }

    // 2. 尝试使用 WebCodecs VideoEncoder + mp4-muxer (确定性逐帧离线渲染与编码)
    const hasWebCodecs =
      typeof VideoEncoder !== "undefined" && typeof VideoFrame !== "undefined";

    if (hasWebCodecs) {
      const codecCandidates = [
        "avc1.42001f",
        "avc1.4d001f",
        "avc1.42E01E",
        "avc1.640028",
      ];
      let selectedCodec: string | null = null;

      for (const cand of codecCandidates) {
        try {
          const support = await VideoEncoder.isConfigSupported({
            codec: cand,
            width,
            height,
            bitrate,
            framerate: fps,
          });
          if (support.supported) {
            selectedCodec = cand;
            break;
          }
        } catch {
          // ignore candidate check error
        }
      }

      if (selectedCodec) {
        const target = new ArrayBufferTarget();
        const muxer = new Muxer({
          target,
          video: {
            codec: "avc",
            width,
            height,
          },
          fastStart: "in-memory",
        });

        let encoderError: Error | null = null;
        const videoEncoder = new VideoEncoder({
          output: (chunk, meta) => muxer.addVideoChunk(chunk, meta),
          error: (e) => {
            encoderError = e instanceof Error ? e : new Error(String(e));
          },
        });

        try {
          videoEncoder.configure({
            codec: selectedCodec,
            width,
            height,
            bitrate,
            framerate: fps,
          });

          onProgress?.(0.02, "正在准备逐帧编码 (WebCodecs)...");

          for (let i = 0; i < totalFrames; i++) {
            if (signal?.aborted) throw new Error("导出已由用户取消");
            if (encoderError) throw encoderError;

            const t = shot.in + i / fps;
            viewportBridge.renderFrame(shot, t);

            const timestampUs = Math.round((i * 1_000_000) / fps);
            const durationUs = Math.round(1_000_000 / fps);

            const frame = new VideoFrame(canvas, {
              timestamp: timestampUs,
              duration: durationUs,
            });

            const isKeyFrame = i % (fps * 2) === 0;
            videoEncoder.encode(frame, { keyFrame: isKeyFrame });
            frame.close();

            // 队列背压控制：若编码器积压超过 5 帧，等待队列释放
            if (videoEncoder.encodeQueueSize > 5) {
              await new Promise<void>((resolve) => {
                const onDequeue = () => {
                  videoEncoder.removeEventListener("dequeue", onDequeue);
                  resolve();
                };
                videoEncoder.addEventListener("dequeue", onDequeue, { once: true });
                setTimeout(resolve, 50);
              });
            }

            const p = (i + 1) / totalFrames;
            onProgress?.(
              Math.min(0.95, 0.05 + p * 0.9),
              `正在渲染并录制机位运动 (${i + 1}/${totalFrames} 帧)...`,
            );

            if (i % 8 === 0) {
              await new Promise((r) => setTimeout(r, 0));
            }
          }

          onProgress?.(0.96, "正在封装标准 MP4 容器...");
          await videoEncoder.flush();
          muxer.finalize();

          onProgress?.(1.0, "机位视频 (MP4) 制作完成！");
          return {
            bytes: new Uint8Array(target.buffer),
            mimeType: "video/mp4",
            extension: "mp4",
            width,
            height,
            codec: selectedCodec,
            mode: "webcodecs",
          };
        } catch (err) {
          if (signal?.aborted) throw err;
          console.warn("WebCodecs 编码异常，尝试平滑降级至 MediaRecorder 录制:", err);
          onProgress?.(0.05, "硬件编码受阻，正在自动转入兼容录制模式...");
        } finally {
          if (videoEncoder.state !== "closed") {
            try {
              videoEncoder.close();
            } catch {
              // ignore
            }
          }
        }
      }
    }

    // 3. 降级方案：使用原生 MediaRecorder 录制
    onProgress?.(0.05, "正在使用原生媒体流录制机位视频 (降级模式)...");
    let stream: MediaStream | null = null;
    let recorder: MediaRecorder | null = null;
    try {
      stream = canvas.captureStream(fps);
      const mimeTypes = [
        "video/mp4;codecs=avc1",
        "video/mp4",
        "video/webm;codecs=h264",
        "video/webm;codecs=vp9",
        "video/webm",
      ];

      let mimeType = "video/webm";
      if (typeof MediaRecorder !== "undefined") {
        for (const m of mimeTypes) {
          if (MediaRecorder.isTypeSupported(m)) {
            mimeType = m;
            break;
          }
        }
      }

      const isMp4 = mimeType.startsWith("video/mp4");
      const extension: "mp4" | "webm" = isMp4 ? "mp4" : "webm";

      recorder = new MediaRecorder(stream, {
        mimeType,
        videoBitsPerSecond: bitrate,
      });

      const chunks: Blob[] = [];
      let recorderError: Error | null = null;
      recorder.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) chunks.push(e.data);
      };
      recorder.onerror = (e: any) => {
        recorderError = e?.error || new Error("MediaRecorder 录制发生异常");
      };

      const frameMs = 1000 / fps;
      recorder.start(100);

      for (let i = 0; i < totalFrames; i++) {
        if (signal?.aborted) {
          throw new Error("导出已由用户取消");
        }
        if (recorderError) {
          throw recorderError;
        }

        const t = shot.in + i / fps;
        viewportBridge.renderFrame(shot, t);
        onProgress?.(
          (i + 1) / totalFrames,
          `录制中 (${i + 1}/${totalFrames} 帧)...`,
        );
        await new Promise((r) => setTimeout(r, frameMs));
      }

      if (recorder.state !== "inactive") {
        recorder.stop();
        await new Promise<void>((resolve, reject) => {
          if (!recorder) return resolve();
          recorder.onstop = () => resolve();
          recorder.onerror = (e: any) => reject(e?.error || new Error("MediaRecorder 停止失败"));
        });
      }

      if (recorderError) throw recorderError;

      const blob = new Blob(chunks, { type: mimeType });
      const buffer = await blob.arrayBuffer();

      return {
        bytes: new Uint8Array(buffer),
        mimeType,
        extension,
        width,
        height,
        codec: mimeType,
        mode: "mediarecorder",
      };
    } finally {
      try {
        if (recorder && recorder.state !== "inactive") recorder.stop();
      } catch {}
      try {
        if (stream) stream.getTracks().forEach((t) => t.stop());
      } catch {}
    }
  } finally {
    restoreCanvas?.();
  }
}

/**
 * 兼容旧调用的别名
 */
export async function renderShotToMp4(
  shot: Shot,
  options: VideoExportOptions = {},
): Promise<VideoExportResult> {
  return renderShotVideo(shot, options);
}

/**
 * 触发浏览器本地下载视频文件
 */
export function downloadVideo(
  filename: string,
  data: Uint8Array | Blob,
  mimeType = "video/mp4",
) {
  const blob =
    data instanceof Blob
      ? data
      : new Blob([data as unknown as BlobPart], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}
