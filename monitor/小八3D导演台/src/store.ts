import { create } from "zustand";
import {
  type Project,
  type Shot,
  type StageObject,
  type Camera,
  type AxisHint,
  initial,
  copy,
  uuid,
  sampledShot,
} from "./core";
import { gcAssets } from "./assets";
export type { AxisHint };
type State = {
  project: Project;
  revision: number;
  selected: string;
  mode: "translate" | "rotate" | "scale";
  time: number;
  playing: boolean;
  notice: string;
  editorView: "stage" | "camera";
  pilot: boolean;
  showFrustum: boolean;
  groundPlacement: boolean;
  autoKey: boolean;
  canUndo: boolean;
  canRedo: boolean;
  undo: () => void;
  redo: () => void;
  commitHistory: () => void;
  clearHistory: () => void;
  resetSession: (project: Project, notice?: string) => void;
  axisHint: AxisHint | null;
  setAxisHint: (hint: AxisHint | null) => void;
  set: (v: Partial<State>) => void;
  edit: (fn: (s: Shot) => void, discrete?: boolean) => void;
  object: (id: string, v: Partial<StageObject>) => void;
  camera: (v: Partial<Camera>) => void;
  seek: (t: number) => void;
  key: (target: string) => void;
  switchShot: (id: string) => void;
  addShot: () => void;
  remove: () => void;
  togglePlay: () => void;
  renameProject: (name: string) => void;
};
export const active = (p: Project) =>
  p.shots.find((s) => s.id === p.activeShot)!;
function upsert(shot: Shot, target: string, time: number) {
  const o = shot.objects.find((o) => o.id === target);
  if (target !== "camera" && (!o || o.locked)) return;
  // 覆盖同一时刻的关键帧时保留它已设的运动曲线。
  const previous = shot.keys.find((k) => k.target === target && Math.abs(k.time - time) < 1e-6);
  shot.keys = shot.keys.filter(
    (k) => !(k.target === target && Math.abs(k.time - time) < 1e-6),
  );
  shot.keys.push({
    id: uuid(),
    target,
    time,
    ...(previous?.ease && previous.ease !== "linear" ? { ease: previous.ease } : {}),
    position: copy(target === "camera" ? shot.camera.position : o!.position),
    ...(target === "camera" ? { camera: copy(shot.camera) } : {rotation:copy(o!.rotation),scale:o!.scale}),
  });
}
const MAX_HISTORY = 50;
const MAX_HISTORY_BYTES = 8 * 1024 * 1024;
// Media records are immutable and shared; only editable scene data is copied.
function snapshotProject(p: Project): Project {
  return {...p, assets: [...p.assets], sceneImages: [...(p.sceneImages ?? [])], shots: copy(p.shots)};
}
export function retainedProjects(extraProjects: Project[] = []): Project[] {
  const current = useStore?.getState?.()?.project;
  return [
    ...(current ? [current] : []),
    ...extraProjects,
    ...past.map((x) => x.project),
    ...future.map((x) => x.project),
    ...(pendingSnapshot ? [pendingSnapshot.project] : []),
  ];
}
export function pruneUnusedAssets(extraProjects: Project[] = []) {
  const retained = retainedProjects(extraProjects);
  const retainedAssetIds = new Set<string>();
  for (const p of retained) {
    if (p?.assets) {
      for (const a of p.assets) {
        retainedAssetIds.add(a.id);
      }
    }
  }
  gcAssets(retainedAssetIds);
}
function trimHistory(incomingProject?: Project) {
  while (past.length > MAX_HISTORY) past.shift();
  let bytes = 0;
  for (let i = past.length - 1; i >= 0; i--) {
    bytes += JSON.stringify(past[i].project.shots).length * 2;
    if (bytes > MAX_HISTORY_BYTES && i < past.length - 1) { past.splice(0, i + 1); break; }
  }
  pruneUnusedAssets(incomingProject ? [incomingProject] : []);
}
let past: Array<{ project: Project; time: number; selected: string }> = [];
let future: Array<{ project: Project; time: number; selected: string }> = [];
let pendingSnapshot: { project: Project; time: number; selected: string } | null = null;
let debounceTimer: ReturnType<typeof setTimeout> | null = null;
let isApplyingHistory = false;

