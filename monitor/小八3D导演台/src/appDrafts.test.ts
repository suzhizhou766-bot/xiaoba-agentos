import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
import { confirmDraftDeparture } from "./draftGuard";
import { initial, parseProject } from "./core";

// Execute the actual App handlers with controlled storage, without a WebGL DOM.
const source = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("App.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function handler(name: string, dependencies: Record<string, unknown>): (...args: any[]) => Promise<any> {
  let code = "";
  function visit(node: ts.Node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === name && node.initializer) code = node.initializer.getText(tree);
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) code = node.getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree);
  assert.ok(code, `App handler ${name} exists`);
  const js = ts.transpileModule(`const callback = ${code}; return callback;`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  return new Function(...Object.keys(dependencies), js)(...Object.values(dependencies));
}

for (const entry of ["restoreDraftById", "handleNewDraft", "load"]) {
  for (const scenario of ["cancel", "retry", "discard"] as const) {
    test(`${entry}: error state ${scenario} protects the actual replacement path`, async () => {
      let id = "original";
      const original = initial(); original.name = "Unsaved edits";
      const target = initial(); target.name = "Target";
      const state = { project: original, revision: 7, set() {}, resetSession(project: typeof original) { state.project = project; state.revision++; } };
      let saves = 0, confirms = 0, reads = 0;
      const deps = {
        saveStatus: "error", confirmDraftDeparture,
        async flushCurrentDraft() { saves++; return scenario === "retry"; },
        window: { confirm() { confirms++; return scenario === "discard"; } },
        setIsRestoring() {}, setBusy() {}, setSaveStatus() {}, setLastSavedAt() {},
        async loadDraftResult() { reads++; return { project: target }; },
        decode: async () => {}, prepareImage: async () => {},
        setTabDraftId(value: string) { id = value; }, getTabDraftId: () => id,
        createNewTabDraftId() { id = "new"; return id; },
        useStore: { getState: () => state }, s: state, initial, parseProject,
        async saveDraft() { return { success: true, updatedAt: 1 }; },
      };
      const fn = handler(entry, deps);
      await fn(entry === "load" ? { async text() { reads++; return JSON.stringify(target); } } : "target");
      assert.equal(saves, 1);
      assert.equal(confirms, scenario === "retry" ? 0 : 1);
      if (scenario === "cancel") {
        assert.equal(state.project, original); assert.equal(id, "original"); assert.equal(reads, 0);
      } else {
        assert.notEqual(state.project, original);
        assert.equal(id, entry === "handleNewDraft" ? "new" : entry === "restoreDraftById" ? "target" : "original");
      }
    });
  }
}

for (const changed of ["revision", "id"] as const) {
  test(`flush rejects stale success when ${changed} changes while saving`, async () => {
    let id = "original";
    const state = { project: initial(), revision: 4, set() {} };
    const statuses: string[] = [];
    let finish!: (value: { success: boolean; updatedAt: number }) => void;
    const fn = handler("flushCurrentDraft", {
      autoSaveTimerRef: { current: null }, savingRevisionRef: { current: 0 },
      getTabDraftId: () => id, useStore: { getState: () => state },
      setSaveStatus: (value: string) => statuses.push(value),
      setLastSavedAt() { throw new Error("stale write updated timestamp"); },
      saveDraft: () => new Promise(resolve => { finish = resolve; }),
    });
    const pending = fn();
    if (changed === "revision") state.revision++;
    else id = "new";
    finish({ success: true, updatedAt: 123 });
    assert.equal(await pending, false);
    assert.deepEqual(statuses, ["saving"]);
  });
}
