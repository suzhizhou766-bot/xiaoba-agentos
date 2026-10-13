import { test } from "node:test";
import assert from "node:assert/strict";
import { initial, type Shot } from "./core";
import { compose, composeInfo, composeMessages, DIRECTOR_CODES, toDirectorParams } from "./directorBridge";

/** 提示词导演台各下拉框的内部取值（抄自 director.html）。送过去的值必须在这里面，否则会被它忽略。 */
const OPTIONS: Record<string, string[]> = {"focal": ["14", "18", "24", "28", "35", "50", "70", "85", "100", "135", "200", "400"], "aperture": ["f/1.4", "f/1.8", "f/2.8", "f/4", "f/5.6", "f/8", "f/11"], "shot_size": ["extreme_wide", "wide_shot", "full_shot", "medium_shot", "close_up", "extreme_close", "macro_close"], "cam_height": ["ground", "low", "hip", "eye", "high", "bird"], "cam_angle": ["level", "low_angle", "high_angle", "dutch"], "cam_move": ["static", "push", "pull", "truck", "pan", "orbit", "handheld_follow", "crane"], "stability": ["locked", "slight", "docu_handheld"], "light_source": ["sun", "golden", "sunset", "night_urban", "indoor", "backlit", "overcast_soft", "moon"], "light_ratio": ["low", "mid", "high"], "light_dir": ["side", "front", "back", "top"], "lens_type": ["ultrawide", "wide", "normal", "shorttele", "tele", "supertele", "macro"], "time_of_day": ["morning", "day", "golden", "bluehour", "night", "overcast"]};
const shotOf = (edit?: (s: Shot) => void) => {
  const project = initial();
  const shot = project.shots[0];
  edit?.(shot);
  return { project, shot };
};
const cam = (shot: Shot, position: [number, number, number], time: number) =>
  shot.keys.push({ id: `k${time}`, target: "camera", time, position, camera: { ...shot.camera, position, lookAt: null } });

test("送出的每个值都是提示词导演台下拉框里有的选项", () => {
  for (const preset of ["classic-3-point", "rembrandt", "film-noir", "soft-window", "golden-hour", "cool-night"] as const) {
    const { project, shot } = shotOf((s) => {
      s.lighting = { ...(s.lighting ?? ({} as never)), preset } as never;
    });
    const { params } = toDirectorParams(project, shot, 1);
    for (const [field, options] of Object.entries(OPTIONS)) {
      const value = (params as Record<string, unknown>)[field];
      if (value !== undefined) assert.ok(options.includes(value as string), `${field}=${value}`);
    }
  }
});

test("焦距和光圈取最近的档位并说明", () => {
  const { project, shot } = shotOf((s) => {
    s.camera.focalLength = 37;
    s.camera.aperture = 2.2;
  });
  const handoff = toDirectorParams(project, shot, 5);
  assert.equal(handoff.params.focal, "35");
  assert.equal(handoff.params.aperture, "f/1.8");
  assert.equal(handoff.params.lens_type, "normal");
  assert.ok(handoff.notes.some((n) => n.includes("37mm") && n.includes("35mm")));
  assert.equal(handoff.at, 5);
  assert.equal(handoff.shot, shot.name);
});

test("默认镜头：两个角色、人数和机位高度", () => {
  const { project, shot } = shotOf();
  const { params } = toDirectorParams(project, shot);
  assert.equal(params.has_person, true);
  assert.deepEqual(params.person, { count: "2", action: "stand" });
  assert.equal(params.cam_move, "static");
  assert.equal(params.stability, "locked");
});

test("没有角色时不填人物；超过三人不填人数", () => {
  const none = shotOf((s) => {
    s.objects = s.objects.filter((o) => o.kind !== "actor");
  });
  assert.equal(toDirectorParams(none.project, none.shot).params.has_person, false);
  const many = shotOf((s) => {
    const base = s.objects.find((o) => o.kind === "actor")!;
    for (let i = 0; i < 3; i++) s.objects.push({ ...base, id: `extra${i}` });
  });
  const handoff = toDirectorParams(many.project, many.shot);
  assert.equal(handoff.params.has_person, true);
  assert.equal(handoff.params.person?.count, undefined);
  assert.ok(handoff.notes.some((n) => n.includes("最多选 3 人")));
});

