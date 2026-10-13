import { handoffTextFiles, cleanProductionPrompt } from "./handoff";
import { macroCameraMovement } from "./spatial";
import { zipSync, unzipSync, strToU8, strFromU8 } from "fflate";
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  initial,
  parseProject,
  sample,
  frameCamera,
  compile,
  frameDescription,
  copy,
  getAxisHint,
  defaultLighting,
  lightingPresets,
  describeLighting,
  lightingToCartesian,
  azimuthToDirectionLabel,
  type Key,
} from "./core";
import { useStore, active } from "./store";
test("24fps position tracks interpolate and hold at boundaries", () => {
  const keys: Key[] = [
    { id: "1", target: "a", time: 1, position: [0, 0, 0] },
    { id: "2", target: "a", time: 3, position: [4, 2, 0] },
  ];
  assert.deepEqual(sample(keys, "a", 2, [9, 9, 9]), [2, 1, 0]);
  assert.deepEqual(sample(keys, "a", 0, [9, 9, 9]), [0, 0, 0]);
  assert.deepEqual(sample(keys, "a", 5, [9, 9, 9]), [4, 2, 0]);
  assert.deepEqual(sample(keys, "b", 2, [9, 9, 9]), [9, 9, 9]);
});
test("project round trip preserves shots, assets and immutable object IDs", () => {
  const p = initial();
  p.assets.push({ id: "asset", name: "test.gltf", format: "gltf", data: "{}" });
  const result = parseProject(JSON.parse(JSON.stringify(p)));
  assert.deepEqual(result, p);
  result.shots[0].objects[0].name = "changed";
  assert.notEqual(result.shots[0].objects[0].name, p.shots[0].objects[0].name);
});
test("reject malformed projects before state mutation", () => {
  const p = initial();
  p.shots[0].camera.position = [NaN, 0, 0];
  assert.throws(() => parseProject(p));
  const q = initial();
  q.shots[0].objects.push(copy(q.shots[0].objects[0]));
  assert.throws(() => parseProject(q));
  const r = initial();
  r.shots[0].keys.push({
    id: "key",
    target: "missing",
    time: 1,
    position: [0, 0, 0],
  });
  assert.throws(() => parseProject(r));
});
test("framing responds to subject scale and focal length", () => {
  const shot = initial().shots[0],
    o = shot.objects[0];
  const a = frameCamera(shot.camera, o, "FS"),
    b = frameCamera(shot.camera, { ...o, scale: 2 }, "FS"),
    c = frameCamera({ ...shot.camera, focalLength: 70 }, o, "FS");
  assert.ok(Math.abs(b.focusDistance / a.focusDistance - 2) < 1e-6);
  assert.ok(c.focusDistance > a.focusDistance);
  assert.ok(
    frameCamera(shot.camera, o, "ELS").focusDistance >
      frameCamera(shot.camera, o, "CU").focusDistance,
  );
});
test("key overwrite, seek and independent shots", () => {
  useStore.getState().set({ project: initial(), time: 0 });
  const s = useStore.getState();
  s.key("camera");
  s.seek(2);
  s.camera({ position: [10, 1, 4] });
  s.key("camera");
  s.camera({ position: [12, 1, 4] });
  s.key("camera");
  assert.equal(active(useStore.getState().project).keys.length, 2);
  s.seek(1);
  assert.equal(active(useStore.getState().project).camera.position[0], 7.5);
  s.addShot();
  const before = copy(useStore.getState().project.shots[0]);
  s.camera({ focalLength: 85 });
  assert.deepEqual(useStore.getState().project.shots[0], before);
});
test("compiler includes all director responsibility sections", () => {
  const p = initial();
  const output = compile(p, p.shots[0]);
  for (const key of [
    "素材职责",
    "时间轴",
    "摄影机",
    "动作",
    "连续性",
    "不变量",
  ])
    assert.ok(output.text.includes(key));
  assert.ok(output.text.includes("请在导出前勾选视频模型"));
  assert.ok(output.text.includes("不是最终生成图"));
});

test("handoff samples marked endpoints rather than current playhead", () => {
  const p = initial(),
    s = p.shots[0];
  s.in = 2;
  s.out = 6;
  s.keys = [
    { id: "a", target: "camera", time: 0, position: [0, 1, 8] },
    { id: "b", target: "camera", time: 8, position: [0, 1, 4] },
  ];
  const d = compile(p, s).json;
  assert.deepEqual(d.首帧反推.摄影机位置米, [0, 1, 7]);
  assert.deepEqual(d.尾帧反推.摄影机位置米, [0, 1, 5]);
  assert.equal(d.运镜与调度采样[0].时间秒, 2);
  s.objects[0].visible = false;
  assert.equal(frameDescription(s, 2).主体构图.length, 1);
  s.visualBrief = "夕阳下的写实庭院";
  assert.equal(compile(p, s).json.用户视觉说明, s.visualBrief);
  assert.equal(
    parseProject(JSON.parse(JSON.stringify(p))).shots[0].visualBrief,
    s.visualBrief,
  );
});

