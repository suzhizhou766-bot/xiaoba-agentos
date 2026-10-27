import { type Project, parseProject } from "./core";
import { createLibrary, type Library } from "./library";

const DB_NAME = "xbdirector_db";
const DB_VERSION = 1;
const STORE_DRAFTS = "drafts";

export type DraftMetadata = {
  id: string;
  name: string;
  updatedAt: number;
  project: Project;
  sessionOwner?: string;
};

export type SaveStatus = "saved" | "saving" | "unsaved" | "error";

let dbPromise: Promise<IDBDatabase> | null = null;

function getDB(): Promise<IDBDatabase> {
  if (dbPromise) return dbPromise;
  if (typeof indexedDB === "undefined") {
    return Promise.reject(new Error("当前环境不支持 IndexedDB"));
  }
  const opening = new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE_DRAFTS)) {
        const store = db.createObjectStore(STORE_DRAFTS, { keyPath: "id" });
        store.createIndex("updatedAt", "updatedAt", { unique: false });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("打开数据库失败"));
  });
  const cached: Promise<IDBDatabase> = opening.catch((error: unknown) => {
    if (dbPromise === cached) dbPromise = null;
    throw error;
  });
  dbPromise = cached;
  return cached;
}

// ---- 本机工程库 ----
// 由小八监看中心提供页面时，草稿存成那台电脑上的文件（/stage/api）；其他情况（开发服务、
// 单独打开构建产物、测试）沿用浏览器里的 IndexedDB。下面每个读写函数先问一次用哪边。
const MIGRATED_KEY = "xbdirector_library_migrated";
let libraryPromise: Promise<Library | null> | null = null;

function serverLibrary(): Promise<Library | null> {
  if (libraryPromise) return libraryPromise;
  libraryPromise = (async () => {
    if (typeof location === "undefined" || typeof fetch === "undefined" || !location.pathname.startsWith("/stage")) return null;
    const library = createLibrary((input, init) => fetch(input, init));
    if (!(await library.available())) return null;
    await migrateBrowserDrafts(library);
    return library;
  })();
  return libraryPromise;
}

/** 第一次用上本机工程库时，把这个浏览器里已有的草稿搬过去一份（浏览器里的原样保留）。 */
async function migrateBrowserDrafts(library: Library): Promise<void> {
  try {
    if (typeof localStorage === "undefined" || localStorage.getItem(MIGRATED_KEY)) return;
    const existing = new Set((await library.list()).map((d) => d.id));
    for (const draft of (await browserListDrafts()).drafts) {
      if (existing.has(draft.id)) continue;
      const loaded = await browserLoadDraft(draft.id);
      if (loaded.project) await library.save(draft.id, loaded.project, currentSessionTabId);
    }
    localStorage.setItem(MIGRATED_KEY, String(Date.now()));
  } catch {
    // 搬不过去不影响使用：下次打开再试
  }
}

/** 当前草稿存在哪里，给界面显示用。 */
export async function draftStorageKind(): Promise<"library" | "browser"> {
  return (await serverLibrary()) ? "library" : "browser";
}

const TAB_DRAFT_KEY = "xbdirector_tab_draft_id";

export const currentSessionTabId =
  typeof window !== "undefined"
    ? "tab-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8)
    : "node-session";

let presenceChannel: BroadcastChannel | null = null;
try {
  if (typeof BroadcastChannel !== "undefined") {
    presenceChannel = new BroadcastChannel("xbdirector_presence");
    presenceChannel.onmessage = (e) => {
      const data = e.data;
      if (!data || typeof data !== "object") return;
      if (
        data.type === "ping" &&
        data.draftId === getTabDraftId() &&
        data.fromTabId !== currentSessionTabId
      ) {
        presenceChannel?.postMessage({
          type: "pong",
          draftId: data.draftId,
          fromTabId: currentSessionTabId,
        });
      }
    };
  }
} catch {
  // BroadcastChannel unavailable
}

