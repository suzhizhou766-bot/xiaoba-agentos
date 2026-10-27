import { test } from "node:test";
import assert from "node:assert/strict";
import { initial } from "./core";
import { handoffTextFiles } from "./handoff";
import { zipSync, unzipSync, strToU8, strFromU8 } from "fflate";

for (const model of ["seedance-2.5", "omni-flash-1.1"] as const) {
  test(`${model}: missing video disables video guidance in exported text`, () => {
    const project = initial(); const shot = project.shots[0]; shot.videoModel = model;
    const textFiles = handoffTextFiles(project, shot, null);
    const archive = unzipSync(zipSync(Object.fromEntries(Object.entries(textFiles).map(([name, text]) => [name, strToU8(text)]))));
    const prompt = strFromU8(archive["video-prompt.zh.txt"]);
    assert.doesNotMatch(prompt, /camera-motion\.(mp4|webm)|<VIDEO_REF_0>|随包导出的/);
    assert.match(prompt, /未生成机位视频|Camera guidance unavailable/);
    assert.match(prompt, /工作流 A|Workflow 1/);
    assert.match(prompt, /工作流 B|Workflow 2/);
  });
  test(`${model}: available video keeps its actual filename`, () => {
    const project = initial(); const shot = project.shots[0]; shot.videoModel = model;
    for (const file of ["camera-motion.mp4", "camera-motion.webm"]) {
      const text = handoffTextFiles(project, shot, file)["video-prompt.zh.txt"];
      assert.ok(text.includes(file));
      assert.doesNotMatch(text, /未生成机位视频|Camera guidance unavailable/);
    }
  });
}