test("model selection persists per shot and rejects unknown profiles", () => {
  const p = initial();
  p.shots[0].videoModel = "omni-flash-1.1";
  useStore.getState().set({ project: p });
  useStore.getState().addShot();
  useStore.getState().edit((s) => (s.videoModel = "seedance-2.5"));
  const restored = parseProject(
    JSON.parse(JSON.stringify(useStore.getState().project)),
  );
  assert.equal(restored.shots[0].videoModel, "omni-flash-1.1");
  assert.equal(restored.shots[1].videoModel, "seedance-2.5");
  const invalid = JSON.parse(JSON.stringify(restored));
  invalid.shots[0].videoModel = "fake";
  assert.throws(() => parseProject(invalid), /视频模型/);
  const legacy = initial();
  assert.equal(parseProject(legacy).shots[0].videoModel, undefined);
  assert.throws(
    () => handoffTextFiles(legacy, legacy.shots[0]),
    /选择视频模型/,
  );
});
test("each ZIP carries matching model, guide, sources and assistant task", () => {
  for (const id of ["omni-flash-1.1", "seedance-2.5"] as const) {
    const p = initial(),
      s = p.shots[0];
    s.videoModel = id;
    s.in = 2;
    s.out = 6;
    const files = handoffTextFiles(p, s);
    const zip = zipSync(
      Object.fromEntries(
        Object.entries(files).map(([k, v]) => [k, strToU8(v)]),
      ),
    );
    const result = unzipSync(zip);
    const doc = JSON.parse(strFromU8(result["handoff.json"]));
    assert.equal(doc.视频模型.id, id);
    assert.equal(JSON.parse(strFromU8(result["model-profile.json"])).id, id);
    assert.ok(strFromU8(result["model-guide.md"]).includes(doc.视频模型.名称));
    assert.ok(strFromU8(result["READ-ME.txt"]).includes("不要再次要求"));
    assert.ok(
      strFromU8(result["prompt-writing-task.txt"]).includes("时间归零"),
    );
    assert.ok(
      JSON.parse(strFromU8(result["sources.json"])).sources.every(
        (s: { url: string }) => s.url.startsWith("https://"),
      ),
    );
    assert.ok(!files["handoff.txt"].includes("待用户在交接后指定"));
    assert.ok(
      files["model-guide.md"].includes(
        id === "omni-flash-1.1" ? "<FIRST_FRAME>" : "素材职责",
      ),
    );
  }
});

test("camera keys interpolate aim and optics with position, and round trip", () => {
  const p = initial(),
    shot = p.shots[0];
  useStore.getState().set({ project: p, time: 0, autoKey: false });
  const s = useStore.getState();
  s.key("camera");
  s.seek(2);
  s.camera({ position: [-3, 1.6, 6], target: [0.5, 1, 0], focalLength: 55 });
  s.key("camera");
  s.seek(1);
  const middle = active(useStore.getState().project);
  assert.deepEqual(middle.camera.position, [0, 1.6, 6]);
  assert.deepEqual(middle.camera.target, [0.25, 1, 0]);
  assert.equal(middle.camera.focalLength, 45);
  const restored = parseProject(
    JSON.parse(JSON.stringify(useStore.getState().project)),
  );
  assert.equal(restored.shots[0].keys[1].camera?.focalLength, 55);
  const output = compile(restored, restored.shots[0]);
  assert.equal(output.json.时间轴.关键帧[1].摄影机状态?.focalLength, 55);
});
test("frustum corners project to the exact camera frame and shrink with focal length", async () => {
  const { viewCone, applyCamera } = await import("./core");
  const { PerspectiveCamera, Vector3 } = await import("three");
  const s = initial().shots[0],
    cam = applyCamera(new PerspectiveCamera(), s.camera, s.objects);
  const cone = viewCone(s.camera, s.objects);
  for (const point of cone.corners) {
    const projected = new Vector3(...point).project(cam);
    assert.ok(Math.abs(Math.abs(projected.x) - 1) < 1e-9);
    assert.ok(Math.abs(Math.abs(projected.y) - 1) < 1e-9);
  }
  const narrow = viewCone({ ...s.camera, focalLength: 70 }, s.objects);
  const width = (p: typeof cone) =>
    new Vector3(...p.corners[0]).distanceTo(new Vector3(...p.corners[1]));
  assert.ok(Math.abs(width(narrow) / width(cone) - 0.5) < 1e-9);
});
test("auto-key overwrites at the same frame and lock prevents object edits", () => {
  useStore.getState().set({ project: initial(), time: 0, autoKey: true });
  const s = useStore.getState();
  s.camera({ focalLength: 50 });
  s.camera({ focalLength: 60 });
  assert.equal(active(useStore.getState().project).keys.length, 1);
  s.seek(2);
  s.camera({ position: [2, 2, 4] });
  assert.equal(active(useStore.getState().project).keys.length, 2);
  const o = active(useStore.getState().project).objects[0];
  s.edit((sh) => (sh.objects[0].locked = true));
  s.object(o.id, { position: [99, 99, 99] });
  assert.deepEqual(
    active(useStore.getState().project).objects[0].position,
    o.position,
  );
  s.set({ selected: o.id });
  s.remove();
  assert.equal(active(useStore.getState().project).objects.length, 2);
  s.set({ autoKey: false });
});
test("track sampling is deterministic for arbitrary scrub order and preserves asset identity", () => {
  const p = initial();
  p.assets = [{ id: "asset", name: "fixture", data: "{}", format: "gltf" }];
  useStore.getState().set({ project: p, time: 0, autoKey: false });
  const s = useStore.getState();
  s.key("camera");
  s.seek(2);
  s.camera({ focalLength: 80, position: [9, 3, 4], target: [1, 2, 3] });
  s.key("camera");
  s.seek(1);
  const expected = copy(active(useStore.getState().project).camera);
  const assets = useStore.getState().project.assets;
  for (const t of [1.75, 0.25, 2, 0, 1]) s.seek(t);
  assert.deepEqual(active(useStore.getState().project).camera, expected);
  assert.equal(useStore.getState().project.assets, assets);
});

test("compiler camera summary samples in mark instead of current playhead", () => {
  const p = initial(), shot = p.shots[0];
  const start = copy(shot.camera), end = {...copy(start), focalLength: 75, position: [-3,2,6] as [number,number,number]};
  shot.keys = [{id:'start',target:'camera',time:0,position:start.position,camera:start},{id:'end',target:'camera',time:4,position:end.position,camera:end}];
  shot.in=2;shot.out=4;
  shot.camera={...end,focalLength:99};
  const result=compile(p,shot).json;
  assert.equal(result.摄影机.焦距毫米,55);
  assert.deepEqual(result.摄影机.机位米,[0,1.8,6]);
  assert.equal(result.摄影机.状态时间秒,2);
});

test('scene images survive project round trip, shot copy and role compilation', () => {
  const p=initial(), shot=p.shots[0];
  p.sceneImages=[{id:'image-1',name:'场景.png',data:'data:image/png;base64,aGVsbG8=',width:1920,height:1080}];
  shot.sceneImageIds=['image-1'];shot.backgroundImageId='image-1';
  const restored=parseProject(JSON.parse(JSON.stringify(p)));
  assert.deepEqual(restored.sceneImages,p.sceneImages);
  assert.equal(compile(restored,restored.shots[0]).json.场景图片[0].用途,'场景外观参考 + 世界空间有限背景板');
  useStore.setState({project:restored,time:0});useStore.getState().addShot();
  assert.equal(active(useStore.getState().project).backgroundImageId,'image-1');
  const bad=copy(p);bad.shots[0].backgroundImageId='missing';assert.throws(()=>parseProject(bad));
  const remote=copy(p);remote.sceneImages![0].data='https://example.com/image.jpg';assert.throws(()=>parseProject(remote));
});