export async function checkDraftConflictInOtherTabs(draftId: string): Promise<boolean> {
  const channel = presenceChannel;
  if (!channel) return false;
  return new Promise((resolve, reject) => {
    let resolved = false;
    const cleanup = () => {
      resolved = true;
      clearTimeout(timeout);
      channel.removeEventListener("message", onMsg);
    };

    const onMsg = (e: MessageEvent) => {
      const data = e.data;
      if (
        data &&
        data.type === "pong" &&
        data.draftId === draftId &&
        data.fromTabId !== currentSessionTabId
      ) {
        if (!resolved) {
          cleanup();
          resolve(true);
        }
      }
    };

    const timeout = setTimeout(() => {
      cleanup();
      resolve(false);
    }, 100);
    try {
      channel.addEventListener("message", onMsg);
      channel.postMessage({ type: "ping", draftId, fromTabId: currentSessionTabId });
    } catch (err) {
      cleanup();
      reject(err);
    }
  });
}

function generateDraftId(): string {
  return "draft-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
}

export function getTabDraftId(): string {
  if (typeof sessionStorage === "undefined") return "default-draft";
  let id = sessionStorage.getItem(TAB_DRAFT_KEY);
  if (!id) {
    id = generateDraftId();
    sessionStorage.setItem(TAB_DRAFT_KEY, id);
  }
  return id;
}

export function setTabDraftId(id: string): void {
  if (typeof sessionStorage !== "undefined") {
    sessionStorage.setItem(TAB_DRAFT_KEY, id);
  }
}

export function createNewTabDraftId(): string {
  const id = generateDraftId();
  setTabDraftId(id);
  return id;
}

export async function saveDraft(
  draftId: string,
  project: Project,
): Promise<{ success: boolean; updatedAt: number; error?: string }> {
  const library = await serverLibrary();
  if (library) {
    try {
      return { success: true, updatedAt: await library.save(draftId, project, currentSessionTabId) };
    } catch (err) {
      return { success: false, updatedAt: Date.now(), error: (err as Error).message || "保存到本机工程库失败" };
    }
  }
  try {
    const db = await getDB();
    const updatedAt = Date.now();
    const record: DraftMetadata = {
      id: draftId,
      name: project.name || "未命名项目",
      updatedAt,
      project: JSON.parse(JSON.stringify(project)), // ensure serializable
      sessionOwner: currentSessionTabId,
    };

    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE_DRAFTS, "readwrite");
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error || new Error("保存事务失败"));
      tx.onabort = () => reject(tx.error || new Error("保存事务中止"));
      tx.objectStore(STORE_DRAFTS).put(record);
    });

    return { success: true, updatedAt };
  } catch (err) {
    return {
      success: false,
      updatedAt: Date.now(),
      error: (err as Error).message || "未知存储错误",
    };
  }
}

export type LoadDraftResult = {
  project: Project | null;
  error?: string;
};

export async function loadDraftResult(draftId: string): Promise<LoadDraftResult> {
  const library = await serverLibrary();
  if (library) {
    try {
      const loaded = await library.load(draftId);
      return { project: loaded ? loaded.project : null };
    } catch (err) {
      return { project: null, error: (err as Error).message || "读取草稿异常" };
    }
  }
  return browserLoadDraft(draftId);
}

async function browserLoadDraft(draftId: string): Promise<LoadDraftResult> {
  try {
    const db = await getDB();
    const record = await new Promise<DraftMetadata | undefined>((resolve, reject) => {
      const tx = db.transaction(STORE_DRAFTS, "readonly");
      tx.onerror = () => reject(tx.error || new Error("读取事务失败"));
      const req = tx.objectStore(STORE_DRAFTS).get(draftId);
      req.onsuccess = () => resolve(req.result as DraftMetadata | undefined);
      req.onerror = () => reject(req.error || new Error("查询记录失败"));
    });

    if (!record || !record.project) return { project: null };
    return { project: parseProject(record.project) };
  } catch (err) {
    return { project: null, error: (err as Error).message || "读取草稿异常" };
  }
}

export async function loadDraft(draftId: string): Promise<Project | null> {
  const res = await loadDraftResult(draftId);
  return res.project;
}

