import {
  Clapperboard,
  Move,
  Plus,
  Redo2,
  RotateCw,
  Scaling,
  Undo2,
} from "lucide-react";
import Stage from "../Stage";
import { active, useStore } from "../store";
import { Boundary } from "../ui";
import { viewportBridge } from "../viewportBridge";
export function ViewportPanel() {
  const s = useStore(),
    p = s.project,
    shot = active(p);
  return (
    <main className="viewport">
      <div className="viewport-toolbar">
        <span>
          <span className="dot" /> 导演视口 <small>PERSPECTIVE</small>
        </span>
        <div className="tools">
          <button
            title="后退一步 (撤销) ⌘Z / Ctrl+Z"
            disabled={!s.canUndo}
            onClick={s.undo}
          >
            <Undo2 size={13} />
          </button>
          <button
            title="前进一步 (重做) ⌘⇧Z / Ctrl+Y"
            disabled={!s.canRedo}
            onClick={s.redo}
          >
            <Redo2 size={13} />
          </button>
          <span className="tool-sep" />
          {(
            [
              ["translate", Move, "W"],
              ["rotate", RotateCw, "E"],
              ["scale", Scaling, "R"],
            ] as const
          ).map(([mode, Icon, key]) => (
            <button
              key={mode}
              className={s.mode === mode ? "active" : ""}
              title={
                { translate: "位移", rotate: "旋转", scale: "等比缩放" }[
                  mode
                ]
              }
              disabled={
                (s.selected.startsWith("camera") && mode === "scale") ||
                (s.selected === "camera-target" && mode === "rotate")
              }
              onClick={() => s.set({ mode })}
            >
              <Icon />
              <kbd>{key}</kbd>
            </button>
          ))}
        </div>
      </div>
      <div className="camera-view-toolbar">
        <button
          className={s.editorView === "stage" ? "active" : ""}
          onClick={() => s.set({ editorView: "stage", pilot: false })}
        >
          自由观察
        </button>
        <button
          className={s.editorView === "camera" ? "active" : ""}
          onClick={() =>
            s.set({ editorView: "camera", selected: "camera" })
          }
        >
          主相机视角
        </button>
        {s.editorView === "stage" ? (
          <>
            <button onClick={() => viewportBridge.useEditorAsCamera?.()}>
              视角设为机位
            </button>
            <button
              className={s.showFrustum ? "active" : ""}
              onClick={() => s.set({ showFrustum: !s.showFrustum })}
            >
              取景虚线
            </button>
          </>
        ) : (
          <button
            className={s.pilot ? "active" : ""}
            onClick={() => s.set({ pilot: !s.pilot, playing: false })}
          >
            {s.pilot ? "退出机位操控" : "操控机位"}
          </button>
        )}
      </div>
      <div className="editor">
        <Boundary>
          <Stage edit />
        </Boundary>
        <div className="viewport-tag">
          SCENE 01 <span> / </span>
          {shot.name}
        </div>
        <div className="viewport-help">
          {s.editorView === "stage"
            ? "自由观察不改变机位 · 选摄影机后 W 移动 / E 转向 · 蓝点为视线目标"
            : s.pilot
              ? "正在操控真实机位：左键环绕 / 右键平移 / 滚轮推拉"
              : "与右侧监看同一摄影机 · 同步 16:9 构图"}
        </div>
        <div className="axis-note">
          Y UP <span>1m GRID</span>
        </div>
      </div>
      <div className="shot-strip">
        <Clapperboard size={17} />
        {p.shots.map((sh, i) => (
          <button
            key={sh.id}
            className={sh.id === shot.id ? "active" : ""}
            onClick={() => {
              s.switchShot(sh.id);
              s.seek(0);
            }}
          >
            <small>{String(i + 1).padStart(2, "0")}</small>
            {sh.name}
          </button>
        ))}
        <button title="复制当前镜头" onClick={s.addShot}>
          <Plus />
        </button>
      </div>
    </main>
  );
}
