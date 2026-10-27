import {
  Box,
  Camera as CameraIcon,
  ChevronRight,
  Download,
  Eye,
  EyeOff,
  Lock,
  Plus,
  Unlock,
} from "lucide-react";
import type { RefObject } from "react";
import { getPresetModel } from "../builtinModels";
import { actor } from "../core";
import { addPresetModelToStage } from "../PropsLibraryModal";
import { SceneImagesPanel } from "../SceneImagesPanel";
import { active, useStore } from "../store";
export function HierarchyPanel({
  importing,
  busy,
  setPropsLibraryOpen,
  files,
}: {
  importing: (list: File[]) => Promise<void>;
  busy: boolean;
  setPropsLibraryOpen: (open: boolean) => void;
  files: RefObject<HTMLInputElement | null>;
}) {
  const s = useStore(),
    shot = active(s.project);
  return (
    <aside className="hierarchy">
      <div className="panel-heading">
        场景层级 <span>SCENE</span>
      </div>
      <div className="scene-root">
        <ChevronRight size={14} /> {shot.name}
      </div>
      <button
        className={`tree-row ${s.selected === "camera" ? "selected" : ""}`}
        onClick={() => s.set({ selected: "camera" })}
      >
        <CameraIcon />
        <span>主摄影机</span>
        <small>CAM A</small>
      </button>
      <div className="tree-label">
        舞台对象 <span>{shot.objects.length}</span>
      </div>
      {shot.objects.map((o) => (
        <div
          className={`tree-row ${s.selected === o.id ? "selected" : ""}`}
          key={o.id}
        >
          <button
            className="tree-name"
            onClick={() => s.set({ selected: o.id })}
          >
            <Box />
            <span>{o.name}</span>
          </button>
          <button
            title={o.visible ? "隐藏" : "显示"}
            onClick={() =>
              s.edit((sh) => {
                sh.objects.find((x) => x.id === o.id)!.visible = !o.visible;
              })
            }
          >
            {o.visible ? <Eye /> : <EyeOff />}
          </button>
          <button
            title={o.locked ? "解锁" : "锁定"}
            onClick={() =>
              s.edit((sh) => {
                sh.objects.find((x) => x.id === o.id)!.locked = !o.locked;
              })
            }
          >
            {o.locked ? <Lock /> : <Unlock />}
          </button>
        </div>
      ))}
      <SceneImagesPanel onImport={list=>void importing(list)} busy={busy} />
      <div className="static-row">
        ▦ <span>地面 / 参考网格</span>
        <Lock size={12} />
      </div>
      <div className="add-buttons">
        <button
          onClick={() => {
            const o = actor(`角色 ${shot.objects.length + 1}`, [0, 0, 0]);
            s.edit((sh) => sh.objects.push(o));
            s.set({ selected: o.id });
          }}
        >
          <Plus />
          角色
        </button>
        <button
          className="btn-props-library"
          title="打开 3D 道具与置景素材库（含桌、椅、沙发、床、门、车、箱等 12 款精选模型与几何体）"
          onClick={() => setPropsLibraryOpen(true)}
        >
          <Box />
          3D 道具库
        </button>
      </div>
      <div className="quick-props-bar">
        <button
          className="quick-prop-pill"
          title="快速向舞台放入经典导演折叠椅"
          onClick={() => {
            const m = getPresetModel("director_chair");
            if (m) void addPresetModelToStage(m, useStore.getState());
          }}
        >
          🎬 导演椅
        </button>
        <button
          className="quick-prop-pill"
          title="快速向舞台放入标准苹果箱 (全号)"
          onClick={() => {
            const m = getPresetModel("apple_box_full");
            if (m) void addPresetModelToStage(m, useStore.getState());
          }}
        >
          🧰 苹果箱
        </button>
        <button
          className="quick-prop-pill"
          title="快速向舞台放入演员走位 T 型地标"
          onClick={() => {
            const m = getPresetModel("actor_t_marker");
            if (m) void addPresetModelToStage(m, useStore.getState());
          }}
        >
          🎯 走位地标
        </button>
        <button
          className="quick-prop-pill"
          title="快速向舞台放入摄影轨道与移动推车"
          onClick={() => {
            const m = getPresetModel("camera_dolly_track");
            if (m) void addPresetModelToStage(m, useStore.getState());
          }}
        >
          🛤️ 摄影轨道
        </button>
        <button
          className="quick-prop-pill"
          title="快速向舞台放入魔术腿与遮光黑旗板"
          onClick={() => {
            const m = getPresetModel("c_stand_flag");
            if (m) void addPresetModelToStage(m, useStore.getState());
          }}
        >
          🚏 魔术腿
        </button>
        <button
          className="quick-prop-pill"
          title="快速向舞台放入影室柔光箱 (主光)"
          onClick={() => {
            const m = getPresetModel("studio_softbox");
            if (m) void addPresetModelToStage(m, useStore.getState());
          }}
        >
          💡 柔光箱
        </button>
        <button
          className="quick-prop-pill"
          title="快速向舞台放入手枪动作道具"
          onClick={() => {
            const m = getPresetModel("prop_handgun");
            if (m) void addPresetModelToStage(m, useStore.getState());
          }}
        >
          🔫 手枪
        </button>
      </div>
      <button
        className="import-box"
        disabled={busy}
        onClick={() => files.current?.click()}
      >
        <Download size={22} />
        <b>{busy ? "正在载入素材…" : "导入 3D 素材"}</b>
        <small>拖入 GLB / GLTF 与依赖文件</small>
      </button>
      <div className="scene-notes">
        <span className="dot" /> WORLD SPACE
        <small>
          单位：米 · Y 轴向上
          <br />
          场景状态随镜头独立保存
        </small>
      </div>
    </aside>
  );
}
