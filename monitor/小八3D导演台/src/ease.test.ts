import { test } from "node:test";
import assert from "node:assert/strict";
import { easeProgress, sample, sampleCamera, sampleTransform, parseProject, initial, copy, uuid, type Key, type V3 } from "./core";
import { useStore, active } from "./store";

const key = (time: number, x: number, extra: Partial<Key> = {}): Key => ({ id: uuid(), target: "obj", time, position: [x, 0, 0] as V3, ...extra });

test("ease curves keep endpoints, stay monotonic and bend the right way", () => {
  for (const ease of ["linear", "easeIn", "easeOut", "easeInOut", undefined] as const) {
    assert.equal(easeProgress(ease, 0), 0);
    assert.equal(easeProgress(ease, 1), 1);
    assert.equal(easeProgress(ease, -3), 0);
    assert.equal(easeProgress(ease, 7), 1);
    let last = 0;
    for (let i = 1; i <= 20; i++) {
      const v = easeProgress(ease, i / 20);
      assert.ok(v >= last, `${ease} must not go backwards`);
      last = v;
    }
  }
  assert.equal(easeProgress(undefined, 0.25), 0.25);
  assert.ok(easeProgress("easeIn", 0.25) < 0.25);
  assert.ok(easeProgress("easeOut", 0.25) > 0.25);
  assert.ok(easeProgress("easeInOut", 0.25) < 0.25 && easeProgress("easeInOut", 0.75) > 0.75);
  assert.equal(easeProgress("easeInOut", 0.5), 0.5);
});

test("a key's curve shapes only the segment that starts at it; old projects stay linear", () => {
  const linear = [key(0, 0), key(2, 10), key(4, 20)];
  assert.deepEqual(sample(linear, "obj", 0.5, [0, 0, 0]), [2.5, 0, 0]);
  const eased = [key(0, 0, { ease: "easeIn" }), key(2, 10), key(4, 20)];
  assert.deepEqual(sample(eased, "obj", 0.5, [0, 0, 0]), [10 * 0.25 * 0.25, 0, 0]);
  assert.deepEqual(sample(eased, "obj", 2, [0, 0, 0]), [10, 0, 0]);
  assert.deepEqual(sample(eased, "obj", 3, [0, 0, 0]), [15, 0, 0]);   // 第二段没设曲线：匀速
  assert.deepEqual(sample(eased, "obj", 9, [0, 0, 0]), [20, 0, 0]);
});

test("camera values and object rotation/scale follow the same curve as position", () => {
  const base = copy(initial().shots[0].camera);
  const cam = (time: number, focal: number, ease?: Key["ease"]): Key => ({
    id: uuid(), target: "camera", time, position: [time, 0, 0] as V3, camera: { ...copy(base), position: [time, 0, 0], focalLength: focal }, ...(ease ? { ease } : {}),
  });
  const mid = sampleCamera([cam(0, 20, "easeOut"), cam(4, 60)], 1, base);
  const p = easeProgress("easeOut", 0.25);
  assert.ok(Math.abs(mid.focalLength - (20 + 40 * p)) < 1e-9);
  assert.ok(Math.abs(mid.position[0] - 4 * p) < 1e-9);
  const object = { rotation: [0, 0, 0] as V3, scale: 1 } as never;
  const spun = sampleTransform([key(0, 0, { rotation: [0, 0, 0], scale: 1, ease: "easeInOut" }), key(2, 0, { rotation: [0, 2, 0], scale: 3 })], "obj", 0.5, object);
  const q = easeProgress("easeInOut", 0.25);
  assert.ok(Math.abs(spun.rotation[1] - 2 * q) < 1e-9 && Math.abs(spun.scale - (1 + 2 * q)) < 1e-9);
});

test("curve survives save/load and re-keying, and bad values are rejected", () => {
  useStore.getState().set({ project: initial(), time: 0 });
  const id = active(useStore.getState().project).objects[0].id;
  useStore.getState().seek(0);
  useStore.getState().key(id);
  useStore.getState().edit((sh) => { sh.keys.find((k) => k.target === id)!.ease = "easeInOut"; });
  useStore.getState().key(id);          // 同一时刻重新打帧
  const project = useStore.getState().project;
  assert.equal(active(project).keys.find((k) => k.target === id)!.ease, "easeInOut");
  const round = parseProject(JSON.parse(JSON.stringify(project)));
  assert.equal(round.shots[0].keys.find((k) => k.target === id)!.ease, "easeInOut");
  const broken = JSON.parse(JSON.stringify(project));
  broken.shots[0].keys[0].ease = "bounce";
  assert.throws(() => parseProject(broken));
});
