import { useState, useMemo } from "react";
import { X, Download, Plus, Search, Check } from "lucide-react";
import { presetModels, type PresetModel } from "./builtinModels";
import { type Asset, type StageObject, type V3, uuid, actor } from "./core";
import { decode, models } from "./assets";
import { useStore, active } from "./store";
import { zipSync, strToU8 } from "fflate";

export interface PrimitiveProp {
  id: string;
  name: string;
  category: "primitive";
  categoryZh: "基础几何";
  icon: string;
  description: string;
  size: V3;
  center: V3;
  color: string;
  shape: "box" | "cylinder" | "sphere" | "ramp";
}

export const primitiveProps: PrimitiveProp[] = [
  {
    id: "primitive_cube",
    name: "标准立方体",
    category: "primitive",
    categoryZh: "基础几何",
    icon: "🧊",
    description: "1m x 1m x 1m 空间遮挡与视线占位基准块",
    size: [1, 1, 1],
    center: [0, 0.5, 0],
    color: "#8ca292",
    shape: "box",
  },
  {
    id: "primitive_pedestal",
    name: "圆柱石台",
    category: "primitive",
    categoryZh: "基础几何",
    icon: "🏛️",
    description: "直径 0.8m x 高 0.9m 基础圆台，用于演员站立高位或视线中心",
    size: [0.8, 0.9, 0.8],
    center: [0, 0.45, 0],
    color: "#a4b0be",
    shape: "cylinder",
  },
  {
    id: "primitive_sphere",
    name: "视线瞄准球",
    category: "primitive",
    categoryZh: "基础几何",
    icon: "⚪",
    description: "直径 1.0m 空间标记球，用于演员眼神对视点、VFX追踪点与光影反射基准",
    size: [1, 1, 1],
    center: [0, 0.5, 0],
    color: "#e67e22",
    shape: "sphere",
  },
  {
    id: "primitive_ramp",
    name: "45° 阶梯斜坡",
    category: "primitive",
    categoryZh: "基础几何",
    icon: "📐",
    description: "长 2m x 宽 1m x 高 1m 倾斜坡道，用于高低落差视点与上下楼梯机位调度",
    size: [1, 1, 2],
    center: [0, 0.5, 0],
    color: "#57606f",
    shape: "ramp",
  },
];

export async function addPresetModelToStage(
  model: PresetModel,
  storeState: ReturnType<typeof useStore.getState>
) {
  const p = storeState.project;
  const shot = active(p);

  // Check if asset is already registered in project assets
  let existingAsset = p.assets.find((a) => a.name === `${model.id}.glb`);
  let assetId = existingAsset ? existingAsset.id : uuid();
  let assetToRegister: Asset | null = null;

  if (!existingAsset) {
    const asset: Asset = {
      id: assetId,
      name: `${model.id}.glb`,
      data: model.dataUri,
      format: "glb",
    };
    await decode(asset);
    assetToRegister = asset;
  } else {
    if (!models.has(assetId)) {
      await decode(existingAsset);
    }
  }

  // Sequential name
  const sameCount = shot.objects.filter((o) => o.name.startsWith(model.name)).length;
  const objName = sameCount > 0 ? `${model.name} ${sameCount + 1}` : model.name;

  const posY = model.boxMinY < 0 ? -model.boxMinY : 0;
  const newObj: StageObject = {
    id: uuid(),
    name: objName,
    kind: "model",
    assetId: assetId,
    position: [0, posY, 0],
    rotation: [0, 0, 0],
    scale: 1,
    visible: true,
    locked: false,
    color: "#a9bac9",
    size: model.size,
    center: model.center,
  };

  if (assetToRegister) {
    storeState.set({
      project: {
        ...storeState.project,
        assets: [...storeState.project.assets, assetToRegister],
      },
    });
  }

  storeState.edit((sh) => {
    sh.objects.push(newObj);
  });

  storeState.set({
    selected: newObj.id,
    notice: `已向舞台添加影视专业素材【${objName}】(物理尺寸: ${model.size[0]}m × ${model.size[1]}m × ${model.size[2]}m)`,
  });
  storeState.commitHistory();
}

export function addPrimitivePropToStage(
  prop: PrimitiveProp,
  storeState: ReturnType<typeof useStore.getState>
) {
  const shot = active(storeState.project);
  const sameCount = shot.objects.filter((o) => o.name.startsWith(prop.name)).length;
  const objName = sameCount > 0 ? `${prop.name} ${sameCount + 1}` : prop.name;

  const newObj: StageObject = {
    ...actor(objName, [0, 0, 0], prop.color),
    kind: "prop" as const,
    size: prop.size,
    center: prop.center,
    shape: prop.shape,
  };

  storeState.edit((sh) => {
    sh.objects.push(newObj);
  });

  storeState.set({
    selected: newObj.id,
    notice: `已向舞台添加基础几何【${objName}】`,
  });
  storeState.commitHistory();
}