test('physical perspective makes a fixed-size actor smaller with depth', async()=>{
  const {spatialFrame}=await import('./spatial');const p=initial(),s=p.shots[0],o=s.objects[0];
  s.camera.position=[0,1,6];s.camera.target=[0,1,0];o.position=[0,0,0];
  s.keys=[{id:'near',target:o.id,time:0,position:[0,0,0]},{id:'far',target:o.id,time:4,position:[0,0,-8]}];
  const near=spatialFrame(s,0).subjects[0],far=spatialFrame(s,4).subjects[0];
  assert.ok(far.heightPercent!<near.heightPercent!/2);assert.equal(o.scale,1);
});
test('finite scene board stays in world while camera pans outside', async()=>{
  const {boardCoverage,defaultBoard}=await import('./spatial');const s=initial().shots[0];
  s.backgroundImageId='board';s.sceneBoard={...defaultBoard(),position:[0,1,-6],width:20,height:12};
  s.camera.position=[0,1,6];s.camera.target=[0,1,0];assert.equal(boardCoverage(s)?.covered,true);
  s.camera.target=[20,1,0];assert.equal(boardCoverage(s)?.covered,false);
  assert.deepEqual(s.sceneBoard.position,[0,1,-6]);
});
test('object transform keys interpolate and persist without changing body size by depth',async()=>{
  const {sampledShot}=await import('./core');const p=initial(),o=p.shots[0].objects[0];
  useStore.setState({project:p,time:0});useStore.getState().key(o.id);useStore.getState().seek(4);
  useStore.getState().object(o.id,{position:[0,0,-8],rotation:[0,Math.PI,0],scale:2});useStore.getState().key(o.id);
  const saved=parseProject(JSON.parse(JSON.stringify(useStore.getState().project)));
  const mid=sampledShot(saved.shots[0],2).objects[0];assert.equal(mid.rotation[1],Math.PI/2);assert.equal(mid.scale,1.5);
});
test('framing reports offscreen B without hiding B in the scene',async()=>{
  const {spatialFrame}=await import('./spatial');const s=initial().shots[0];s.objects[0].position=[0,0,0];s.objects[1].position=[5,0,0];
  s.camera.position=[0,1.5,2];s.camera.target=[0,1.5,0];s.camera.focalLength=85;
  const frame=spatialFrame(s,0);assert.equal(frame.subjects[1].screen,'画外');assert.equal(s.objects[1].visible,true);
});

test('3 second edit exports 3 seconds unless a hold is explicitly selected',async()=>{
 const {exportShot,exportTimes}=await import('./exportPlan');const p=initial(),s=p.shots[0],o=s.objects[0];
 s.keys=[{id:'a',target:o.id,time:0,position:o.position,scale:1},{id:'b',target:o.id,time:3,position:[0,0,-4],scale:1.31}];
 const e=exportShot(s);assert.equal(e.out,3);assert.deepEqual(exportTimes(e),[.75,1.5,2.25]);assert.equal(e.keys[1].scale,1);assert.equal(s.keys[1].scale,1.31);
 assert.equal(exportShot({...s,exportRange:'manual'}).out,30);assert.equal(exportShot({...s,scaleMotion:true}).keys[1].scale,1.31);
 s.videoModel='seedance-2.5';const files=handoffTextFiles(p,s);
 assert.equal(JSON.parse(files['handoff.json']).尾帧反推.时间秒,3);
 assert.equal(JSON.parse(files['project.xbdirector.json']).shots[0].out,3);
 assert.equal(JSON.parse(files['source-project.xbdirector.json']).shots[0].out,30);
 assert.match(files['video-prompt.zh.txt'],/3.000 秒/);
});

test('scene board pitch and horizonRatio survive round trip and influence coverage', async () => {
  const { boardCoverage, defaultBoard } = await import('./spatial');
  const p = initial(), s = p.shots[0];
  p.sceneImages = [{ id: 'board-img', name: 'board.png', data: 'data:image/png;base64,aGVsbG8=', width: 1920, height: 1080 }];
  s.sceneImageIds = ['board-img'];
  s.backgroundImageId = 'board-img';
  s.sceneBoard = {
    ...defaultBoard(),
    position: [0, 3, -6],
    width: 12,
    height: 6.75,
    yaw: 0,
    pitch: 0.1,
    horizonRatio: 0.65,
    confirmed: true,
  };
  const restored = parseProject(JSON.parse(JSON.stringify(p)));
  assert.equal(restored.shots[0].sceneBoard?.pitch, 0.1);
  assert.equal(restored.shots[0].sceneBoard?.horizonRatio, 0.65);
  assert.equal(restored.shots[0].sceneBoard?.confirmed, true);

  // Invalid horizonRatio rejected
  const bad = copy(p);
  bad.shots[0].sceneBoard!.horizonRatio = 1.5;
  assert.throws(() => parseProject(bad));
});

test('directional overflow detects left, right, top and bottom frustum spills', async () => {
  const { boardCoverage, defaultBoard } = await import('./spatial');
  const s = initial().shots[0];
  s.backgroundImageId = 'board';
  s.sceneBoard = { ...defaultBoard(), position: [0, 1.6, -5], width: 10, height: 6, yaw: 0, pitch: 0, confirmed: true };
  s.camera.position = [0, 1.6, 2];
  s.camera.target = [0, 1.6, 0];
  s.camera.focalLength = 35;

  const centerCov = boardCoverage(s);
  assert.ok(centerCov?.covered);
  assert.equal(centerCov?.overflow.left, false);
  assert.equal(centerCov?.overflow.right, false);

  // Pan camera to the right -> right side spills
  s.camera.target = [10, 1.6, 0];
  const panRightCov = boardCoverage(s);
  assert.equal(panRightCov?.covered, false);
  assert.ok(panRightCov?.overflow.right);

  // Pan camera to the left -> left side spills
  s.camera.target = [-10, 1.6, 0];
  const panLeftCov = boardCoverage(s);
  assert.equal(panLeftCov?.covered, false);
  assert.ok(panLeftCov?.overflow.left);

  // Tilt camera up -> top side spills
  s.camera.target = [0, 10, 0];
  const tiltUpCov = boardCoverage(s);
  assert.equal(tiltUpCov?.covered, false);
  assert.ok(tiltUpCov?.overflow.top);
});

