// 本机工程库：草稿存成监看中心所在电脑上的文件，而不是只存在浏览器里。
// 模型和图片按内容哈希单独上传一次，工程文件里只留引用，保存和读取都只传变化的部分。
import { type Project, parseProject } from "./core";

export const ASSET_REF = "xbasset:sha256:";
const ALLOWED = new Set(["model/gltf-binary", "image/png", "image/jpeg", "image/webp", "application/octet-stream"]);

export type LibraryDraft = { id: string; name: string; updatedAt: number; shotCount: number };
type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

function decodeDataUrl(data: string, fallbackType: string | null): { type: string; bytes: Uint8Array } | null {
  const match = /^data:([^;,]*)((?:;[^;,]*)*?)(;base64)?,/.exec(data.slice(0, 200));
  if (!match || !match[3]) return null;
  const type = ALLOWED.has(match[1]) ? match[1] : fallbackType;
  if (!type) return null;
  const binary = atob(data.slice(match[0].length));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return { type, bytes };
}

function encodeDataUrl(type: string, bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return `data:${type};base64,${btoa(binary)}`;
}

async function sha256(bytes: Uint8Array): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes as BufferSource));
  return Array.from(digest, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function createLibrary(fetchImpl: FetchLike, base = "/stage/api") {
  const onServer = new Set<string>();          // 已确认在本机工程库里的素材
  const hashOf = new Map<string, string>();    // 素材记录 → 哈希（素材内容不可变，按 id 和长度记）
  const dataOf = new Map<string, string>();    // 哈希 → 浏览器里用的 data URL

  async function call(path: string, init?: RequestInit) {
    const response = await fetchImpl(base + path, { cache: "no-store", ...init });
    let payload: any = null;
    try {
      payload = await response.json();
    } catch {
      throw new Error(`本机工程库返回了无法识别的内容（HTTP ${response.status}）`);
    }
    return { status: response.status, payload };
  }
  const post = (path: string, body: unknown) =>
    call(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

  /** 把工程里内嵌的模型和图片换成引用，返回换好的工程和这次用到的素材。 */
  async function externalize(project: Project) {
    const pending = new Map<string, { type: string; bytes: Uint8Array }>();
    const swap = async (item: { id: string; data: string }, fallbackType: string | null) => {
      if (typeof item.data !== "string" || !item.data.startsWith("data:")) return item;
      const cacheKey = `${item.id}:${item.data.length}`;
      let hash = hashOf.get(cacheKey);
      if (!hash || !onServer.has(hash)) {
        const decoded = decodeDataUrl(item.data, fallbackType);
        if (!decoded) return item;               // 认不出的格式：照旧内嵌
        hash = hash ?? (await sha256(decoded.bytes));
        hashOf.set(cacheKey, hash);
        dataOf.set(hash, item.data);
        if (!onServer.has(hash)) pending.set(hash, decoded);
      }
      return { ...item, data: ASSET_REF + hash };
    };
    const assets = await Promise.all(project.assets.map((a) => (a.format === "glb" ? swap(a, "application/octet-stream") : a)));
    const sceneImages = project.sceneImages && (await Promise.all(project.sceneImages.map((i) => swap(i, null))));
    return { project: { ...project, assets, ...(sceneImages ? { sceneImages } : {}) } as Project, pending };
  }

  async function upload(pending: Map<string, { type: string; bytes: Uint8Array }>, force = false) {
    if (!pending.size) return;
    let wanted = [...pending.keys()];
    if (!force) {
      const { payload } = await post("/assets/missing", { hashes: wanted });
      if (!payload?.ok) throw new Error(payload?.error || "无法核对本机工程库里的素材");
      const missing = new Set<string>(payload.missing);
      for (const hash of wanted) if (!missing.has(hash)) onServer.add(hash);
      wanted = wanted.filter((hash) => missing.has(hash));
    }
    for (const hash of wanted) {
      const { type, bytes } = pending.get(hash)!;
      const { status, payload } = await call(`/asset?h=${hash}&type=${encodeURIComponent(type)}`, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: bytes as BodyInit,
      });
      if (status !== 200 || !payload?.ok) throw new Error(payload?.error || "素材保存到本机失败");
      onServer.add(hash);
    }
  }

  /** 把引用换回浏览器里用的内嵌数据。 */
  async function internalize(raw: any) {
    const restore = async (item: any) => {
      if (!item || typeof item.data !== "string" || !item.data.startsWith(ASSET_REF)) return item;
      const hash = item.data.slice(ASSET_REF.length);
      let data = dataOf.get(hash);
      if (!data) {
        const response = await fetchImpl(`${base}/asset?h=${hash}`, { cache: "force-cache" });
        if (!response.ok) throw new Error(`工程引用的素材「${item.name || item.id}」在本机工程库里找不到`);
        const type = (response.headers.get("Content-Type") || "application/octet-stream").split(";")[0];
        data = encodeDataUrl(type, new Uint8Array(await response.arrayBuffer()));
        dataOf.set(hash, data);
      }
      onServer.add(hash);
      hashOf.set(`${item.id}:${data.length}`, hash);
      return { ...item, data };
    };
    return {
      ...raw,
      assets: await Promise.all((raw.assets ?? []).map(restore)),
      ...(raw.sceneImages ? { sceneImages: await Promise.all(raw.sceneImages.map(restore)) } : {}),
    };
  }

  return {
    async available(): Promise<boolean> {
      try {
        const { status, payload } = await call("/library");
        return status === 200 && payload?.ok === true;
      } catch {
        return false;
      }
    },
    async list(): Promise<LibraryDraft[]> {
      const { payload } = await call("/library");
      if (!payload?.ok) throw new Error(payload?.error || "读不到本机工程库");
      return payload.drafts;
    },
    async save(id: string, project: Project, sessionOwner: string): Promise<number> {
      const { project: slim, pending } = await externalize(project);
      await upload(pending);
      const body = { id, name: project.name || "未命名项目", project: slim, sessionOwner };
      let { status, payload } = await post("/draft", body);
      if (status === 409 && Array.isArray(payload?.missing)) {
        // 本机工程库里的素材被清掉了：把这次工程用到的重新传一遍再存
        const again = new Map<string, { type: string; bytes: Uint8Array }>();
        for (const hash of payload.missing as string[]) {
          onServer.delete(hash);
          const data = dataOf.get(hash);
          const decoded = data ? decodeDataUrl(data, "application/octet-stream") : null;
          if (decoded) again.set(hash, decoded);
        }
        await upload(again, true);
        ({ status, payload } = await post("/draft", body));
      }
      if (status !== 200 || !payload?.ok) throw new Error(payload?.error || `保存到本机工程库失败（HTTP ${status}）`);
      return payload.updatedAt as number;
    },
    async load(id: string): Promise<{ project: Project; updatedAt: number } | null> {
      const { payload } = await call(`/draft?id=${encodeURIComponent(id)}`);
      if (!payload?.ok) throw new Error(payload?.error || "读取草稿失败");
      if (!payload.record?.project) return null;
      return { project: parseProject(await internalize(payload.record.project)), updatedAt: payload.record.updatedAt ?? 0 };
    },
    async remove(id: string): Promise<void> {
      const { status, payload } = await post("/draft/delete", { id });
      if (status !== 200 || !payload?.ok) throw new Error(payload?.error || "删除草稿失败");
    },
  };
}

export type Library = ReturnType<typeof createLibrary>;