function commitPending(set: (v: Partial<State>) => void) {
  if (debounceTimer) {
    clearTimeout(debounceTimer);
    debounceTimer = null;
  }
  if (!pendingSnapshot) return;
  past.push(pendingSnapshot);
  trimHistory();
  future = [];
  pendingSnapshot = null;
  set({ canUndo: past.length > 0, canRedo: false });
}

function recordChange(
  get: () => State,
  set: (v: Partial<State>) => void,
  discrete = false,
  incomingProject?: Project,
) {
  if (isApplyingHistory) return;
  const current = (discrete || !pendingSnapshot) ? {
    project: snapshotProject(get().project),
    time: get().time,
    selected: get().selected,
  } : pendingSnapshot!;
  // Any new edit invalidates redo immediately, including during debounce.
  future = [];
  set({canRedo: false, canUndo: true});
  if (discrete) {
    if (debounceTimer) {
      clearTimeout(debounceTimer);
      debounceTimer = null;
    }
    if (pendingSnapshot) {
      past.push(pendingSnapshot);
      pendingSnapshot = null;
    }
    past.push(current);
    trimHistory(incomingProject);
    future = [];
    set({ canUndo: past.length > 0, canRedo: false });
  } else {
    if (!pendingSnapshot) {
      pendingSnapshot = current;
    }
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      commitPending(set);
    }, 400);
  }
}

