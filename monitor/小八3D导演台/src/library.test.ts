import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { initial, type Project } from "./core";
import { createLibrary, ASSET_REF } from "./library";

/** 内存里的假工程库，协议与监看中心的 /stage/api 一致。 */
function fakeServer() {
  const drafts = new Map<string, any>();
  const assets = new Map<string, { type: string; bytes: Uint8Array }>();
  const log: string[] = [];
  let clock = 1000;
  const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  const fetchImpl = async (input: string, init: RequestInit = {}) => {
    const url = new URL(input, "http://x");
    const route = `${init.method ?? "GET"} ${url.pathname.replace("/stage/api", "")}`;
    log.push(route);
    if (route === "GET /library")
      return json(200, { ok: true, drafts: [...drafts.values()].sort((a, b) => b.updatedAt - a.updatedAt).map((r) => ({ id: r.id, name: r.name, updatedAt: r.updatedAt, shotCount: r.project.shots.length })) });
    if (route === "GET /draft") return json(200, { ok: true, record: drafts.get(url.searchParams.get("id")!) ?? null });
    if (route === "POST /assets/missing") return json(200, { ok: true, missing: JSON.parse(init.body as string).hashes.filter((h: string) => !assets.has(h)) });
    if (route === "POST /asset") {
      const bytes = new Uint8Array(init.body as Uint8Array);
      const hash = url.searchParams.get("h")!;
      if (createHash("sha256").update(bytes).digest("hex") !== hash) return json(400, { ok: false, error: "素材内容与编号不符" });
      assets.set(hash, { type: url.searchParams.get("type")!, bytes });
      return json(200, { ok: true });
    }
    if (route === "GET /asset") {
      const asset = assets.get(url.searchParams.get("h")!);
      return asset ? new Response(asset.bytes as BodyInit, { status: 200, headers: { "Content-Type": asset.type } }) : new Response("no", { status: 404 });
    }
    if (route === "POST /draft") {
      const body = JSON.parse(init.body as string);
      const missing = [...JSON.stringify(body.project).matchAll(/xbasset:sha256:([0-9a-f]{64})/g)].map((m) => m[1]).filter((h) => !assets.has(h));
      if (missing.length) return json(409, { ok: false, error: "有素材还没有保存到本机", missing });
      drafts.set(body.id, { ...body, updatedAt: ++clock });
      return json(200, { ok: true, updatedAt: drafts.get(body.id).updatedAt });
    }
    if (route === "POST /draft/delete") { drafts.delete(JSON.parse(init.body as string).id); return json(200, { ok: true }); }
    return json(404, { ok: false, error: "not found" });
  };
  return { fetchImpl, drafts, assets, log };
}

const dataUrl = (type: string, bytes: number[]) => `data:${type};base64,${Buffer.from(bytes).toString("base64")}`;
function projectWithMedia(): Project {
  const p = initial();
  p.name = "夜戏";
  p.assets = [
    { id: "a1", name: "chair.glb", format: "glb", data: dataUrl("model/gltf-binary", [1, 2, 3, 4, 250, 251]) },
    { id: "a2", name: "odd.glb", format: "glb", data: dataUrl("application/x-unknown", [9, 9, 9]) },
    { id: "a3", name: "scene.gltf", format: "gltf", data: '{"asset":{"version":"2.0"}}' },
  ];
  p.sceneImages = [{ id: "i1", name: "bg.png", data: dataUrl("image/png", [137, 80, 78, 71, 5, 6]), width: 2, height: 2 }];
  return p;
}

test("save stores media once by hash and load gives back the identical project", async () => {
  const server = fakeServer();
  const library = createLibrary(server.fetchImpl);
  assert.equal(await library.available(), true);
  const project = projectWithMedia();
  await library.save("d1", project, "tab-1");
  const stored = server.drafts.get("d1");
  assert.equal(stored.sessionOwner, "tab-1");
  assert.ok(stored.project.assets[0].data.startsWith(ASSET_REF));
  assert.ok(stored.project.assets[1].data.startsWith(ASSET_REF));           // 认不出类型的 glb 按二进制存
  assert.equal(stored.project.assets[2].data, '{"asset":{"version":"2.0"}}'); // gltf 是文字，留在工程里
  assert.ok(stored.project.sceneImages[0].data.startsWith(ASSET_REF));
  assert.equal(server.assets.size, 3);
  assert.equal(JSON.stringify(stored).includes("base64"), false);
  assert.equal(project.assets[0].data.startsWith("data:"), true);            // 传进来的工程本身不被改动

  const uploadsBefore = server.log.filter((r) => r === "POST /asset").length;
  await library.save("d1", project, "tab-1");
  await library.save("d2", project, "tab-1");
  assert.equal(server.log.filter((r) => r === "POST /asset").length, uploadsBefore);   // 再存不重复上传
  assert.equal(server.assets.size, 3);

  const fresh = createLibrary(server.fetchImpl);                               // 换一个浏览器会话来读
  const loaded = await fresh.load("d1");
  assert.equal(loaded!.project.name, "夜戏");
  assert.equal(loaded!.project.assets[0].data, project.assets[0].data);
  assert.equal(loaded!.project.sceneImages![0].data, project.sceneImages![0].data);
  assert.deepEqual(Buffer.from(loaded!.project.assets[1].data.split(",")[1], "base64"), Buffer.from([9, 9, 9]));
  assert.deepEqual((await fresh.list()).map((d) => d.id), ["d2", "d1"]);
  assert.equal(await fresh.load("nope"), null);
});

test("assets wiped on the server are re-uploaded on the next save; a missing asset fails the load clearly", async () => {
  const server = fakeServer();
  const library = createLibrary(server.fetchImpl);
  const project = projectWithMedia();
  await library.save("d1", project, "tab");
  server.assets.clear();                                                      // 工程库里的素材被清掉
  await library.save("d1", project, "tab");
  assert.equal(server.assets.size, 3);
  server.assets.clear();
  await assert.rejects(createLibrary(server.fetchImpl).load("d1"), /找不到/);
  await library.remove("d1");
  assert.deepEqual(await library.list(), []);
});

test("an unreachable or broken library is reported as unavailable or as an error, never as success", async () => {
  const down = createLibrary(async () => { throw new TypeError("fetch failed"); });
  assert.equal(await down.available(), false);
  const html = createLibrary(async () => new Response("<html>", { status: 200 }));
  assert.equal(await html.available(), false);
  const refusing = createLibrary(async () => new Response(JSON.stringify({ ok: false, error: "磁盘已满" }), { status: 500 }));
  await assert.rejects(refusing.save("d", initial(), "tab"), /磁盘已满/);
  await assert.rejects(refusing.list(), /磁盘已满/);
});