test("从首尾机位判断运镜：推近、拉远、升降", () => {
  const run = (from: [number, number, number], to: [number, number, number]) => {
    const { project, shot } = shotOf((s) => {
      s.camera = { ...s.camera, lookAt: null, target: [0, 1.5, 0] };
      s.keys = [];
      cam(s, from, 0);
      cam(s, to, 3);
      s.in = 0;
      s.out = 3;
      s.duration = 3;
    });
    return toDirectorParams(project, shot).params.cam_move;
  };
  assert.equal(run([0, 1.5, 8], [0, 1.5, 4]), "push");
  assert.equal(run([0, 1.5, 4], [0, 1.5, 8]), "pull");
  assert.equal(run([0, 1.5, 6], [0, 4.5, 6.2]), "crane");
  assert.equal(run([0, 1.5, 6], [6, 1.5, 0]), "orbit");
});

test("俯仰角度由机位和视线目标决定", () => {
  const angle = (y: number) => {
    const { project, shot } = shotOf((s) => {
      s.keys = [];
      s.camera = { ...s.camera, lookAt: null, target: [0, 1.5, 0], position: [0, y, 5] };
    });
    return toDirectorParams(project, shot).params.cam_angle;
  };
  assert.equal(angle(1.5), "level");
  assert.equal(angle(0.2), "low_angle");
  assert.equal(angle(4), "high_angle");
});

test("写最终提示词：必须先选视频模型；消息里带指南和镜头数据，不带坐标之外的密钥类内容", () => {
  const blank = shotOf();
  assert.throws(() => composeMessages(blank.project, blank.shot), /视频模型/);
  const { project, shot } = shotOf((s) => {
    s.videoModel = "seedance-2.5";
    s.visualBrief = "雨夜的站台";
  });
  const [system, user] = composeMessages(project, shot);
  assert.equal(system.role, "system");
  assert.match(system.content, /只输出提示词本身/);
  assert.match(user.content, /《模型使用指南》/);
  assert.match(user.content, /雨夜的站台/);
});

test("调用后台接口：成功返回文字，失败给出后台的说明", async () => {
  const { project, shot } = shotOf((s) => {
    s.videoModel = "omni-flash-1.1";
  });
  const calls: { url: string; body: any }[] = [];
  const ok = (async (url: string, init?: RequestInit) => {
    calls.push({ url, body: init?.body ? JSON.parse(init.body as string) : null });
    return new Response(JSON.stringify(init ? { ok: true, text: "成品", model: "real", ms: 1200 } : { available: true, models: ["m"], followsClient: false }));
  }) as unknown as typeof fetch;
  assert.deepEqual(await composeInfo(ok), { available: true, models: ["m"], followsClient: false });
  assert.deepEqual(await compose(project, shot, "m", ok), { text: "成品", model: "real", ms: 1200 });
  assert.equal(calls[1].url, "/stage/api/compose");
  assert.equal(calls[1].body.model, "m");
  assert.equal(calls[1].body.messages.length, 2);
  const bad = (async () => new Response(JSON.stringify({ ok: false, error: "Key 池返回 HTTP 403" }))) as unknown as typeof fetch;
  await assert.rejects(compose(project, shot, "m", bad), /HTTP 403/);
  const down = (async () => {
    throw new Error("offline");
  }) as unknown as typeof fetch;
  assert.equal((await composeInfo(down)).available, false);
});

test("每个字段的取值表都和测试里抄的选项一致", () => {
  for (const [field, options] of Object.entries(OPTIONS)) assert.deepEqual(Object.values(DIRECTOR_CODES[field]), options, field);
});
