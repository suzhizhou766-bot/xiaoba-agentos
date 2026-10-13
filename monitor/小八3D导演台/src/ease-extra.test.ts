import { test } from "node:test";
import assert from "node:assert/strict";
import { easeProgress, sample, uuid, type Key, type V3 } from "./core";

const key = (time: number, x: number, extra: Partial<Key> = {}): Key => ({ id: uuid(), target: "obj", time, position: [x, 0, 0] as V3, ...extra });

test("every curve sits exactly at the half point when t=0.5", () => {
  assert.equal(easeProgress("linear", 0.5), 0.5);
  assert.equal(easeProgress("easeIn", 0.5), 0.25);
  assert.equal(easeProgress("easeOut", 0.5), 0.75);
  assert.equal(easeProgress("easeInOut", 0.5), 0.5);
});

test("easeIn(t) and easeOut(1-t) are mirrors and always sum to 1", () => {
  for (let i = 0; i <= 10; i++) {
    const t = i / 10;
    const sum = easeProgress("easeIn", t) + easeProgress("easeOut", 1 - t);
    assert.ok(Math.abs(sum - 1) < 1e-12, `easeIn(${t}) + easeOut(${1 - t}) = ${sum}`);
  }
});

test("a lone key keeps its own position at any time, whatever curve is set", () => {
  for (const ease of ["linear", "easeIn", "easeOut", "easeInOut", undefined] as const) {
    const only = [key(2, 7, ease ? { ease } : {})];
    for (const time of [0, 1, 1.9, 2, 2.5, 3, 100]) {
      assert.deepEqual(
        sample(only, "obj", time, [999, 999, 999]),
        [7, 0, 0],
        `a single ${String(ease)} key must not drift or fall back`,
      );
    }
  }
});

test("at a key's exact time, sample returns that key's position for each curve", () => {
  const keys = [key(0, 0, { ease: "linear" }), key(2, 10, { ease: "easeIn" }), key(4, 20, { ease: "easeOut" })];
  assert.deepEqual(sample(keys, "obj", 0, [999, 999, 999]), [0, 0, 0]);
  assert.deepEqual(sample(keys, "obj", 2, [999, 999, 999]), [10, 0, 0]);
  assert.deepEqual(sample(keys, "obj", 4, [999, 999, 999]), [20, 0, 0]);
});

test("keys handed over out of order sample the same as sorted keys", () => {
  const sorted = [key(0, 0, { ease: "easeIn" }), key(2, 10, { ease: "easeOut" }), key(4, 20, { ease: "linear" })];
  const shuffled = [sorted[2], sorted[0], sorted[1]];   // 乱序传入
  for (const time of [-1, 0, 0.5, 1, 2, 2.5, 3, 4, 5, 100]) {
    assert.deepEqual(
      sample(shuffled, "obj", time, [999, 999, 999]),
      sample(sorted, "obj", time, [999, 999, 999]),
    );
  }
});
