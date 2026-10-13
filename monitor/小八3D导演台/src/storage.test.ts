import { test } from "node:test";
import assert from "node:assert/strict";
import { initial } from "./core";

test("draft storage preserves identity, recovers connections and cleans presence checks", async (t) => {
  const originals = Object.fromEntries(["indexedDB", "sessionStorage", "BroadcastChannel"].map(k => [k, Object.getOwnPropertyDescriptor(globalThis, k)]));
  t.after(() => {
    for (const [key, descriptor] of Object.entries(originals)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else Reflect.deleteProperty(globalThis, key);
    }
  });
  const values = new Map<string, string>();
  const records = new Map<string, any>();
  let openCalls = 0;
  let failWrite = false;
  let channel: Presence;
  class Presence {
    onmessage: any;
    listeners = new Set<(event: any) => void>();
    mode: "timeout" | "respond" | "throw" = "timeout";
    constructor() { channel = this; }
    addEventListener(_type: string, callback: (event: any) => void) { this.listeners.add(callback); }
    removeEventListener(_type: string, callback: (event: any) => void) { this.listeners.delete(callback); }
    postMessage(data: any) {
      if (this.mode === "throw") throw new Error("channel closed");
      if (this.mode === "respond") queueMicrotask(() => this.emit(data.draftId));
    }
    emit(draftId: string) {
      for (const listener of this.listeners) listener({ data: { type: "pong", draftId, fromTabId: "other-tab" } });
    }
  }
  const db = {
    transaction() {
      const tx: any = {};
      tx.objectStore = () => ({
        get(id: string) {
          const request: any = {};
          queueMicrotask(() => { request.result = structuredClone(records.get(id)); request.onsuccess(); });
          return request;
        },
        put(record: any) {
          queueMicrotask(() => {
            if (failWrite) { tx.error = new Error("quota failure"); tx.onabort(); }
            else { records.set(record.id, structuredClone(record)); tx.oncomplete(); }
          });
        },
      });
      return tx;
    },
  };
  Object.defineProperties(globalThis, {
    sessionStorage: { configurable: true, value: { getItem: (k: string) => values.get(k) ?? null, setItem: (k: string, v: string) => values.set(k, v) } },
    BroadcastChannel: { configurable: true, value: Presence },
    indexedDB: { configurable: true, value: { open() {
      openCalls++;
      if (openCalls === 1) throw new Error("synchronous open failure");
      const request: any = {};
      queueMicrotask(() => {
        if (openCalls === 2) { request.error = new Error("temporary open failure"); request.onerror(); }
        else { request.result = db; request.onsuccess(); }
      });
      return request;
    } } },
  });
  const storage = await import("./storage");
  const original = initial(); original.name = "Original";
  const other = initial(); other.name = "Other";
  records.set("active", { project: original });
  records.set("other", { project: other });
  storage.setTabDraftId("active");

  await t.test("a transient open error permits the next read and reuses the successful connection", async () => {
    assert.match((await storage.loadDraftResult("active")).error!, /synchronous open failure/);
    assert.match((await storage.loadDraftResult("active")).error!, /temporary open failure/);
    const recovered = await storage.loadDraftResult("active");
    assert.equal(recovered.error, undefined);
    assert.equal(recovered.project?.name, "Original");
    await storage.loadDraftResult("other");
    assert.equal(openCalls, 3);
  });
  await t.test("copying either current or other draft leaves active ID and records isolated", async () => {
    for (const source of ["active", "other"]) {
      const result = await storage.forkDraft(source);
      assert.ok(result);
      assert.equal(storage.getTabDraftId(), "active");
      assert.notEqual(result.id, source);
      assert.equal(records.get(result.id).project.name, records.get(source).project.name + " (副本)");
      const edited = structuredClone(original); edited.name = "Edited active";
      await storage.saveDraft(storage.getTabDraftId(), edited);
      assert.notEqual(records.get(result.id).project.name, "Edited active");
    }
    assert.equal((await storage.loadDraftResult(storage.getTabDraftId())).project?.name, "Edited active");
    assert.equal(records.get("other").project.name, "Other");
  });
  await t.test("failed copy leaves active identity and stored records unchanged", async () => {
    failWrite = true;
    const before = structuredClone([...records]);
    assert.equal(await storage.forkDraft("other"), null);
    assert.equal(storage.getTabDraftId(), "active");
    assert.deepEqual([...records], before);
    failWrite = false;
  });
  await t.test("timeouts and late replies leave no listeners", async () => {
    channel!.mode = "timeout";
    assert.deepEqual(await Promise.all(["a", "b", "c"].map(storage.checkDraftConflictInOtherTabs)), [false, false, false]);
    assert.equal(channel!.listeners.size, 0);
    channel!.emit("a");
    assert.equal(channel!.listeners.size, 0);
  });
  await t.test("matching replies clean listeners", async () => {
    channel!.mode = "respond";
    assert.equal(await storage.checkDraftConflictInOtherTabs("a"), true);
    assert.equal(channel!.listeners.size, 0);
  });
  await t.test("send failures reject and clean listeners", async () => {
    channel!.mode = "throw";
    await assert.rejects(storage.checkDraftConflictInOtherTabs("a"), /channel closed/);
    assert.equal(channel!.listeners.size, 0);
  });
});