export const useStore = create<State>((set, get) => ({
  project: initial(),
  revision: 0,
  selected: "camera",
  mode: "translate",
  time: 0,
  playing: false,
  notice: "准备就绪 · 选择主摄影机可直接拖动机位",
  editorView: "stage",
  pilot: false,
  showFrustum: true,
  groundPlacement: false,
  autoKey: false,
  canUndo: false,
  canRedo: false,
  axisHint: null,
  setAxisHint: (hint) => {
    if (hint) {
      set({
        axisHint: hint,
        notice: `机位控制：【${hint.axis} 轴 · ${hint.title}】${hint.desc}`,
      });
    } else {
      set({ axisHint: null });
    }
  },
  set: (v) => {
    if (v.project && v.project !== get().project) {
      const p = get().project;
      const onlyNameChanged =
        v.project.name !== p.name &&
        v.project.shots === p.shots &&
        v.project.assets === p.assets &&
        v.project.sceneImages === p.sceneImages;
      recordChange(get, set, !onlyNameChanged, v.project);
      set({...v, revision: get().revision + 1});
    } else set(v);
  },
  renameProject: (name) => {
    const p = get().project;
    if (p.name === name) return;
    recordChange(get, set, false);
    set({
      revision: get().revision + 1,
      project: { ...p, name },
    });
  },
  edit: (fn, discrete = false) => {
    recordChange(get, set, discrete);
    const p = get().project;
    const shot = copy(active(p));
    fn(shot);
    set({
      revision: get().revision + 1,
      project: {
        ...p,
        shots: p.shots.map((s) => (s.id === shot.id ? shot : s)),
      },
    });
  },
  object: (id, v) => {
    get().edit((s) => {
      const o = s.objects.find((o) => o.id === id);
      if (o && !o.locked) {
        Object.assign(o, v);
        if (get().autoKey && (v.position || v.rotation || v.scale !== undefined)) upsert(s, id, get().time);
      }
    });
    set({ playing: false });
  },
  camera: (v) => {
    get().edit((s) => {
      Object.assign(s.camera, v);
      if (get().autoKey) upsert(s, "camera", get().time);
    });
    set({ playing: false });
  },
  seek: (t) => {
    const p = get().project,
      shot = active(p);
    const time = Math.min(shot.duration, Math.max(0, Math.round(t * 24) / 24));
    if (!Number.isFinite(time)) return;
    if (time === get().time && get().playing) return;
    const pose = sampledShot(shot, time);
    set({
      time,
      project: {
        ...p,
        shots: p.shots.map((s) => (s.id === shot.id ? pose : s)),
      },
    });
  },
  key: (target) => {
    const actual = target === "camera-target" ? "camera" : target;
    const o = active(get().project).objects.find((o) => o.id === actual);
    if (actual !== "camera" && (!o || o.locked)) {
      set({ notice: "锁定或无效对象不能打关键帧" });
      return;
    }
    get().edit((s) => upsert(s, actual, get().time), true);
    set({
      notice: `已记录 ${get().time.toFixed(3)}s ${actual === "camera" ? "摄影机机位 / 朝向 / 焦距" : "对象位置 / 旋转 / 缩放"}关键帧`,
    });
  },
  togglePlay: () => {
    const state = get(),
      s = active(state.project);
    if (state.playing) {
      set({ playing: false });
      return;
    }
    if (!s.keys.length) {
      set({
        notice: "尚无关键帧：先记录起点，定位到另一时间，调整机位后记录终点",
      });
      return;
    }
    if (state.time >= s.out || state.time < s.in) state.seek(s.in);
    set({ playing: true, pilot: false });
  },
  switchShot: (id) => {
    const p = get().project;
    if (!p.shots.some((s) => s.id === id)) return;
    set({
      revision: get().revision + 1,
      project: { ...p, activeShot: id },
      time: 0,
      playing: false,
      selected: "camera",
      pilot: false,
    });
    get().seek(0);
  },
  addShot: () => {
    recordChange(get, set, true);
    const p = get().project,
      s = copy(active(p));
    s.id = uuid();
    s.name = `镜头 ${String(p.shots.length + 1).padStart(2, "0")}`;
    set({
      revision: get().revision + 1,
      project: { ...p, shots: [...p.shots, s], activeShot: s.id },
      time: 0,
      playing: false,
      selected: "camera",
      notice: "已复制当前镜头",
    });
    get().seek(0);
  },
  remove: () => {
    const id = get().selected;
    if (id.startsWith("camera")) {
      set({ notice: "主摄影机不能删除" });
      return;
    }
    if (active(get().project).objects.find((o) => o.id === id)?.locked) {
      set({ notice: "对象已锁定，请先解锁" });
      return;
    }
    get().edit((s) => {
      s.objects = s.objects.filter((o) => o.id !== id);
      s.keys = s.keys.filter((k) => k.target !== id);
      if (s.camera.lookAt === id) s.camera.lookAt = null;
      s.keys.forEach((k) => {
        if (k.camera?.lookAt === id) k.camera.lookAt = null;
      });
    }, true);
    set({ selected: "camera" });
  },
  undo: () => {
    if (debounceTimer) {
      clearTimeout(debounceTimer);
      debounceTimer = null;
    }
    if (pendingSnapshot) {
      past.push(pendingSnapshot);
      pendingSnapshot = null;
    }
    if (!past.length) return;
    const current = {
      project: snapshotProject(get().project),
      time: get().time,
      selected: get().selected,
    };
    const prev = past.pop()!;
    future.push(current);
    if (future.length > MAX_HISTORY) future.shift();

    isApplyingHistory = true;
    set({
      revision: get().revision + 1,
      project: prev.project,
      time: prev.time,
      selected: prev.selected,
      canUndo: past.length > 0,
      canRedo: true,
      notice: "已后退一步（撤销）",
      playing: false,
    });
    isApplyingHistory = false;
  },
  redo: () => {
    if (pendingSnapshot) { commitPending(set); return; }
    if (debounceTimer) {
      clearTimeout(debounceTimer);
      debounceTimer = null;
    }
    pendingSnapshot = null;
    if (!future.length) return;
    const current = {
      project: snapshotProject(get().project),
      time: get().time,
      selected: get().selected,
    };
    const next = future.pop()!;
    past.push(current);
    trimHistory();

    isApplyingHistory = true;
    set({
      revision: get().revision + 1,
      project: next.project,
      time: next.time,
      selected: next.selected,
      canUndo: true,
      canRedo: future.length > 0,
      notice: "已前进一步（重做）",
      playing: false,
    });
    isApplyingHistory = false;
  },
  commitHistory: () => {
    commitPending(set);
  },
  clearHistory: () => {
    if (debounceTimer) {
      clearTimeout(debounceTimer);
      debounceTimer = null;
    }
    past = [];
    future = [];
    pendingSnapshot = null;
    set({ canUndo: false, canRedo: false });
    pruneUnusedAssets();
  },
  resetSession: (project, notice) => {
    if (debounceTimer) {
      clearTimeout(debounceTimer);
      debounceTimer = null;
    }
    past = [];
    future = [];
    pendingSnapshot = null;
    isApplyingHistory = false;
    set({
      project,
      revision: get().revision + 1,
      time: 0,
      selected: "camera",
      playing: false,
      canUndo: false,
      canRedo: false,
      notice: notice ?? get().notice,
    });
    pruneUnusedAssets();
  },
}));