test('object bottom grounding accurately places actors and props on arbitrary surface heights', async () => {
  const { objectBottomY, groundedPositionY } = await import('./spatial');
  const { actor } = await import('./core');
  const a = actor('主角', [0, 5, 0], '#fff');
  assert.ok(Math.abs(objectBottomY(a) - 5) < 1e-4);

  // Ground to Y=0
  const groundedY = groundedPositionY(a, 0);
  assert.ok(Math.abs(groundedY - 0) < 1e-4);
  a.position[1] = groundedY;
  assert.ok(Math.abs(objectBottomY(a) - 0) < 1e-4);

  // Prop with height 3, center [0, 1.5, 0], currently at position [0, 10, 0]
  const prop = { ...actor('箱子', [0, 10, 0], '#637d86'), kind: 'prop' as const, size: [1, 3, 0.4] as [number,number,number], center: [0, 1.5, 0] as [number,number,number] };
  assert.ok(Math.abs(objectBottomY(prop) - 10) < 1e-4);

  // Snap actor onto top of table at Y = 0.8
  const tableTopY = 0.8;
  const onTableY = groundedPositionY(a, tableTopY);
  a.position[1] = onTableY;
  assert.ok(Math.abs(objectBottomY(a) - tableTopY) < 1e-4);
});

test('macro camera synthesizer and clean production prompts for Seedance and Omni', async () => {
  const { macroCameraMovement } = await import('./spatial');
  const { cleanProductionPrompt, videoPrompt } = await import('./handoff');
  const p = initial(), s = p.shots[0];
  s.camera.position = [0, 1.6, 6];
  s.camera.target = [0, 1.6, 0];
  s.keys = [
    { id: 'k1', target: 'camera', time: 0, position: [0, 1.6, 6], camera: { ...s.camera, position: [0, 1.6, 6] } },
    { id: 'k2', target: 'camera', time: 3, position: [0, 1.6, 2], camera: { ...s.camera, position: [0, 1.6, 2] } },
  ];
  s.in = 0; s.out = 3;
  const macro = macroCameraMovement(s);
  assert.match(macro.movement, /向前推进（Dolly In）/);

  // Clean prompt for Seedance 2.5
  s.videoModel = 'seedance-2.5';
  const seedanceClean = cleanProductionPrompt(p, s);
  assert.match(seedanceClean, /【Seedance 2.5 成品提示词/);
  assert.match(seedanceClean, /@Image1 为起始构图/);
  assert.match(seedanceClean, /Dolly In/);

  // Clean prompt for Omni
  s.videoModel = 'omni-flash-1.1';
  const omniClean = cleanProductionPrompt(p, s);
  assert.match(omniClean, /【Gemini Omni 1.1 Flash 成品提示词/);
  assert.match(omniClean, /<FIRST_FRAME> to <LAST_FRAME>/);

  // Full videoPrompt separates production prompt from audit
  const full = videoPrompt(p, s);
  assert.ok(full.includes('【Gemini Omni 1.1 Flash 成品提示词'));
  assert.ok(full.includes('【导演台安全与空间审计'));
});

test("undo and redo history allows stepping backward and forward across edits and keyframes", () => {
  const store = useStore.getState();
  store.clearHistory();
  store.set({ project: initial(), time: 0, autoKey: false });
  // Loading the fixture is setup; public project replacements now enter history.
  store.clearHistory();

  const initialShot = active(useStore.getState().project);
  const initialFocal = initialShot.camera.focalLength;
  assert.equal(useStore.getState().canUndo, false);
  assert.equal(useStore.getState().canRedo, false);

  // 1. Discrete edit: change camera and commit
  store.camera({ focalLength: 85 });
  store.commitHistory();
  assert.equal(active(useStore.getState().project).camera.focalLength, 85);
  assert.equal(useStore.getState().canUndo, true);
  assert.equal(useStore.getState().canRedo, false);

  // Undo
  useStore.getState().undo();
  assert.equal(active(useStore.getState().project).camera.focalLength, initialFocal);
  assert.equal(useStore.getState().canUndo, false);
  assert.equal(useStore.getState().canRedo, true);

  // Redo
  useStore.getState().redo();
  assert.equal(active(useStore.getState().project).camera.focalLength, 85);
  assert.equal(useStore.getState().canUndo, true);
  assert.equal(useStore.getState().canRedo, false);

  // 2. Keyframe recording and undo
  store.key("camera"); // key at 0s
  assert.equal(active(useStore.getState().project).keys.length, 1);
  store.seek(2);
  store.camera({ position: [5, 2, 8] });
  store.key("camera"); // key at 2s
  assert.equal(active(useStore.getState().project).keys.length, 2);

  // Undo key at 2s
  useStore.getState().undo();
  assert.equal(active(useStore.getState().project).keys.length, 1);
  assert.equal(useStore.getState().canRedo, true);

  // Redo key at 2s
  useStore.getState().redo();
  assert.equal(active(useStore.getState().project).keys.length, 2);
  assert.deepEqual(active(useStore.getState().project).keys[1].position, [5, 2, 8]);

  // 3. Clear history resets canUndo and canRedo
  useStore.getState().clearHistory();
  assert.equal(useStore.getState().canUndo, false);
  assert.equal(useStore.getState().canRedo, false);
});

test("a fresh edit immediately invalidates redo before the debounce commits", () => {
  const store = useStore.getState();
  store.set({project: initial(), time: 0, autoKey: false});
  store.clearHistory();
  store.camera({focalLength: 50});
  store.commitHistory();
  store.undo();
  assert.equal(useStore.getState().canRedo, true);
  store.camera({focalLength: 70});
  assert.equal(useStore.getState().canRedo, false);
  store.redo();
  assert.equal(active(useStore.getState().project).camera.focalLength, 70);
  store.undo();
  assert.equal(active(useStore.getState().project).camera.focalLength, 35);
  store.redo();
  assert.equal(active(useStore.getState().project).camera.focalLength, 70);
  store.clearHistory();
});

