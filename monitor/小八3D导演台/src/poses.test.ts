import { test } from "node:test";
import assert from "node:assert/strict";
import { actor, compile, initial, parseProject } from "./core";
import { hipHeight, isPoseId, poseBounds, POSE_IDS, POSES, poseOf } from "./poses";
import { DIRECTOR_CODES, toDirectorParams } from "./directorBridge";

/** 提示词导演台「人物动作」下拉框的取值（抄自 director.html）。 */
const DIRECTOR_ACTIONS = ["stand", "walk", "run", "lookback", "lean", "sit", "stretch", "hold", "wave", "squat", "jump"];

test("姿态编号与提示词导演台的人物动作一一对应", () => {
  assert.deepEqual([...POSE_IDS].sort(), [...DIRECTOR_ACTIONS].sort());
  assert.ok(DIRECTOR_CODES.count);
});

test("每个姿态的关节角度都在人体做得到的范围内", () => {
  for (const id of POSE_IDS) {
    const p = POSES[id];
    assert.ok(p.label, id);
    assert.ok(Math.abs(p.lean) <= 45 && Math.abs(p.twist) <= 45 && Math.abs(p.headYaw) <= 80 && Math.abs(p.headPitch) <= 40, id);
    for (const side of [p.left, p.right]) {
      assert.ok(side.arm.raise >= -60 && side.arm.raise <= 180 && side.arm.out >= 0 && side.arm.out <= 170, id);
      assert.ok(side.arm.elbow >= 0 && side.arm.elbow <= 150, id);
      assert.ok(side.leg.hip >= -40 && side.leg.hip <= 125 && side.leg.knee >= 0 && side.leg.knee <= 150, id);
    }
  }
});

test("脚落在地面上：站直髋高 0.9 米，坐和蹲更低，跳起来离地", () => {
  assert.ok(Math.abs(hipHeight(POSES.stand) - 0.9) < 1e-9);
  assert.ok(Math.abs(hipHeight(POSES.sit) - 0.47) < 0.01);
  assert.ok(hipHeight(POSES.squat) < 0.5);
  assert.ok(hipHeight(POSES.jump) > 0.9);
  for (const id of POSE_IDS) assert.ok(hipHeight(POSES[id]) >= 0.2 && hipHeight(POSES[id]) <= 1.3, id);
});

test("包围盒：站立沿用旧尺寸，坐下和蹲下变矮，举手变高，都是正数", () => {
  assert.deepEqual(poseBounds(undefined), { size: [0.52, 1.8, 0.4], center: [0, 0.9, 0] });
  assert.deepEqual(poseBounds("stand"), poseBounds(undefined));
  assert.ok(poseBounds("sit").size[1] < 1.5);
  assert.ok(poseBounds("squat").size[1] < poseBounds("sit").size[1] + 0.2);
  assert.ok(poseBounds("stretch").size[1] > 1.8);
  for (const id of POSE_IDS) {
    const b = poseBounds(id);
    assert.ok(b.size.every((n) => n > 0.2 && n < 3), id);
    assert.ok(Math.abs(b.center[1] - (POSES[id].lift + b.size[1] / 2)) < 0.011, id);
  }
});

test("未知姿态按站立处理；工程文件里的姿态必须是认识的", () => {
  assert.equal(poseOf(undefined), POSES.stand);
  assert.equal(isPoseId("sit"), true);
  assert.equal(isPoseId("fly"), false);
  assert.equal(isPoseId("constructor"), false);
  const project = initial();
  project.shots[0].objects[0].pose = "sit";
  assert.equal(parseProject(JSON.parse(JSON.stringify(project))).shots[0].objects[0].pose, "sit");
  (project.shots[0].objects[0] as { pose: string }).pose = "fly";
  assert.throws(() => parseProject(JSON.parse(JSON.stringify(project))), /场景对象/);
  const old = initial();
  assert.equal(parseProject(JSON.parse(JSON.stringify(old))).shots[0].objects[0].pose, undefined);
});

test("交接包写明占位姿态；送到提示词导演台时带上人物动作", () => {
  const project = initial();
  const shot = project.shots[0];
  shot.objects.find((o) => o.kind === "actor")!.pose = "run";
  shot.objects.push({ ...actor("道具箱", [3, 0, 0]), kind: "prop" });
  const roles = compile(project, shot).json!.素材职责 as { 名称: string; 占位姿态?: string }[];
  assert.match(roles[0].占位姿态!, /^奔跑/);
  assert.equal(roles[roles.length - 1].占位姿态, undefined);
  assert.equal(toDirectorParams(project, shot).params.person?.action, "run");
});
