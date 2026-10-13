import {
  Aperture,
  ChevronRight,
  Code2,
  FolderOpen,
  Redo2,
  Save,
  Undo2,
} from "lucide-react";
import type { RefObject } from "react";
import type { SaveStatus } from "../storage";
import { useStore } from "../store";
import { download } from "../ui";
export function HeaderBar({
  saveStatus,
  lastSavedAt,
  mobilePane,
  setMobilePane,
  setDraftsOpen,
  setCompiler,
  projectFile,
}: {
  saveStatus: SaveStatus;
  lastSavedAt: number | null;
  mobilePane: "stage" | "monitor" | "hierarchy";
  setMobilePane: (pane: "stage" | "monitor" | "hierarchy") => void;
  setDraftsOpen: (open: boolean) => void;
  setCompiler: (open: boolean) => void;
  projectFile: RefObject<HTMLInputElement | null>;
}) {
  const s = useStore(),
    p = s.project;
  return (
    <header>
      <div className="brand">
        <div className="brand-mark">
          <Aperture size={24} />
        </div>
        <div>
          小八 <b>Director Stage</b>
          <small>VIRTUAL PRODUCTION WORKSPACE</small>
        </div>
      </div>
      <div className="project-title">
        <input
          aria-label="项目名称"
          value={p.name}
          onChange={(e) => s.renameProject(e.target.value)}
          onBlur={() => s.commitHistory()}
        />
        <div
          className={`save-pill save-${saveStatus}`}
          onClick={() => setDraftsOpen(true)}
          role="button"
          tabIndex={0}
          title={
            saveStatus === "saved"
              ? `本机草稿已保存 (${lastSavedAt ? new Date(lastSavedAt).toLocaleTimeString() : "已同步"}) · 点击打开草稿箱`
              : saveStatus === "saving"
              ? "草稿正在写入本机数据库... · 点击打开草稿箱"
              : saveStatus === "error"
              ? "本机草稿保存失败（监看中心未运行或存储空间不足）· 点击打开草稿箱"
              : "存在尚未持久化的修改 · 点击打开草稿箱"
          }
        >
          <span className="dot" />
          <small>
            {saveStatus === "saved"
              ? "草稿已保存"
              : saveStatus === "saving"
              ? "保存中..."
              : saveStatus === "error"
              ? "保存失败"
              : "未保存"}
          </small>
        </div>
        <span className="badge">v0.8.0</span>
      </div>
      <div className="actions">
        <div className="responsive-pane-switcher">
          <button
            className={mobilePane === "stage" ? "active" : ""}
            onClick={() => setMobilePane("stage")}
          >
            片场
          </button>
          <button
            className={mobilePane === "monitor" ? "active" : ""}
            onClick={() => setMobilePane("monitor")}
          >
            监看
          </button>
          <button
            className={mobilePane === "hierarchy" ? "active" : ""}
            onClick={() => setMobilePane("hierarchy")}
          >
            层级
          </button>
        </div>
        <div className="history-group">
          <button
            title="后退一步 (撤销) ⌘Z / Ctrl+Z"
            disabled={!s.canUndo}
            onClick={s.undo}
          >
            <Undo2 size={13} />
            <span>后退一步</span>
          </button>
          <button
            title="前进一步 (重做) ⌘⇧Z / Ctrl+Y"
            disabled={!s.canRedo}
            onClick={s.redo}
          >
            <Redo2 size={13} />
            <span>前进一步</span>
          </button>
        </div>
        <button
          title="本机草稿箱"
          onClick={() => setDraftsOpen(true)}
        >
          <Save size={13} />
          <span>草稿箱</span>
        </button>
        <button onClick={() => projectFile.current?.click()}>
          <FolderOpen />
          <span>打开</span>
        </button>
        <button
          onClick={() => {
            download(`${p.name}.xbdirector.json`, JSON.stringify(p, null, 2));
            s.set({ notice: "项目与内嵌素材已导出" });
          }}
        >
          <Save />
          <span>保存项目</span>
        </button>
        <button
          className="gold"
          onClick={() => {
            s.set({ playing: false });
            setCompiler(true);
          }}
        >
          <Code2 />
          <span>导演编译器</span>
          <ChevronRight />
        </button>
      </div>
    </header>
  );
}
