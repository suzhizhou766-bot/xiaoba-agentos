import { Diamond, Focus, Image as ImageIcon, Trash2, Video } from "lucide-react";
import { POSES, POSE_IDS, poseBounds, type PoseId } from "../poses";
import { LightingPanel } from "../LightingPanel";
import { SpatialMonitor } from "../SpatialMonitor";
import Stage from "../Stage";
import { frameCamera, frames, type V3 } from "../core";
import { groundedPositionY, spatialFrame } from "../spatial";
import { active, useStore } from "../store";
import { Boundary, Field, Vector } from "../ui";
export function InspectorPanel({
  tab,
  setTab,
  guide,
  setGuide,
  screenshot,
  exportVideo,
  busy,
}: {
  tab: "camera" | "object" | "lighting";
  setTab: (tab: "camera" | "object" | "lighting") => void;
  guide: boolean;
  setGuide: (guide: boolean) => void;
  screenshot: () => void;
  exportVideo: () => Promise<void>;
  busy: boolean;
}) {
  const s = useStore(),
    shot = active(s.project),
    selected = shot.objects.find((o) => o.id === s.selected),
    cam = shot.camera;
  const currentFrame = spatialFrame(shot, s.time);
  const currentCoverage = currentFrame.coverage;
  return (
    <aside className="inspector">
      <div className="panel-heading">
        主摄影机监看{" "}
        <span>
          <i className="live-dot" /> LIVE
        </span>
      </div>
      <div className="monitor">
        <Boundary>
          <Stage />
        </Boundary>
        {guide && (
          <div className="guides">
            <div className="thirds" />
            <div className="safe" />
          </div>
        )}
        {currentCoverage && !currentCoverage.covered && (
          <div className="monitor-overflow-hud">
            {currentCoverage.overflow?.top && <div className="edge-warning edge-top" />}
            {currentCoverage.overflow?.bottom && <div className="edge-warning edge-bottom" />}
            {currentCoverage.overflow?.left && <div className="edge-warning edge-left" />}
            {currentCoverage.overflow?.right && <div className="edge-warning edge-right" />}
            <span className="overflow-pill">⚠ 背景越界</span>
          </div>
        )}
        <span className="monitor-label">CAM A · {cam.focalLength}mm</span>
        <span className="monitor-time">{s.time.toFixed(2)}s</span>
      </div>
      <div className="monitor-tools">
        <button
          className={guide ? "active" : ""}
          onClick={() => setGuide(!guide)}
        >
          <Focus />
          辅助线
        </button>
        <span>16:9 · 24fps</span>
        <button title="导出当前画面" onClick={screenshot}>
          <ImageIcon />
        </button>
        <button title="导出机位运动视频（MP4）" onClick={exportVideo} disabled={busy}>
          <Video />
        </button>
      </div>
      <SpatialMonitor />
      <div className="tabs">
        <button
          className={tab === "camera" ? "active" : ""}
          onClick={() => setTab("camera")}
        >
          摄影机
        </button>
        <button
          className={tab === "object" ? "active" : ""}
          onClick={() => setTab("object")}
        >
          对象属性
        </button>
        <button
          className={tab === "lighting" ? "active" : ""}
          onClick={() => setTab("lighting")}
        >
          光影布光
        </button>
      </div>
      <div className="properties">
        {tab === "camera" ? (
          <>
            <div className="section-title">
              OPTICS <span>镜头与构图</span>
            </div>
            <Field
              label="焦距"
              value={cam.focalLength}
              min={8}
              max={300}
              step={1}
              unit="mm"
              onChange={(n) => s.camera({ focalLength: n })}
            />
            <Field
              label="水平 FOV"
              value={
                (2 *
                  Math.atan(cam.sensorWidth / (2 * cam.focalLength)) *
                  180) /
                Math.PI
              }
              min={7}
              max={120}
              unit="°"
              onChange={(n) =>
                s.camera({
                  focalLength: Math.max(
                    8,
                    Math.min(
                      300,
                      cam.sensorWidth / (2 * Math.tan((n * Math.PI) / 360)),
                    ),
                  ),
                })
              }
            />
            <Field
              label="传感器宽"
              value={cam.sensorWidth}
              min={8}
              max={70}
              unit="mm"
              onChange={(n) => s.camera({ sensorWidth: n })}
            />
            <div className="preset-grid-header">
              <span>景别预设 (一键构图)</span>
              <small>自动拉近/推远机位</small>
            </div>
            <div className="preset-grid">
              {Object.keys(frames).map((f) => {
                const meta = frames[f];
                return (
                  <button
                    className={cam.framing === f ? "active" : ""}
                    key={f}
                    title={`${meta.label} (${f}) · ${meta.desc}`}
                    onClick={() => {
                      if (!selected) {
                        s.set({
                          notice:
                            "请先在层级树选择角色或模型，再使用景别预设",
                        });
                        return;
                      }
                      s.camera(frameCamera(cam, selected, f));
                      s.set({
                        notice: `已按 ${selected.name} 包围盒设置 ${meta.label}（${f}）构图`,
                      });
                    }}
                  >
                    <span className="preset-name">{meta.label}</span>
                    <span className="preset-code">{f}</span>
                  </button>
                );
              })}
            </div>
            <p className="hint">
              {selected
                ? `已选主体：${selected.name} · 点击景别自动计算镜头距离与视线高度。`
                : "请先在舞台或层级树选择角色，点击景别自动设置拍摄距离与高度。"}
            </p>
            <div className="section-title">
              POSITION <span>机位与视线</span>
            </div>
            <Vector
              label="摄影机位置 / m"
              value={cam.position}
              onChange={(v) => s.camera({ position: v })}
            />
            <Field
              label="机位高度"
              value={cam.position[1]}
              onChange={(n) =>
                s.camera({
                  position: [cam.position[0], n, cam.position[2]],
                })
              }
              unit="m"
            />
            <Field
              label="画面滚转"
              value={((cam.roll ?? 0) * 180) / Math.PI}
              min={-180}
              max={180}
              unit="°"
              onChange={(n) => s.camera({ roll: (n * Math.PI) / 180 })}
            />
            <label className="field">
              <span>LookAt</span>
              <select
                value={cam.lookAt ?? ""}
                onChange={(e) =>
                  s.camera({ lookAt: e.target.value || null })
                }
              >
                <option value="">固定目标点</option>
                {shot.objects.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.name}
                  </option>
                ))}
              </select>
            </label>
            {!cam.lookAt && (
              <Vector
                label="目标点 / m"
                value={cam.target}
                onChange={(v) => s.camera({ target: v })}
              />
            )}
            <div className="camera-actions">
              <button
                onClick={() =>
                  s.set({
                    selected: "camera",
                    editorView: "stage",
                    mode: "translate",
                  })
                }
              >
                拖动机位
              </button>
              <button
                onClick={() =>
                  s.set({
                    selected: "camera-target",
                    editorView: "stage",
                    mode: "translate",
                  })
                }
              >
                拖动视线目标
              </button>
            </div>
            <button className="wide" onClick={() => s.key("camera")}>
              <Diamond />
              记录相机关键帧
            </button>
            <div className="section-title">
              DEPTH OF FIELD <span>生成意图参数</span>
            </div>
            <Field
              label="光圈"
              value={cam.aperture}
              min={0.7}
              max={32}
              unit="f/"
              onChange={(n) => s.camera({ aperture: n })}
            />
            <Field
              label="对焦距离"
              value={cam.focusDistance}
              min={0.01}
              max={1000}
              unit="m"
              onChange={(n) => s.camera({ focusDistance: n })}
            />
            <label className="check">
              <input
                type="checkbox"
                checked={cam.dof}
                onChange={(e) => s.camera({ dof: e.target.checked })}
              />
              景深意图写入提示词
            </label>
            <p className="hint">MVP 监看不渲染光学散景。</p>
          </>
        ) : tab === "lighting" ? (
          <LightingPanel />
        ) : selected ? (
          <>
            <div className="section-title">
              OBJECT <span>{selected.kind.toUpperCase()}</span>
            </div>
            <input
              aria-label="对象名称"
              className="name-input"
              disabled={selected.locked}
              value={selected.name}
              onChange={(e) =>
                s.object(selected.id, { name: e.target.value })
              }
            />
            <small className="uuid">UUID {selected.id}</small>
            <fieldset disabled={selected.locked}>
              <Vector
                label="位置 / m"
                value={selected.position}
                onChange={(v) => s.object(selected.id, { position: v })}
              />
              <Vector
                label="旋转 / °"
                value={
                  selected.rotation.map((v) => (v * 180) / Math.PI) as V3
                }
                onChange={(v) =>
                  s.object(selected.id, {
                    rotation: v.map((x) => (x * Math.PI) / 180) as V3,
                  })
                }
              />
              {selected.kind === "actor" && !selected.assetId && (
                <label className="field">
                  姿态
                  <select
                    aria-label="角色姿态"
                    value={selected.pose ?? "stand"}
                    onChange={(e) => {
                      const pose = e.target.value as PoseId;
                      // 姿态改变了身高和占地，包围盒跟着改，自动构图和空间检查才对得上
                      s.object(selected.id, { pose, ...poseBounds(pose) });
                      s.commitHistory();
                    }}
                  >
                    {POSE_IDS.map((id) => (
                      <option key={id} value={id}>
                        {POSES[id].label}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              {selected.kind === "prop" && <Vector label="代理尺寸 / m" value={selected.size} onChange={v=>{const size=v.map(n=>Math.max(0.01,n)) as V3;s.object(selected.id,{size,center:[0,size[1]/2,0]});}} />}
              <Field
                label="等比缩放"
                value={selected.scale}
                min={0.01}
                max={100}
                onChange={(n) => s.object(selected.id, { scale: n })}
              />
              <button
                type="button"
                className="wide"
                style={{ margin: "4px 0" }}
                onClick={() => {
                  const newY = groundedPositionY(selected, 0);
                  s.object(selected.id, {
                    position: [selected.position[0], newY, selected.position[2]],
                  });
                  s.set({ notice: `已将 ${selected.name} 脚底贴齐地面 Y=0` });
                }}
              >
                脚底贴齐地面 (Y=0)
              </button>
              <button className="wide" onClick={() => s.key(selected.id)}>
                <Diamond />
                记录对象状态
              </button>
              <button className="wide danger" onClick={s.remove}>
                <Trash2 />
                删除对象
              </button>
            </fieldset>
            <p className="hint">
              {selected.locked
                ? "对象已锁定，点击层级树锁图标解锁。"
                : "W 位移 · E 旋转 · R 等比缩放（拖动 X 手柄）"}
            </p>
          </>
        ) : (
          <p className="hint">在场景或层级树中选择一个对象。</p>
        )}
      </div>
    </aside>
  );
}