test("project replacement and rename can be undone with media references intact", () => {
  const store = useStore.getState();
  const original = initial();
  original.assets.push({id: "history-asset", name: "fixture.gltf", format: "gltf", data: "{}"});
  store.set({project: original, time: 0, autoKey: false});
  store.clearHistory();
  store.set({project: {...original, name: "改名项目"}});
  store.undo();
  assert.equal(useStore.getState().project.name, original.name);
  assert.equal(useStore.getState().project.assets[0], original.assets[0]);
  store.redo();
  assert.equal(useStore.getState().project.name, "改名项目");
  store.set({project: initial()});
  store.undo();
  assert.equal(useStore.getState().project.name, "改名项目");
  assert.equal(useStore.getState().project.assets[0].data, "{}");
  store.clearHistory();
});

test("content revision changes with edits and history, not timeline sampling", () => {
  const store = useStore.getState();
  store.set({project: initial(), time: 0, autoKey: false});
  store.clearHistory();
  const before = useStore.getState().revision;
  store.seek(2);
  assert.equal(useStore.getState().revision, before);
  store.camera({focalLength: 85});
  assert.equal(useStore.getState().revision, before + 1);
  store.undo();
  assert.equal(useStore.getState().revision, before + 2);
  store.redo();
  assert.equal(useStore.getState().revision, before + 3);
  store.clearHistory();
});

test("camera axis hover hints identify horizontal, vertical and dolly movements", () => {
  // 1. Camera translation mode: X = 水平移动, Y = 俯仰移动, Z = 前后移动
  const hintX = getAxisHint("X", "translate", "camera");
  assert(hintX);
  assert.equal(hintX.axis, "X");
  assert(hintX.title.includes("水平移动"));
  assert(hintX.desc.includes("左右平移"));

  const hintY = getAxisHint("Y", "translate", "camera");
  assert(hintY);
  assert.equal(hintY.axis, "Y");
  assert(hintY.title.includes("俯仰移动"));
  assert(hintY.desc.includes("垂直上下升降"));

  const hintZ = getAxisHint("Z", "translate", "camera");
  assert(hintZ);
  assert.equal(hintZ.axis, "Z");
  assert(hintZ.title.includes("前后移动"));
  assert(hintZ.desc.includes("前后推进或拉远"));

  // 2. Camera rotation mode: X = 俯仰旋转, Y = 水平摇镜, Z = 画面滚转
  const rotX = getAxisHint("X", "rotate", "camera");
  assert(rotX);
  assert(rotX.title.includes("俯仰旋转"));

  const rotY = getAxisHint("Y", "rotate", "camera");
  assert(rotY);
  assert(rotY.title.includes("水平摇镜"));

  const rotZ = getAxisHint("Z", "rotate", "camera");
  assert(rotZ);
  assert(rotZ.title.includes("画面滚转"));

  // 3. Camera target mode
  const targetX = getAxisHint("X", "translate", "camera-target");
  assert(targetX);
  assert(targetX.title.includes("目标点水平移动"));

  // 4. Null or missing axis
  assert.equal(getAxisHint(null, "translate", "camera"), null);
  assert.equal(getAxisHint(undefined, "translate", "camera"), null);

  // 5. Store integration
  const store = useStore.getState();
  store.setAxisHint(hintX);
  assert.deepEqual(useStore.getState().axisHint, hintX);
  assert(useStore.getState().notice.includes("水平移动"));

  store.setAxisHint(null);
  assert.equal(useStore.getState().axisHint, null);
});

test("builtin 3D preset models validate, decode and integrate with project assets", async () => {
  const { presetModels, getPresetModel } = await import("./builtinModels");
  const { decode, models } = await import("./assets");
  const { addPresetModelToStage } = await import("./PropsLibraryModal");

  // 1. Verify catalog completeness
  assert.equal(presetModels.length, 14);
  const categories = new Set(presetModels.map((m) => m.category));
  assert(categories.has("blocking"));
  assert(categories.has("filming_gear"));
  assert(categories.has("lighting"));
  assert(categories.has("narrative"));
  assert(categories.has("architecture"));

  for (const m of presetModels) {
    assert(m.id && m.name && m.description);
    assert.equal(m.size.length, 3);
    assert(m.size.every((v) => v > 0), `${m.name} size must be positive`);
    assert(m.dataUri.startsWith("data:model/gltf-binary;base64,"));
  }

  // 2. Lookups
  const chair = getPresetModel("director_chair");
  assert(chair);
  assert.equal(chair.name, "经典导演椅");

  const dolly = getPresetModel("camera_dolly_track");
  assert(dolly);
  assert.equal(dolly.name, "摄影轨道与移动推车");

  // 3. Decode model to Three.js scene
  const scene = await decode({
    id: chair.id,
    name: "director_chair.glb",
    data: chair.dataUri,
    format: "glb",
  });
  assert(scene);
  assert(models.has(chair.id));

  // 4. Add model to active project stage
  const store = useStore.getState();
  const initialObjCount = active(store.project).objects.length;
  await addPresetModelToStage(chair, store);

  const updatedShot = active(useStore.getState().project);
  assert.equal(updatedShot.objects.length, initialObjCount + 1);

  const addedObj = updatedShot.objects[updatedShot.objects.length - 1];
  assert.equal(addedObj.kind, "model");
  assert.equal(addedObj.name, "经典导演椅");
  assert(addedObj.assetId);

  // 5. Verify asset exists in project.assets
  const asset = useStore.getState().project.assets.find((a) => a.id === addedObj.assetId);
  assert(asset);
  assert.equal(asset.format, "glb");

  // 6. Verify project round trip and validation
  const parsed = parseProject(JSON.parse(JSON.stringify(useStore.getState().project)));
  assert.equal(parsed.shots.length, useStore.getState().project.shots.length);
  assert(parsed.assets.some((a) => a.id === addedObj.assetId));
});