export function downloadGlbFile(model: PresetModel) {
  const byteCharacters = atob(model.dataUri.split(",")[1]);
  const byteNumbers = new Array(byteCharacters.length);
  for (let i = 0; i < byteCharacters.length; i++) {
    byteNumbers[i] = byteCharacters.charCodeAt(i);
  }
  const byteArray = new Uint8Array(byteNumbers);
  const blob = new Blob([byteArray], { type: "model/gltf-binary" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${model.id}.glb`;
  a.click();
  URL.revokeObjectURL(url);
}

export function downloadAllModelsZip() {
  const zipFiles: Record<string, Uint8Array> = {};

  for (const model of presetModels) {
    const b64 = model.dataUri.split(",")[1];
    const binStr = atob(b64);
    const u8 = new Uint8Array(binStr.length);
    for (let i = 0; i < binStr.length; i++) {
      u8[i] = binStr.charCodeAt(i);
    }
    zipFiles[`models/${model.id}.glb`] = u8;
  }

  zipFiles["README.txt"] = strToU8(
    `【小八导演台影视级 3D 预演素材包】\n\n本压缩包包含 14 款专为影视导演运镜设计、片场走位调度与摄影机/灯光机位规划的标准 3D 模型（GLTF 2.0 / GLB 格式）。\n可直接在小八导演台中加载，亦可导入 Cine Tracer、Unreal Engine、Blender、Maya 或 Cinema 4D。\n\n包含素材清单：\n` +
      presetModels
        .map(
          (m, idx) =>
            `${idx + 1}. [${m.categoryZh}] 【${m.name}】(${m.id}.glb) - 物理尺寸: ${m.size[0]}m × ${m.size[1]}m × ${m.size[2]}m - ${m.description}`
        )
        .join("\n") +
      `\n\n小八导演台 © 2026\n`
  );

  const zipped = zipSync(zipFiles);
  const blob = new Blob([zipped as any], { type: "application/zip" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `xiaoba-director-props-pack.zip`;
  a.click();
  URL.revokeObjectURL(url);
}

export function PropsLibraryModal({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<string>("all");
  const [query, setQuery] = useState<string>("");
  const [addingId, setAddingId] = useState<string | null>(null);

  const filteredPresetModels = useMemo(() => {
    return presetModels.filter((m) => {
      const matchTab = tab === "all" || m.category === tab;
      const matchQuery =
        !query.trim() ||
        m.name.toLowerCase().includes(query.toLowerCase()) ||
        m.description.toLowerCase().includes(query.toLowerCase()) ||
        m.categoryZh.includes(query);
      return matchTab && matchQuery;
    });
  }, [tab, query]);

  const filteredPrimitives = useMemo(() => {
    if (tab !== "all" && tab !== "primitive") return [];
    return primitiveProps.filter((p) => {
      return (
        !query.trim() ||
        p.name.toLowerCase().includes(query.toLowerCase()) ||
        p.description.toLowerCase().includes(query.toLowerCase())
      );
    });
  }, [tab, query]);

  const handleAddPreset = async (m: PresetModel) => {
    setAddingId(m.id);
    try {
      await addPresetModelToStage(m, useStore.getState());
      setTimeout(() => {
        setAddingId(null);
        onClose();
      }, 150);
    } catch (e: any) {
      alert("添加模型失败: " + e.message);
      setAddingId(null);
    }
  };

  const handleAddPrimitive = (p: PrimitiveProp) => {
    addPrimitivePropToStage(p, useStore.getState());
    onClose();
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal props-library-modal"
        onClick={(e) => e.stopPropagation()}
        style={{ maxWidth: 880, width: "94vw", maxHeight: "90vh" }}
      >
        <div className="modal-header">
          <div>
            <span className="eyebrow">DIRECTOR & CINEMATOGRAPHY PREVIS ASSETS</span>
            <h2>片场与导演专业 3D 预演素材库</h2>
            <p>对标 Cine Tracer 与 FrameForge · 片场调度地标 · 摄影轨道机位 · 影视灯光与动作道具</p>
          </div>
          <button title="关闭" onClick={onClose}>
            <X />
          </button>
        </div>

        <div className="props-modal-toolbar">
          <div className="props-category-tabs">
            <button
              className={tab === "all" ? "active" : ""}
              onClick={() => setTab("all")}
            >
              全部 ({presetModels.length + primitiveProps.length})
            </button>
            <button
              className={tab === "blocking" ? "active" : ""}
              onClick={() => setTab("blocking")}
            >
              🎬 导演调度 (4)
            </button>
            <button
              className={tab === "filming_gear" ? "active" : ""}
              onClick={() => setTab("filming_gear")}
            >
              🎥 摄影器材 (2)
            </button>
            <button
              className={tab === "lighting" ? "active" : ""}
              onClick={() => setTab("lighting")}
            >
              💡 灯光控光 (3)
            </button>
            <button
              className={tab === "narrative" ? "active" : ""}
              onClick={() => setTab("narrative")}
            >
              🎭 叙事动作 (4)
            </button>
            <button
              className={tab === "architecture" ? "active" : ""}
              onClick={() => setTab("architecture")}
            >
              🟩 视效置景 (1)
            </button>
            <button
              className={tab === "primitive" ? "active" : ""}
              onClick={() => setTab("primitive")}
            >
              ⬛ 基础几何 (4)
            </button>
          </div>

          <div className="props-search-row">
            <div className="props-search-box">
              <Search size={14} />
              <input
                type="text"
                placeholder="搜索导演调度、摄影、灯光或动作道具（如：导演椅、苹果箱、轨道、魔术腿、柔光箱、手枪、手机）..."
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
              {query && (
                <button className="clear-query" onClick={() => setQuery("")}>
                  ×
                </button>
              )}
            </div>

            <button
              className="btn-download-pack"
              title="打包导出全部 14 款电影级 GLB 格式素材包"
              onClick={downloadAllModelsZip}
            >
              <Download size={14} />
              打包下载全部模型 (ZIP)
            </button>
          </div>
        </div>

        <div className="props-grid-container">
          {filteredPresetModels.length === 0 && filteredPrimitives.length === 0 && (
            <div className="props-empty-hint">
              未找到与 “{query}” 匹配的素材，请尝试不同关键词
            </div>
          )}

          {/* 3D Preset Models */}
          {filteredPresetModels.map((m) => {
            const isAdding = addingId === m.id;
            return (
              <div key={m.id} className="prop-card">
                <div className="prop-card-header">
                  <div className="prop-icon-badge">{m.icon}</div>
                  <div className="prop-title-box">
                    <div className="prop-name-line">
                      <span className="prop-name">{m.name}</span>
                      <span className="prop-cat-badge">{m.categoryZh}</span>
                    </div>
                    <span className="prop-dim-badge">
                      {m.size[0]}m × {m.size[1]}m × {m.size[2]}m
                    </span>
                  </div>
                </div>

                <p className="prop-desc">{m.description}</p>

                <div className="prop-actions">
                  <button
                    className="prop-btn-add"
                    disabled={isAdding}
                    onClick={() => handleAddPreset(m)}
                  >
                    {isAdding ? <Check size={14} /> : <Plus size={14} />}
                    {isAdding ? "已加入舞台" : "放入舞台"}
                  </button>
                  <button
                    className="prop-btn-dl"
                    title={`下载 ${m.name} 的标准 .glb 文件 (${m.fileSizeKb}KB)`}
                    onClick={() => downloadGlbFile(m)}
                  >
                    <Download size={13} />
                    .GLB
                  </button>
                </div>
              </div>
            );
          })}

          {/* Primitive Props */}
          {filteredPrimitives.map((p) => (
            <div key={p.id} className="prop-card primitive-card">
              <div className="prop-card-header">
                <div className="prop-icon-badge">{p.icon}</div>
                <div className="prop-title-box">
                  <div className="prop-name-line">
                    <span className="prop-name">{p.name}</span>
                    <span className="prop-cat-badge">基础几何</span>
                  </div>
                  <span className="prop-dim-badge">
                    {p.size[0]}m × {p.size[1]}m × {p.size[2]}m
                  </span>
                </div>
              </div>

              <p className="prop-desc">{p.description}</p>

              <div className="prop-actions">
                <button
                  className="prop-btn-add"
                  onClick={() => handleAddPrimitive(p)}
                >
                  <Plus size={14} />
                  放入舞台
                </button>
              </div>
            </div>
          ))}
        </div>

        <div className="modal-footer props-modal-footer">
          <small>
            🎬 调度技巧：添加【导演椅】或【苹果箱】后，可直接将角色放置在其上方坐立；放置【摄影轨道】可精确可视化运镜横移起止距离；放置【走位 T 型地标】可直观规划演员走位轨迹。
          </small>
          <button className="primary" onClick={onClose}>
            完成
          </button>
        </div>
      </div>
    </div>
  );
}
