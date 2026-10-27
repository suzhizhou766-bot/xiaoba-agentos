import { test } from "node:test";
import assert from "node:assert/strict";
import { confirmDraftDeparture } from "./draftGuard";

for (const status of ["unsaved", "saving", "error"] as const) {
  test(`${status}: failed save and cancelled departure preserve the current project`, async () => {
    const project = { name: "Unsaved edits" };
    let current = project;
    let saves = 0;
    let confirmations = 0;
    const proceed = await confirmDraftDeparture(status,
      async () => { saves++; return false; },
      () => { confirmations++; return false; });
    if (proceed) current = { name: "Replacement" };
    assert.equal(current, project);
    assert.equal(saves, 1);
    assert.equal(confirmations, 1);
  });
  test(`${status}: successful retry allows departure without a loss warning`, async () => {
    assert.equal(await confirmDraftDeparture(status, async () => true,
      () => { throw new Error("Unexpected confirmation"); }), true);
  });
  test(`${status}: explicit confirmation allows departure after failed retry`, async () => {
    assert.equal(await confirmDraftDeparture(status, async () => false, () => true), true);
  });
}

test("saved draft can be left without another write", async () => {
  assert.equal(await confirmDraftDeparture("saved",
    async () => { throw new Error("Unexpected write"); },
    () => { throw new Error("Unexpected warning"); }), true);
});

test("departure waits for the save before asking to discard", async () => {
  let finish!: (saved: boolean) => void;
  let warned = false;
  const pending = confirmDraftDeparture("error", () => new Promise(resolve => { finish = resolve; }),
    () => { warned = true; return false; });
  assert.equal(warned, false);
  finish(false);
  assert.equal(await pending, false);
  assert.equal(warned, true);
});

test("unexpected save rejection still requires a discard decision", async () => {
  for (const discard of [false, true]) {
    assert.equal(await confirmDraftDeparture("error", async () => { throw new Error("storage offline"); }, () => discard), discard);
  }
});