test("lighting spherical coordinate transformation and direction labels", () => {
  assert.equal(azimuthToDirectionLabel(0).zh, "正前方");
  assert.equal(azimuthToDirectionLabel(45).zh, "右前侧 45°");
  assert.equal(azimuthToDirectionLabel(90).zh, "正右侧 90°");
  assert.equal(azimuthToDirectionLabel(180).zh, "正后方逆光 180°");
  assert.equal(azimuthToDirectionLabel(270).zh, "正左侧 270°");
  assert.equal(azimuthToDirectionLabel(315).zh, "左前侧 315°");

  const posTop = lightingToCartesian(0, 90, 10, 0);
  assert(Math.abs(posTop[1] - 10) < 1e-4);
  assert(Math.abs(posTop[0]) < 1e-4);
  assert(Math.abs(posTop[2]) < 1e-4);

  const posFront = lightingToCartesian(0, 0, 10, 0);
  assert(Math.abs(posFront[1]) < 1e-4);
  assert(Math.abs(posFront[2] - 10) < 1e-4);
});

test("lighting presets and prompt synthesizer integration", () => {
  const presetKeys = Object.keys(lightingPresets) as (keyof typeof lightingPresets)[];
  assert(presetKeys.includes("classic-3-point"));
  assert(presetKeys.includes("rembrandt"));
  assert(presetKeys.includes("film-noir"));
  assert(presetKeys.includes("soft-window"));
  assert(presetKeys.includes("golden-hour"));
  assert(presetKeys.includes("cool-night"));

  for (const key of presetKeys) {
    const desc = describeLighting({ preset: key, ...lightingPresets[key].config });
    assert(desc.presetName.length > 0);
    assert(desc.detailZh.includes("光"));
    assert(desc.detailEn.length > 0);
  }

  // Test prompt generation integration in cleanProductionPrompt
  const p = initial();
  const shot = active(p);
  shot.lighting = {
    preset: "rembrandt",
    ...lightingPresets["rembrandt"].config,
  };

  // Seedance 2.5
  shot.videoModel = "seedance-2.5";
  const seedancePrompt = cleanProductionPrompt(p, shot);
  assert(seedancePrompt.includes("[光影与布光]"));
  assert(seedancePrompt.includes("伦勃朗"));

  // Gemini Omni 1.1 Flash
  shot.videoModel = "omni-flash-1.1";
  const omniPrompt = cleanProductionPrompt(p, shot);
  assert(omniPrompt.includes("Lighting & Atmosphere:"));
  assert(omniPrompt.includes("rembrandt"));
});

test("lighting round trip persistence and backward compatibility in parseProject", () => {
  const p = initial();
  const shot = active(p);
  shot.lighting = {
    preset: "custom",
    keyAzimuth: 123,
    keyElevation: 45,
    keyDistance: 9,
    keyIntensity: 2.2,
    keyColor: "#ffaa00",
    fillEnabled: true,
    fillAzimuth: 240,
    fillElevation: 30,
    fillIntensity: 0.8,
    fillColor: "#00aaff",
    rimEnabled: false,
    rimAzimuth: 180,
    rimElevation: 60,
    rimIntensity: 1.5,
    rimColor: "#ffffff",
    ambientIntensity: 0.15,
    ambientColor: "#112233",
    groundBounceIntensity: 0.4,
  };

  const serialized = JSON.parse(JSON.stringify(p));
  const parsed = parseProject(serialized);
  const parsedShot = parsed.shots.find((s) => s.id === shot.id);
  assert(parsedShot?.lighting);
  assert.equal(parsedShot.lighting.preset, "custom");
  assert.equal(parsedShot.lighting.keyAzimuth, 123);
  assert.equal(parsedShot.lighting.keyColor, "#ffaa00");
  assert.equal(parsedShot.lighting.fillEnabled, true);
  assert.equal(parsedShot.lighting.rimEnabled, false);
});

test("bilingual cinematic camera movement and multi-workflow prompt synthesis", async () => {
  const { heightPercentToShotScale, macroCameraMovement } = await import("./spatial");
  assert.equal(heightPercentToShotScale(15).zh, "全景/远景");
  assert.equal(heightPercentToShotScale(35).zh, "中景");
  assert.equal(heightPercentToShotScale(55).zh, "中近景");
  assert.equal(heightPercentToShotScale(80).zh, "近景特写");
  assert.equal(heightPercentToShotScale(null).zh, "画外");

  assert.equal(heightPercentToShotScale(35).en, "medium shot");
  assert.equal(heightPercentToShotScale(55).en, "medium close-up");

  const p = initial();
  const shot = active(p);
  const macro = macroCameraMovement(shot);
  assert(macro.movementEn.length > 0);
  assert(macro.cinematicSubjectsZh.length > 0);
  assert(macro.cinematicSubjectsEn.length > 0);

  // Test Omni 1.1 Flash multi-workflow prompt
  shot.videoModel = "omni-flash-1.1";
  const omniPrompt = cleanProductionPrompt(p, shot);
  assert(omniPrompt.includes("Workflow 1: First & Last Frame Interpolation"));
  assert(omniPrompt.includes("Workflow 2: Single Image / Multi-Reference to Video"));
  assert(omniPrompt.includes("Workflow 3: Camera Motion Video Guided"));
  assert(omniPrompt.includes("<FIRST_FRAME>"));
  assert(omniPrompt.includes("<LAST_FRAME>"));
  assert(omniPrompt.includes("<IMAGE_REF_0>"));
  assert(omniPrompt.includes("camera-motion.mp4"));

  // Test Seedance 2.5 multi-workflow prompt
  shot.videoModel = "seedance-2.5";
  const seedancePrompt = cleanProductionPrompt(p, shot);
  assert(seedancePrompt.includes("工作流 A：首尾帧生视频模式"));
  assert(seedancePrompt.includes("工作流 B：单图/多参考图生视频模式"));
  assert(seedancePrompt.includes("工作流 C：相机运动视频引导模式"));
  assert(seedancePrompt.includes("@Image1"));
  assert(seedancePrompt.includes("@Image2"));
  assert(seedancePrompt.includes("camera-motion.mp4"));
});