export async function forkDraft(
  sourceDraftId: string,
  newNameSuffix = " (副本)",
): Promise<{ id: string; project: Project } | null> {
  const source = await loadDraft(sourceDraftId);
  if (!source) return null;
  const newId = generateDraftId();
  const forkedProject: Project = {
    ...source,
    name: (source.name || "未命名项目") + newNameSuffix,
  };
  const res = await saveDraft(newId, forkedProject);
  if (!res.success) return null;
  return { id: newId, project: forkedProject };
}

export async function getLatestDraft(): Promise<{ id: string; project: Project; updatedAt: number } | null> {
  const library = await serverLibrary();
  if (library) {
    try {
      for (const draft of await library.list()) {
        try {
          const loaded = await library.load(draft.id);
          if (loaded) return { id: draft.id, project: loaded.project, updatedAt: loaded.updatedAt };
        } catch {
          continue;   // 这份坏了就看下一份
        }
      }
    } catch {
      // 读不到列表：当作没有
    }
    return null;
  }
  try {
    const db = await getDB();
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_DRAFTS, "readonly");
      tx.onerror = () => reject(tx.error || new Error("读取事务失败"));
      const store = tx.objectStore(STORE_DRAFTS);
      const index = store.index("updatedAt");
      const req = index.openCursor(null, "prev");
      req.onsuccess = () => {
        const cursor = req.result;
        if (cursor) {
          const val = cursor.value as DraftMetadata;
          try {
            const project = parseProject(val.project);
            resolve({ id: val.id, project, updatedAt: val.updatedAt });
          } catch {
            cursor.continue();
          }
        } else {
          resolve(null);
        }
      };
      req.onerror = () => reject(req.error || new Error("游标扫描失败"));
    });
  } catch {
    return null;
  }
}

export type DraftSummary = {
  id: string;
  name: string;
  updatedAt: number;
  shotCount: number;
};

export type ListDraftsResult = {
  drafts: DraftSummary[];
  error?: string;
};

export async function listDraftsResult(): Promise<ListDraftsResult> {
  const library = await serverLibrary();
  if (library) {
    try {
      return { drafts: await library.list() };
    } catch (err) {
      return { drafts: [], error: (err as Error).message || "草稿扫描失败" };
    }
  }
  return browserListDrafts();
}

async function browserListDrafts(): Promise<ListDraftsResult> {
  try {
    const db = await getDB();
    const list = await new Promise<DraftSummary[]>((resolve, reject) => {
      const tx = db.transaction(STORE_DRAFTS, "readonly");
      tx.onerror = () => reject(tx.error || new Error("读取事务失败"));
      const store = tx.objectStore(STORE_DRAFTS);
      const index = store.index("updatedAt");
      const req = index.openCursor(null, "prev");
      const items: DraftSummary[] = [];
      req.onsuccess = () => {
        const cursor = req.result;
        if (cursor) {
          const val = cursor.value as DraftMetadata;
          items.push({
            id: val.id,
            name: val.name || "未命名项目",
            updatedAt: val.updatedAt,
            shotCount: val.project?.shots?.length || 0,
          });
          cursor.continue();
        } else {
          resolve(items);
        }
      };
      req.onerror = () => reject(req.error || new Error("游标扫描失败"));
    });
    return { drafts: list };
  } catch (err) {
    return { drafts: [], error: (err as Error).message || "草稿扫描失败" };
  }
}

export async function listDrafts(): Promise<DraftSummary[]> {
  const res = await listDraftsResult();
  return res.drafts;
}

export async function deleteDraft(
  draftId: string,
): Promise<{ success: boolean; error?: string }> {
  const library = await serverLibrary();
  if (library) {
    try {
      await library.remove(draftId);
      return { success: true };
    } catch (err) {
      return { success: false, error: (err as Error).message || "删除草稿失败" };
    }
  }
  try {
    const db = await getDB();
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE_DRAFTS, "readwrite");
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error || new Error("删除事务失败"));
      tx.onabort = () => reject(tx.error || new Error("删除事务中止"));
      tx.objectStore(STORE_DRAFTS).delete(draftId);
    });
    return { success: true };
  } catch (err) {
    return { success: false, error: (err as Error).message || "删除草稿失败" };
  }
}
