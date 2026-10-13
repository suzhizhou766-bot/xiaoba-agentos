import { exportShot, exportTimes } from "./exportPlan";
import { CompilerBridge } from "./panels/CompilerBridge";
import { confirmDraftDeparture } from "./draftGuard";
import { readSceneImages, prepareImage, sceneImageFiles } from "./sceneImages";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Download, X } from "lucide-react";
import type { VideoExportResult } from "./videoExport";
import {
  getTabDraftId,
  saveDraft,
  loadDraft,
  loadDraftResult,
  setTabDraftId,
  createNewTabDraftId,
  type SaveStatus,
} from "./storage";
import { zipSync, strToU8 } from "fflate";
import { modelProfiles, modelGuide } from "./modelProfiles";
import { videoPrompt, handoffTextFiles, cleanProductionPrompt } from "./handoff";
import { viewportBridge } from "./viewportBridge";
import { useStore, active } from "./store";
import {
  sampledShot,
  compile,
  copy,
  parseProject,
  uuid,
  initial,
} from "./core";
import { decode, importModels } from "./assets";
import { PropsLibraryModal } from "./PropsLibraryModal";
import { DraftsModal } from "./DraftsModal";
import { download } from "./ui";
import { HeaderBar } from "./panels/HeaderBar";
import { HierarchyPanel } from "./panels/HierarchyPanel";
import { ViewportPanel } from "./panels/ViewportPanel";
import { InspectorPanel } from "./panels/InspectorPanel";
import { TimelinePanel } from "./panels/TimelinePanel";
export default function App() {
  const s = useStore(),
    p = s.project,
    shot = active(p),
    selected = shot.objects.find((o) => o.id === s.selected),
    cam = shot.camera;
  const [tab, setTab] = useState<"camera" | "object" | "lighting">("camera"),
    [compiler, setCompiler] = useState(false),
    [guide, setGuide] = useState(true),
    [propsLibraryOpen, setPropsLibraryOpen] = useState(false),
    [draftsOpen, setDraftsOpen] = useState(false),
    [isRestoring, setIsRestoring] = useState(false),
    [busy, setBusy] = useState(false),
    [saveStatus, setSaveStatus] = useState<SaveStatus>("saved"),
    [lastSavedAt, setLastSavedAt] = useState<number | null>(null),
    [mobilePane, setMobilePane] = useState<"stage" | "monitor" | "hierarchy">("stage");
  const files = useRef<HTMLInputElement>(null),
    projectFile = useRef<HTMLInputElement>(null),
    abortControllerRef = useRef<AbortController | null>(null),
    savingRevisionRef = useRef<number>(0),
    autoSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const compiled = useMemo(
    () => (compiler ? compile(p, shot) : { text: "", json: null }),
    [compiler, p, shot],
  );
  const profile = shot.videoModel ? modelProfiles[shot.videoModel] : null;

  const flushCurrentDraft = async (): Promise<boolean> => {
    if (autoSaveTimerRef.current) {
      clearTimeout(autoSaveTimerRef.current);
      autoSaveTimerRef.current = null;
    }
    const currentProject = useStore.getState().project;
    const currentRev = useStore.getState().revision;
    const draftId = getTabDraftId();
    setSaveStatus("saving");
    savingRevisionRef.current = currentRev;
    const res = await saveDraft(draftId, currentProject);
    // A completed write only confirms the captured draft revision.
    if (getTabDraftId() !== draftId || useStore.getState().revision !== currentRev) {
      return false;
    }
    if (res.success) {
      if (useStore.getState().revision === currentRev) {
        setSaveStatus("saved");
        setLastSavedAt(res.updatedAt);
      }
      return true;
    } else {
      setSaveStatus("error");
      useStore.getState().set({
        notice: "草稿保存失败，请检查存储配额：" + (res.error || "未知错误"),
      });
      return false;
    }
  };

  const restoreDraftById = async (draftId: string) => {
    if (!(await confirmDraftDeparture(saveStatus, flushCurrentDraft, () => window.confirm(
      "当前修改尚未完整保存。继续切换可能丢失修改，是否放弃未保存修改并切换？",
    )))) return;
    setIsRestoring(true);
    try {
      const res = await loadDraftResult(draftId);
      if (!res.project) {
        s.set({
          notice: res.error
            ? `读取草稿失败：${res.error}`
            : "未能读取到该草稿数据或记录为空",
        });
        return;
      }
      const draft = res.project;
      for (const a of draft.assets) await decode(a);
      await Promise.all((draft.sceneImages ?? []).map(prepareImage));
      setTabDraftId(draftId);
      useStore.getState().resetSession(
        draft,
        `已恢复本机草稿「${draft.name || "未命名项目"}」`,
      );
      setSaveStatus("saved");
      setLastSavedAt(Date.now());
    } catch (err) {
      console.warn("恢复草稿失败:", err);
      s.set({ notice: "恢复草稿失败：" + ((err as Error).message || "未知错误") });
    } finally {
      setIsRestoring(false);
    }
  };

  const handleNewDraft = async () => {
    if (!(await confirmDraftDeparture(saveStatus, flushCurrentDraft, () => window.confirm(
      "当前修改尚未完整保存。继续新建可能丢失修改，是否放弃未保存修改并新建？",
    )))) return;
    const newId = createNewTabDraftId();
    const fresh = initial();
    useStore.getState().resetSession(fresh, "已新建空白草稿");
    setSaveStatus("saving");
    const res = await saveDraft(newId, fresh);
    if (res.success) {
      setSaveStatus("saved");
      setLastSavedAt(res.updatedAt);
    } else {
      setSaveStatus("error");
      useStore.getState().set({
        notice: "新建草稿落盘失败：" + (res.error || "未知存储错误"),
      });
    }
  };

  // Auto-restore draft on mount
  useEffect(() => {
    let activeEffect = true;
    const draftId = getTabDraftId();
    setIsRestoring(true);
    loadDraftResult(draftId).then(async (res) => {
      if (!activeEffect || !res.project) {
        if (activeEffect) setIsRestoring(false);
        return;
      }
      try {
        const draft = res.project;
        for (const a of draft.assets) await decode(a);
        await Promise.all((draft.sceneImages ?? []).map(prepareImage));
        if (!activeEffect) return;
        useStore.getState().resetSession(
          draft,
          `已自动恢复本机草稿「${draft.name || "未命名项目"}」`,
        );
        setSaveStatus("saved");
        setLastSavedAt(Date.now());
      } catch (err) {
        console.warn("恢复本地草稿失败:", err);
      } finally {
        if (activeEffect) setIsRestoring(false);
      }
    });
    return () => {
      activeEffect = false;
    };
  }, []);

  // Auto-save on revision change with race protection
  const isInitialMount = useRef(true);
  useEffect(() => {
    if (isInitialMount.current) {
      isInitialMount.current = false;
      return;
    }
    if (isRestoring) return;
    setSaveStatus("unsaved");
    const currentRev = s.revision;
    if (autoSaveTimerRef.current) {
      clearTimeout(autoSaveTimerRef.current);
    }
    autoSaveTimerRef.current = setTimeout(async () => {
      if (isRestoring) return;
      setSaveStatus("saving");
      savingRevisionRef.current = currentRev;
      const currentProject = useStore.getState().project;
      const res = await saveDraft(getTabDraftId(), currentProject);
      if (useStore.getState().revision === currentRev) {
        if (res.success) {
          setSaveStatus("saved");
          setLastSavedAt(res.updatedAt);
        } else {
          setSaveStatus("error");
          useStore.getState().set({
            notice: "自动保存失败：" + (res.error || "存储异常"),
          });
        }
      }
    }, 500);
    return () => {
      if (autoSaveTimerRef.current) {
        clearTimeout(autoSaveTimerRef.current);
        autoSaveTimerRef.current = null;
      }
    };
  }, [s.revision, isRestoring]);

  // Warn before unload if unsaved or saving
  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      if (saveStatus === "unsaved" || saveStatus === "saving" || saveStatus === "error") {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [saveStatus]);

  useEffect(() => {
    setTab(s.selected.startsWith("camera") ? "camera" : "object");
  }, [s.selected]);
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (
        (e.target as HTMLElement).closest(
          "input,textarea,select,[contenteditable]",
        )
      )
        return;

      const mod = e.metaKey || e.ctrlKey;
      if (mod) {
        if (e.key === "z" || e.key === "Z") {
          e.preventDefault();
          if (e.shiftKey) useStore.getState().redo();
          else useStore.getState().undo();
          return;
        }
        if ((e.key === "y" || e.key === "Y") && !e.shiftKey) {
          e.preventDefault();
          useStore.getState().redo();
          return;
        }
        return;
      }
      if (e.altKey) return;
      const mode = { w: "translate", e: "rotate", r: "scale" }[
        e.key.toLowerCase()
      ] as typeof s.mode | undefined;
      if (mode) {
        const selected = useStore.getState().selected;
        if (selected.startsWith("camera") && mode === "scale")
          s.set({ notice: "摄影机不做物体缩放，请调整焦距或移动机位" });
        else if (selected === "camera-target" && mode === "rotate")
          s.set({ notice: "视线目标使用位移手柄调整" });
        else s.set({ mode });
      }
      if (e.code === "Space") {
        e.preventDefault();
        useStore.getState().togglePlay();
      }
      if (e.key === "Delete" || e.key === "Backspace") s.remove();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);
  useEffect(() => {
    if (!s.playing) return;
    let id: number;
    let previous = performance.now(),
      elapsed = s.time;
    const tick = (now: number) => {
      elapsed += (now - previous) / 1000;
      previous = now;
      if (elapsed >= shot.out) {
        s.seek(shot.out);
        s.set({ playing: false });
        return;
      }
      s.seek(elapsed);
      id = requestAnimationFrame(tick);
    };
    id = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(id);
  }, [s.playing, shot.id, shot.out]);
  async function importing(list: File[]) {
    setBusy(true);
    try {
      if (!list.length) return;
      if (!list.some(file => /\.(glb|gltf)$/i.test(file.name)) && list.some(file => /\.(png|jpe?g)$/i.test(file.name))) {
        const images = await readSceneImages(list);
        const state=useStore.getState(), current=active(state.project);
        state.set({project:{...state.project, sceneImages:[...(state.project.sceneImages??[]),...images],shots:state.project.shots.map(sh=>sh.id===current.id?{...sh,sceneImageIds:[...(sh.sceneImageIds??[]),...images.map(image=>image.id)]}:sh)},notice:`已添加 ${images.length} 张场景图片；可在左侧选择用途`});
        return;
      }
      const result = await importModels(list);
      const state = useStore.getState(),
        project = copy(state.project);
      project.assets.push(...result.assets);
      active(project).objects.push(...result.objects);
      s.set({
        project,
        selected: result.objects[0].id,
        notice: `已导入 ${result.objects.length} 个模型`,
      });
    } catch (e) {
      s.set({ notice: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }
  async function load(file?: File) {
    if (!file) return;
    if (!(await confirmDraftDeparture(saveStatus, flushCurrentDraft, () => window.confirm(
      "当前修改尚未完整保存。继续导入可能丢失修改，是否放弃未保存修改并导入？",
    )))) return;
    setBusy(true);
    try {
      const project = parseProject(JSON.parse(await file.text()));
      for (const a of project.assets) await decode(a);
      await Promise.all((project.sceneImages ?? []).map(prepareImage));
      useStore.getState().resetSession(
        project,
        `项目「${project.name}」已加载，素材已恢复`,
      );
      setSaveStatus("saving");
      const res = await saveDraft(getTabDraftId(), project);
      if (res.success) {
        setSaveStatus("saved");
        setLastSavedAt(res.updatedAt);
      } else {
        setSaveStatus("error");
      }
    } catch (e) {
      s.set({ notice: "加载失败，已保留当前项目：" + (e as Error).message });
    } finally {
      setBusy(false);
    }
  }
  function screenshot() {
    try {
      if (!viewportBridge.capture) throw Error("监看尚未就绪");
      download(`${shot.name}_${Math.round(s.time * 24)}f.png`, viewportBridge.capture());
      s.set({ notice: "已导出主摄影机画面（不包含辅助线）" });
    } catch (e) {
      s.set({ notice: (e as Error).message });
    }
  }
  async function exportVideo() {
    const time = s.time;
    setBusy(true);
    s.set({ playing: false });
    abortControllerRef.current = new AbortController();
    try {
      const { renderShotVideo, downloadVideo } = await import("./videoExport");
      s.set({ notice: "正在逐帧渲染并编码机位运动视频..." });
      const video = await renderShotVideo(exportShot(shot), {
        width: 1280,
        height: 720,
        fps: 24,
        signal: abortControllerRef.current.signal,
        onProgress: (p, msg) => {
          s.set({ notice: `${msg} (${Math.round(p * 100)}%)` });
        },
      });
      const filename = `${shot.name}_camera-motion.${video.extension}`;
      downloadVideo(filename, video.bytes, video.mimeType);
      s.set({
        notice: `机位运动视频 (${filename}) 导出成功！[${video.mode === "webcodecs" ? "离线确定性" : "实时录制"}]`,
      });
    } catch (e) {
      s.set({ notice: "机位视频导出失败：" + (e as Error).message });
    } finally {
      s.seek(time);
      setBusy(false);
      abortControllerRef.current = null;
    }
  }
  async function edgeCapture() {
    const time = s.time;
    setBusy(true);
    s.set({ playing: false });
    try {
      for (const [label, t] of [["首帧", exportShot(shot).in], ["尾帧", exportShot(shot).out]] as const) {
        s.seek(t);
        await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
        if (!viewportBridge.capture) throw Error("主摄影机尚未就绪");
        download(`${shot.name}_${label}.png`, viewportBridge.capture(sampledShot(exportShot(shot),t)));
      }
      s.set({ notice: "已导出 1920×1080 构图参考首尾帧；若仅收到一张，可使用交接包 ZIP" });
    } catch (e) {
      s.set({ notice: "首尾帧导出失败：" + (e as Error).message });
    } finally {
      s.seek(time);
      setBusy(false);
    }
  }
  async function exportHandoff() {
    const state = useStore.getState();
    const project = copy(state.project),
      current = exportShot(active(project)),
      time = state.time;
    if (!current.videoModel) {
      state.set({ notice: "请先选择视频模型" });
      return;
    }
    setBusy(true);
    state.set({ playing: false });
    abortControllerRef.current = new AbortController();
    try {
      const intermediate = exportTimes(current);
      const intermediateFiles = intermediate.map((time, i) => ({
        file: `motion-${i + 1}.png`,
        time,
      }));

      // 先渲染视频，使得导出的提示词与 manifest 中的文件名保持一致
      let videoResult: VideoExportResult | null = null;
      let videoError: string | null = null;
      try {
        const { renderShotVideo } = await import("./videoExport");
        state.set({ notice: "正在逐帧生成机位运动视频..." });
        videoResult = await renderShotVideo(current, {
          width: 1280,
          height: 720,
          fps: 24,
          signal: abortControllerRef.current.signal,
          onProgress: (p, msg) => {
            state.set({ notice: `打包中：${msg}` });
          },
        });
      } catch (videoErr) {
        if (abortControllerRef.current?.signal.aborted) {
          throw videoErr;
        }
        videoError = (videoErr as Error).message || "未能生成机位视频";
        console.warn("未能生成机位视频，继续打包其余文件:", videoErr);
      }

      const videoFilename = videoResult
        ? `camera-motion.${videoResult.extension}`
        : null;

      const entries: Record<string, Uint8Array> = Object.fromEntries(
        Object.entries(handoffTextFiles(project, current, videoFilename)).map(
          ([name, text]) => [name, strToU8(text)],
        ),
      );

      entries["intermediate-frames.json"] = strToU8(
        JSON.stringify(intermediateFiles, null, 2),
      );

      for (const [name, t] of [
        ["first-frame-blocking.png", current.in],
        ["last-frame-blocking.png", current.out],
        ...intermediate.map((t, i) => [`motion-${i + 1}.png`, t] as const),
      ] as readonly (readonly [string, number])[]) {
        if (abortControllerRef.current?.signal.aborted) {
          throw new Error("导出已由用户取消");
        }
        if (!viewportBridge.capture) throw Error("主摄影机尚未就绪，请等待视口加载后重试");
        const uri = viewportBridge.capture(sampledShot(current, t));
        if (!uri.startsWith("data:image/png"))
          throw Error("未能取得构图参考图，请展开浏览器窗口后重试");
        entries[name] = Uint8Array.from(atob(uri.split(",")[1]), (c) =>
          c.charCodeAt(0),
        );
      }

      if (videoResult && videoFilename) {
        entries[videoFilename] = videoResult.bytes;
      }

      Object.assign(
        entries,
        sceneImageFiles(
          (project.sceneImages ?? []).filter((image) =>
            current.sceneImageIds?.includes(image.id),
          ),
        ),
      );

      // Export manifest
      const manifest = {
        packageVersion: "0.8.0",
        exportedAt: new Date().toISOString(),
        projectName: project.name,
        shot: {
          id: current.id,
          name: current.name,
          duration: current.duration,
          in: current.in,
          out: current.out,
        },
        videoModel: current.videoModel,
        status: videoResult ? "complete" : "partial",
        files: Object.keys(entries),
        video: videoResult
          ? {
              status: "success",
              file: videoFilename,
              mode: videoResult.mode,
              codec: videoResult.codec,
              width: videoResult.width,
              height: videoResult.height,
            }
          : {
              status: "missing",
              error: videoError,
            },
      };
      entries["export-manifest.json"] = strToU8(JSON.stringify(manifest, null, 2));

      const bytes = zipSync(entries);
      const url = URL.createObjectURL(
        new Blob([new Uint8Array(bytes)], { type: "application/zip" }),
      );
      const link = document.createElement("a");
      link.href = url;
      link.download = `${current.name}.${current.videoModel}.director-handoff.zip`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);

      if (videoResult) {
        state.set({
          notice: `交接包已完整打包（含 ${modelProfiles[current.videoModel].name} 指南与机位视频），直接发给我即可`,
        });
      } else {
        state.set({
          notice: `交接包已部分打包（提示词与图片已就绪，机位视频未生成，详见包内 export-manifest.json）`,
        });
      }
    } catch (e) {
      state.set({ notice: "交接包导出失败：" + (e as Error).message });
    } finally {
      state.seek(time);
      setBusy(false);
      abortControllerRef.current = null;
    }
  }
  return (
    <div
      className="app"
      onDragOver={(e) => {
        e.preventDefault();
      }}
      onDrop={(e) => {
        e.preventDefault();
        if (!busy) void importing(Array.from(e.dataTransfer.files));
      }}
    >
      <HeaderBar
        saveStatus={saveStatus}
        lastSavedAt={lastSavedAt}
        mobilePane={mobilePane}
        setMobilePane={setMobilePane}
        setDraftsOpen={setDraftsOpen}
        setCompiler={setCompiler}
        projectFile={projectFile}
      />
      <div className={`workspace pane-${mobilePane}`}>
        <HierarchyPanel
          importing={importing}
          busy={busy}
          setPropsLibraryOpen={setPropsLibraryOpen}
          files={files}
        />
        <ViewportPanel />
        <InspectorPanel
          tab={tab}
          setTab={setTab}
          guide={guide}
          setGuide={setGuide}
          screenshot={screenshot}
          exportVideo={exportVideo}
          busy={busy}
        />
      </div>
      <TimelinePanel edgeCapture={edgeCapture} />
      <footer>
        <span style={{ display: "inline-flex", alignItems: "center", gap: "8px" }}>
          <span className="dot" />
          {s.notice}
          {busy && abortControllerRef.current && (
            <button
              className="footer-cancel-btn"
              onClick={() => {
                abortControllerRef.current?.abort();
                s.set({ notice: "正在中止当前任务..." });
              }}
              style={{
                background: "#4a1d1d",
                color: "#fc8181",
                border: "1px solid #742a2a",
                borderRadius: "4px",
                padding: "2px 8px",
                fontSize: "11px",
                cursor: "pointer",
              }}
            >
              取消任务
            </button>
          )}
        </span>
        <span>
          {shot.objects.length} OBJECTS <i /> {shot.keys.length} KEYS <i /> WEB
          · LOCAL
        </span>
      </footer>
      <input
        ref={files}
        type="file"
        multiple
        hidden
        onChange={(e) => {
          if (e.target.files) void importing(Array.from(e.target.files));
          e.target.value = "";
        }}
      />
      <input
        ref={projectFile}
        type="file"
        accept=".json"
        hidden
        onChange={(e) => {
          void load(e.target.files?.[0]);
          e.target.value = "";
        }}
      />
      {compiler && (
        <div className="modal-backdrop">
          <div className={`modal ${busy ? "exporting" : ""}`}>
            <div className="modal-header">
              <div>
                <span className="eyebrow">DIRECTOR COMPILER</span>
                <h2>把你的镜头设计，交给我制作。</h2>
                <p>选择视频模型 → 导出交接包 → 助手制作首尾帧与适配提示词</p>
              </div>
              <button title="关闭" onClick={() => setCompiler(false)}>
                <X />
              </button>
            </div>
            <fieldset className="model-picker">
              <legend>导出范围与尺寸</legend>
              {!!compiled.json?.空间导演记录.越界区间.length && <p className="coverage-warning">待完善场景参考：{compiled.json.空间导演记录.越界区间.map(r=>`${r.from.toFixed(2)}–${r.to.toFixed(2)}秒`).join('、')} 存在背景越界。允许导出诊断交接包；生成最终首尾图前需补充参考或调整取景，不能把灰黑边缘当作布景。</p>}
              <div style={{display:'flex',gap:'8px',flexWrap:'wrap',margin:'4px 0'}}>
                <button onClick={()=>download(`${shot.name}.video-prompt.zh.txt`,videoPrompt(p,shot),"text/plain;charset=utf-8")}>完整提示词与审计 TXT</button>
                <button onClick={async () => {
                  if(!shot.videoModel){s.set({notice:"请先勾选下方目标视频模型"});return;}
                  const clean = cleanProductionPrompt(p, shot);
                  try {
                    if (!navigator.clipboard?.writeText) throw new Error("当前环境不支持剪贴板写入");
                    await navigator.clipboard.writeText(clean);
                    s.set({ notice: "已复制模型纯净提示词到剪贴板，可直接粘贴生成" });
                  } catch (err) {
                    s.set({ notice: "复制失败：" + ((err as Error).message || "权限不足") });
                  }
                }}>一键复制纯净提示词</button>
              </div>
              <label>时长范围<select aria-label="导出范围" value={shot.exportRange??'auto'} onChange={e=>s.edit(sh=>{sh.exportRange=e.target.value as 'auto'|'manual';})}><option value="auto">自动截止到最后关键帧</option><option value="manual">使用手动首尾标记（可保留停留）</option></select></label>
              <p>实际导出：{exportShot(shot).in.toFixed(3)}–{exportShot(shot).out.toFixed(3)} 秒，共 {(exportShot(shot).out-exportShot(shot).in).toFixed(3)} 秒</p>
              <label><input type="checkbox" checked={shot.scaleMotion??false} onChange={e=>s.edit(sh=>{sh.scaleMotion=e.target.checked;})} />允许物体自身变大/变小（默认固定首键尺寸，保留透视变化）</label>
            </fieldset>
            <fieldset className="model-picker">
              <legend>本镜头使用的视频模型</legend>
              <div className="model-options">
                {Object.values(modelProfiles).map((m) => (
                  <label
                    key={m.id}
                    className={shot.videoModel === m.id ? "chosen" : ""}
                  >
                    <input
                      type="radio"
                      name="video-model"
                      value={m.id}
                      checked={shot.videoModel === m.id}
                      onChange={() => s.edit((sh) => (sh.videoModel = m.id))}
                    />
                    <span>
                      <b>{m.name}</b>
                      <small>{m.summary}</small>
                    </span>
                  </label>
                ))}
              </div>
              <p className="hint">
                {profile
                  ? `已选 ${profile.name} · 随镜头保存，ZIP / TXT / JSON 均携带此选择`
                  : "导出前勾选一个模型；旧项目不会被擅自指定模型。"}
              </p>
            </fieldset>
            {profile && (
              <details className="model-guide">
                <summary>
                  查看提示词教学与使用方法 · 核验 {profile.checkedAt}
                </summary>
                <div className="guide-content">
                  <h3>{profile.name}</h3>
                  <p>{profile.language}</p>
                  <h4>官方资料核验</h4>
                  {profile.officialNotes.map((n) => (
                    <p key={n}>{n}</p>
                  ))}
                  <h4>操作步骤</h4>
                  <ol>
                    {profile.workflow.map((n) => (
                      <li key={n}>{n}</li>
                    ))}
                  </ol>
                  {profile.sections.map((section) => (
                    <section key={section.title}>
                      <h4>{section.title}</h4>
                      <p>{section.text}</p>
                    </section>
                  ))}
                  <h4>原创模板</h4>
                  <pre>{profile.template}</pre>
                  <h4>资料来源</h4>
                  {profile.sources.map((source) => (
                    <p key={source.url}>
                      <a href={source.url} target="_blank" rel="noreferrer">
                        {source.title}
                      </a>{" "}
                      · {source.scope}
                    </p>
                  ))}
                  <button
                    onClick={() =>
                      download(
                        `${profile.id}.guide.md`,
                        modelGuide(profile),
                        "text/markdown",
                      )
                    }
                  >
                    下载完整指南
                  </button>
                </div>
              </details>
            )}
            <CompilerBridge project={p} shot={shot} notify={(notice) => s.set({ notice })} />
            <label className="handoff-brief">
              视觉说明与参考素材备注（随镜头保存）
              <textarea
                aria-label="视觉说明"
                placeholder="例如：角色身份与服装、场景外观、写实或插画风格、光照；参考图片可在左侧添加，随交接包导出。"
                value={shot.visualBrief ?? ""}
                onChange={(e) =>
                  s.edit((sh) => (sh.visualBrief = e.target.value))
                }
              />
            </label>
            <textarea
              aria-label="反推交接说明"
              readOnly
              value={compiled.text}
            />
            <div className="modal-footer">
              <small>
                交接包带上已选模型及对应教程。直接发给我，无需重复说明模型；包内预演图只用于构图。
              </small>
              <button
                disabled={!profile}
                onClick={() =>
                  download(
                    `${shot.name}.director.json`,
                    JSON.stringify(compiled.json, null, 2),
                  )
                }
              >
                导出 JSON
              </button>
              <button
                className="gold"
                disabled={!profile}
                onClick={() =>
                  download(
                    `${shot.name}.prompt.txt`,
                    compiled.text,
                    "text/plain",
                  )
                }
              >
                <Download />
                反推说明 TXT
              </button>
              {busy ? (
                <button
                  className="cancel-btn"
                  onClick={() => {
                    abortControllerRef.current?.abort();
                    s.set({ notice: "正在中止导出..." });
                  }}
                  style={{
                    background: "#4a1d1d",
                    color: "#fc8181",
                    border: "1px solid #742a2a",
                    borderRadius: "6px",
                    padding: "8px 16px",
                    fontSize: "12px",
                    cursor: "pointer",
                    display: "inline-flex",
                    alignItems: "center",
                    gap: "6px",
                  }}
                >
                  <X size={14} /> 取消导出
                </button>
              ) : (
                <button
                  className="gold"
                  disabled={!profile}
                  onClick={() => void exportHandoff()}
                >
                  <Download />
                  导出交接包 ZIP
                </button>
              )}
            </div>
          </div>
        </div>
      )}
      {propsLibraryOpen && (
        <PropsLibraryModal onClose={() => setPropsLibraryOpen(false)} />
      )}
      <DraftsModal
        isOpen={draftsOpen}
        onClose={() => setDraftsOpen(false)}
        currentDraftId={getTabDraftId()}
        onSelectDraft={restoreDraftById}
        onNewDraft={handleNewDraft}
        currentSaveStatus={saveStatus}
        lastSavedAt={lastSavedAt}
      />
    </div>
  );
}