test("lighting ratio outputs key-to-fill ratio in standard order (e.g. 2.9:1) and clarifies parameter ratio", () => {
  const desc = describeLighting({
    preset: "classic-3-point",
    keyAzimuth: 45,
    keyElevation: 45,
    keyIntensity: 2.6,
    keyColor: "#fff8ed",
    fillEnabled: true,
    fillAzimuth: 315,
    fillElevation: 30,
    fillIntensity: 0.9,
    fillColor: "#e8f0fe",
    rimEnabled: true,
    rimAzimuth: 200,
    rimElevation: 60,
    rimIntensity: 1.8,
    rimColor: "#ffffff",
    ambientIntensity: 0.35,
    ambientColor: "#ffffff",
  });

  assert.equal(desc.ratio, "2.9:1");
  assert(desc.summaryZh.includes("2.9:1"));
  assert(desc.detailZh.includes("主辅参数光强比约 2.9:1"));
  assert(desc.detailEn.includes("2.9:1 key-to-fill parameter intensity ratio"));
});

test("macro camera movement recognizes round-trip zoom (35->100->35) and round-trip dolly", () => {
  const p = initial();
  const s = p.shots[0];
  s.in = 0;
  s.out = 4;

  // 1. Zoom round trip: 35mm -> 100mm -> 35mm (start and end equal, but middle zooms in)
  s.camera.position = [0, 1.6, 6];
  s.camera.target = [0, 1.6, 0];
  s.camera.focalLength = 35;
  s.keys = [
    { id: "k1", target: "camera", time: 0, position: [0, 1.6, 6], camera: { ...s.camera, focalLength: 35 } },
    { id: "k2", target: "camera", time: 2, position: [0, 1.6, 6], camera: { ...s.camera, focalLength: 100 } },
    { id: "k3", target: "camera", time: 4, position: [0, 1.6, 6], camera: { ...s.camera, focalLength: 35 } },
  ];

  const zoomMacro = macroCameraMovement(s);
  assert(!zoomMacro.movement.includes("机位固定（Static Shot）"));
  assert.match(zoomMacro.movement, /光学变焦推进至 100mm 后回缩至 35mm/);
  assert.match(zoomMacro.movementEn, /optical zoom in to 100mm then pulling back to 35mm/);

  // 2. Dolly In and Out round trip: 6m -> 2m -> 6m
  s.camera.focalLength = 50;
  s.keys = [
    { id: "d1", target: "camera", time: 0, position: [0, 1.6, 6], camera: { ...s.camera, position: [0, 1.6, 6] } },
    { id: "d2", target: "camera", time: 2, position: [0, 1.6, 2], camera: { ...s.camera, position: [0, 1.6, 2] } },
    { id: "d3", target: "camera", time: 4, position: [0, 1.6, 6], camera: { ...s.camera, position: [0, 1.6, 6] } },
  ];

  const dollyMacro = macroCameraMovement(s);
  assert.match(dollyMacro.movement, /向前推进约 4.0m 后拉回原位（Dolly In & Out）/);
  assert.match(dollyMacro.movementEn, /Smooth Dolly In ~4.0m then pulling backward/);
});

test("subject exit causal attribution distinguishes pure pan, camera push, and actor movement", () => {
  const p = initial();
  const s = p.shots[0];
  s.in = 0;
  s.out = 3;

  // Actor at [0, 0, 0]
  const actorObj = s.objects[0];
  actorObj.position = [0, 0, 0];

  // 1. Pure camera pan by 60° to the right (causing subject to exit frame on left)
  // Camera stays at [0, 1.6, 4], initial target [0, 1.6, 0] -> rotated target [5, 1.6, 0]
  s.camera.position = [0, 1.6, 4];
  s.camera.target = [0, 1.6, 0];
  s.keys = [
    { id: "p1", target: "camera", time: 0, position: [0, 1.6, 4], camera: { ...s.camera, target: [0, 1.6, 0] } },
    { id: "p2", target: "camera", time: 3, position: [0, 1.6, 4], camera: { ...s.camera, target: [8, 1.6, 0] } },
  ];

  const panMacro = macroCameraMovement(s);
  const panSubjZh = panMacro.cinematicSubjectsZh.find((x) => x.includes(actorObj.name));
  assert(panSubjZh, "Should contain actor description");
  assert(panSubjZh.includes("摇镜转向移出画面"));
  assert(!panSubjZh.includes("向前推移"));

  // 2. Pure actor movement out of frame while camera stays locked
  s.keys = [
    { id: "m1", target: actorObj.id, time: 0, position: [0, 0, 0] },
    { id: "m2", target: actorObj.id, time: 3, position: [10, 0, 0] },
  ];
  s.camera.target = [0, 1.6, 0];

  const actorMoveMacro = macroCameraMovement(s);
  const actorSubjZh = actorMoveMacro.cinematicSubjectsZh.find((x) => x.includes(actorObj.name));
  assert(actorSubjZh);
  assert(actorSubjZh.includes("角色自行走位移出画面"));
  assert(!actorSubjZh.includes("向前推移"));
});

test("prop shapes (cylinder, sphere, ramp) round-trip through parseProject and preserve geometry types", () => {
  const p = initial();
  const s = p.shots[0];

  s.objects.push({
    id: "prop-cyl",
    name: "圆台",
    kind: "prop",
    shape: "cylinder",
    position: [1, 0, 0],
    rotation: [0, 0, 0],
    scale: 1,
    size: [0.8, 0.9, 0.8],
    center: [0, 0.45, 0],
    visible: true,
    locked: false,
    color: "#a4b0be",
  });

  s.objects.push({
    id: "prop-sph",
    name: "瞄准球",
    kind: "prop",
    shape: "sphere",
    position: [-1, 0, 0],
    rotation: [0, 0, 0],
    scale: 1,
    size: [1, 1, 1],
    center: [0, 0.5, 0],
    visible: true,
    locked: false,
    color: "#e67e22",
  });

  s.objects.push({
    id: "prop-rmp",
    name: "坡道",
    kind: "prop",
    shape: "ramp",
    position: [0, 0, -2],
    rotation: [0, 0, 0],
    scale: 1,
    size: [1, 1, 2],
    center: [0, 0.5, 0],
    visible: true,
    locked: false,
    color: "#57606f",
  });

  const serialized = JSON.parse(JSON.stringify(p));
  const parsed = parseProject(serialized);
  const shot = parsed.shots[0];

  const cyl = shot.objects.find((o) => o.id === "prop-cyl");
  const sph = shot.objects.find((o) => o.id === "prop-sph");
  const rmp = shot.objects.find((o) => o.id === "prop-rmp");

  assert.equal(cyl?.shape, "cylinder");
  assert.equal(sph?.shape, "sphere");
  assert.equal(rmp?.shape, "ramp");
});

