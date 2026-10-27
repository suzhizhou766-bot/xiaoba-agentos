import { useState, useEffect } from "react";
import { X, Trash2, FolderOpen, Plus, Clock, Film, CheckCircle2, Copy } from "lucide-react";
import {
  listDraftsResult,
  deleteDraft,
  checkDraftConflictInOtherTabs,
  forkDraft,
  draftStorageKind,
  type DraftSummary,
  type SaveStatus,
} from "./storage";

interface DraftsModalProps {
  isOpen: boolean;
  onClose: () => void;
  currentDraftId: string;
  onSelectDraft: (draftId: string) => Promise<void>;
  onNewDraft: () => void;
  currentSaveStatus: SaveStatus;
  lastSavedAt: number | null;
}

export function DraftsModal({
  isOpen,
  onClose,
  currentDraftId,
  onSelectDraft,
  onNewDraft,
  currentSaveStatus,
  lastSavedAt,
}: DraftsModalProps) {
  const [drafts, setDrafts] = useState<DraftSummary[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionDraftId, setActionDraftId] = useState<string | null>(null);
  const [storageKind, setStorageKind] = useState<"library" | "browser" | null>(null);

  const refreshList = async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const res = await listDraftsResult();
      setDrafts(res.drafts);
      if (res.error) setLoadError(res.error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (isOpen) {
      void refreshList();
      void draftStorageKind().then(setStorageKind);
    }
  }, [isOpen]);

  if (!isOpen) return null;

  const filtered = drafts.filter((d) =>
    (d.name || "未命名项目").toLowerCase().includes(search.trim().toLowerCase()),
  );

  const handleDelete = async (d: DraftSummary) => {
    if (
      !window.confirm(
        `确定要永久删除草稿「${d.name || "未命名项目"}」吗？\n删除后数据不可恢复。`,
      )
    ) {
      return;
    }
    setActionDraftId(d.id);
    try {
      const res = await deleteDraft(d.id);
      if (!res.success) {
        alert("删除草稿失败：" + (res.error || "未知存储错误"));
      } else {
        await refreshList();
      }
    } finally {
      setActionDraftId(null);
    }
  };

  const handleFork = async (d: DraftSummary) => {
    setActionDraftId(d.id);
    try {
      const forked = await forkDraft(d.id);
      if (!forked) {
        alert("创建草稿副本失败，请检查存储配额");
      } else {
        await refreshList();
      }
    } finally {
      setActionDraftId(null);
    }
  };

  const handleSelect = async (draftId: string) => {
    setActionDraftId(draftId);
    try {
      if (draftId !== currentDraftId) {
        const inOtherTab = await checkDraftConflictInOtherTabs(draftId);
        if (inOtherTab) {
          const doFork = window.confirm(
            "检测到该草稿当前正由另一个浏览器标签页打开编辑！\n\n为防止两个标签页自动保存相互覆盖数据，建议【复制为新独立草稿】进行编辑。\n\n点击「确定」：复制为新独立草稿编辑（推荐）\n点击「取消」：强行以原草稿接管编辑",
          );
          if (doFork) {
            const forked = await forkDraft(draftId);
            if (forked) {
              await onSelectDraft(forked.id);
              onClose();
              return;
            } else {
              alert("创建草稿副本失败，请检查存储配额");
              return;
            }
          }
        }
      }
      await onSelectDraft(draftId);
      onClose();
    } catch (err) {
      alert("无法安全切换草稿：" + ((err as Error).message || "冲突探测失败"));
    } finally {
      setActionDraftId(null);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal drafts-modal"
        onClick={(e) => e.stopPropagation()}
        style={{ maxWidth: "780px", height: "80vh" }}
      >
        <div className="modal-header">
          <div>
            <span className="eyebrow">LOCAL DRAFTS</span>
            <h2>小八导演台 · 本机草稿箱</h2>
            <p>
              {storageKind === "library"
                ? "草稿保存在这台电脑的本机工程库里（小八监看 / 小八3D导演台 / 工程库），换浏览器、清浏览器数据都不会丢。"
                : "草稿保存在当前浏览器的本地数据库中，关闭或刷新页面不丢失；清除浏览器数据会一并清掉。"}
            </p>
          </div>
          <button title="关闭" onClick={onClose}>
            <X size={18} />
          </button>
        </div>

        <div className="drafts-toolbar">
          <div className="drafts-search">
            <input
              type="text"
              placeholder="搜索历史草稿名称..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <button
            className="drafts-new-btn gold"
            onClick={() => {
              if (
                window.confirm(
                  "将开启一个全新的空白草稿会话。系统将在切换前自动保存当前草稿，是否继续？",
                )
              ) {
                onNewDraft();
                onClose();
              }
            }}
          >
            <Plus size={14} />
            新建空白草稿
          </button>
        </div>

        <div className="drafts-list-container">
          {loading ? (
            <div className="drafts-empty">正在扫描本机草稿数据库...</div>
          ) : loadError ? (
            <div className="drafts-empty" style={{ color: "#ef4444" }}>
              扫描本机草稿失败：{loadError}
            </div>
          ) : filtered.length === 0 ? (
            <div className="drafts-empty">
              {search.trim() ? "未找到符合搜索条件的草稿" : "暂无历史草稿"}
            </div>
          ) : (
            <div className="drafts-grid">
              {filtered.map((d) => {
                const isCurrent = d.id === currentDraftId;
                const isBusy = actionDraftId === d.id;

                return (
                  <div
                    key={d.id}
                    className={`draft-card ${isCurrent ? "active-draft" : ""}`}
                  >
                    <div className="draft-card-main">
                      <div className="draft-card-header">
                        <span className="draft-name" title={d.name}>
                          {d.name || "未命名项目"}
                        </span>
                        {isCurrent && (
                          <span className="draft-badge current">
                            <CheckCircle2 size={11} /> 当前正在编辑
                          </span>
                        )}
                      </div>

                      <div className="draft-meta">
                        <span title="最后修改时间">
                          <Clock size={12} />
                          {new Date(d.updatedAt).toLocaleString("zh-CN", {
                            month: "numeric",
                            day: "numeric",
                            hour: "2-digit",
                            minute: "2-digit",
                            second: "2-digit",
                          })}
                        </span>
                        <span title="镜头数量">
                          <Film size={12} />
                          {d.shotCount} 个镜头
                        </span>
                        {isCurrent && (
                          <span className={`save-status-indicator save-${currentSaveStatus}`}>
                            ● {currentSaveStatus === "saved"
                              ? "已同步"
                              : currentSaveStatus === "saving"
                              ? "正在保存"
                              : currentSaveStatus === "error"
                              ? "保存异常"
                              : "有变动未存"}
                          </span>
                        )}
                      </div>
                    </div>

                    <div className="draft-card-actions">
                      <button
                        className="draft-action-btn load"
                        disabled={isBusy || (isCurrent && currentSaveStatus === "saved")}
                        onClick={() => void handleSelect(d.id)}
                        title={isCurrent ? "重新载入当前已存状态" : "加载此草稿到片场"}
                      >
                        <FolderOpen size={13} />
                        {isCurrent ? "当前会话" : "载入草稿"}
                      </button>
                      <button
                        className="draft-action-btn fork"
                        disabled={isBusy}
                        onClick={() => void handleFork(d)}
                        title="复制为新独立草稿"
                      >
                        <Copy size={13} />
                        副本
                      </button>
                      <button
                        className="draft-action-btn delete"
                        disabled={isBusy}
                        onClick={() => void handleDelete(d)}
                        title="删除该草稿"
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="modal-footer drafts-modal-footer">
          <small>
            💡 多标签页各自拥有独立草稿 ID；若载入同一草稿，系统会自动检测并提示复制副本，彻底防止互相覆盖。
          </small>
          <button onClick={onClose}>关闭</button>
        </div>
      </div>
    </div>
  );
}