test("macro camera movement recognizes round-trip pan (Pan & Return)", async () => {
  const { macroCameraMovement } = await import("./spatial");
  const p = initial();
  const s = p.shots[0];

  s.camera.position = [0, 1.6, 4];
  s.camera.target = [0, 1.6, 0];
  s.keys = [
    { id: "k1", target: "camera", time: 0, position: [0, 1.6, 4], camera: { ...s.camera, target: [0, 1.6, 0] } },
    { id: "k2", target: "camera", time: 1.5, position: [0, 1.6, 4], camera: { ...s.camera, target: [5, 1.6, 0] } },
    { id: "k3", target: "camera", time: 3, position: [0, 1.6, 4], camera: { ...s.camera, target: [0, 1.6, 0] } },
  ];

  const macro = macroCameraMovement(s);
  assert(macro.movement.includes("Pan & Return") || macro.movement.includes("平滑回正"));
  assert(macro.movementEn.includes("Pan & Return"));
});

test("dynamic videoFileName parameter reflects in handoffTextFiles and cleanProductionPrompt", () => {
  const p = initial();
  const s = p.shots[0];
  s.videoModel = "seedance-2.5";

  const seedanceWebm = cleanProductionPrompt(p, s, "camera-motion.webm");
  assert(seedanceWebm.includes("camera-motion.webm"));
  assert(!seedanceWebm.includes("camera-motion.mp4"));

  s.videoModel = "omni-flash-1.1";
  const omniWebm = cleanProductionPrompt(p, s, "camera-motion.webm");
  assert(omniWebm.includes("camera-motion.webm"));
  assert(!omniWebm.includes("camera-motion.mp4"));

  const files = handoffTextFiles(p, s, "camera-motion.webm");
  assert(files["video-prompt.zh.txt"].includes("camera-motion.webm"));
});

test("asset GC disposes unreferenced meshes and removes from models Map", async () => {
  const { models, gcAssets } = await import("./assets");
  const { Group, Mesh, BoxGeometry, MeshBasicMaterial } = await import("three");

  let disposedGeometry = false;
  let disposedMaterial = false;

  const group = new Group();
  const geo = new BoxGeometry();
  geo.dispose = () => { disposedGeometry = true; };
  const mat = new MeshBasicMaterial();
  mat.dispose = () => { disposedMaterial = true; };
  const mesh = new Mesh(geo, mat);
  group.add(mesh);

  const orphanId = "asset-orphan-" + Date.now();
  const retainedId = "asset-retained-" + Date.now();
  models.set(orphanId, group);
  models.set(retainedId, new Group());

  gcAssets([retainedId]);

  assert.equal(models.has(orphanId), false, "Orphan asset must be removed from models map");
  assert.equal(models.has(retainedId), true, "Retained asset must be kept");
  assert.equal(disposedGeometry, true, "Geometry dispose must be called");
  assert.equal(disposedMaterial, true, "Material dispose must be called");

  // Clean up
  models.delete(retainedId);
});

test("resetSession atomically installs new project and preserves pre-decoded asset models", async () => {
  const { models } = await import("./assets");
  const { Group } = await import("three");

  const newAssetId = "audit-model-" + Date.now();
  const dummyGroup = new Group();
  models.set(newAssetId, dummyGroup);

  const proj = initial();
  proj.name = "Audit B Project";
  proj.assets.push({
    id: newAssetId,
    name: "audit.glb",
    data: "data:application/octet-stream;base64,AAA=",
    format: "glb",
  });

  const store = useStore.getState();
  store.resetSession(proj, "已载入测试草稿");

  assert.equal(models.has(newAssetId), true, "New asset model must NOT be garbage-collected during resetSession");
  assert.equal(useStore.getState().project.name, "Audit B Project");
  assert.equal(useStore.getState().canUndo, false, "Undo stack must be clean across resetSession");

  // Clean up
  models.delete(newAssetId);
});

test("resetSession clears undo stack across draft switches and prevents cross-project undo", () => {
  const store = useStore.getState();
  const projA = initial();
  projA.name = "Project A";
  store.resetSession(projA);

  // Edit in Project A
  store.camera({ focalLength: 85 });
  store.commitHistory();
  assert.equal(useStore.getState().canUndo, true, "Should have undo history in Project A");

  // Switch to Project B via resetSession
  const projB = initial();
  projB.name = "Project B";
  store.resetSession(projB);

  assert.equal(useStore.getState().canUndo, false, "canUndo must be false immediately after draft switch");
  assert.equal(useStore.getState().canRedo, false, "canRedo must be false immediately after draft switch");

  // Attempting undo must be a no-op and must not revert back to Project A
  store.undo();
  assert.equal(useStore.getState().project.name, "Project B", "Undo must not jump back to previous draft Project A");
});

test("set with incoming project preserves incoming assets during history trimming GC", async () => {
  const { models } = await import("./assets");
  const { Group } = await import("three");

  const currentProj = initial();
  currentProj.name = "Current Without Asset";
  useStore.getState().resetSession(currentProj);

  const incomingAssetId = "incoming-asset-" + Date.now();
  models.set(incomingAssetId, new Group());

  const incomingProj = initial();
  incomingProj.name = "Incoming With Asset";
  incomingProj.assets.push({
    id: incomingAssetId,
    name: "incoming.glb",
    data: "data:application/octet-stream;base64,BBB=",
    format: "glb",
  });

  useStore.getState().set({ project: incomingProj });

  assert.equal(
    models.has(incomingAssetId),
    true,
    "Incoming project asset model must be preserved even when trimHistory/recordChange triggers GC",
  );

  // Clean up
  models.delete(incomingAssetId);
});

