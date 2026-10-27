// after-effects-control 示例：Hello AE 四段文字动效（10 秒）
// 自己写日志 logs/hello_fx.log（经队列执行时，执行器另记 logs/<队列文件名>.log）。
// 不删除、不覆盖已有东西：同名合成或工程文件已存在时，名字后面加时间戳。
(function () {
  var LOG = "hello_fx";
  var LF = new File("/Users/suzhizhou/Documents/私人助理/工作区/ae-scripts/logs/hello_fx.log");
  function L(m) { LF.encoding = "UTF-8"; LF.open("a"); LF.writeln("[" + new Date().toTimeString().substr(0, 8) + "] " + m); LF.close(); }
  try {
  var WS = "/Users/suzhizhou/Documents/私人助理/工作区";
  var dirs = [WS + "/ae-scripts/preview", WS + "/ae-projects"];
  for (var d = 0; d < dirs.length; d++) { var fd = new Folder(dirs[d]); if (!fd.exists) fd.create(); }
  var STAMP = (function () { var t = new Date(); function z(n) { return (n < 10 ? "0" : "") + n; }
    return t.getFullYear() + z(t.getMonth() + 1) + z(t.getDate()) + "_" + z(t.getHours()) + z(t.getMinutes()) + z(t.getSeconds()); })();
  app.beginUndoGroup("Hello AE 文字动效示例");
  var W = 1920, H = 1080, FPS = 30, DUR = 10, NAME = "Hello_AE_动效";
  for (var i = app.project.numItems; i >= 1; i--) {
    var it = app.project.item(i);
    if (it instanceof CompItem && it.name === NAME) { NAME = NAME + "_" + STAMP; L("已有同名合成，新合成改名为 " + NAME); break; }
  }
  var comp = app.project.items.addComp(NAME, W, H, 1, DUR, FPS);
  comp.motionBlur = true; comp.shutterAngle = 180;

  // 背景：渐变
  var bg = comp.layers.addSolid([0.05, 0.06, 0.10], "背景", W, H, 1, DUR);
  try {
    var rp = bg.property("ADBE Effect Parade").addProperty("ADBE Ramp");
    rp.property("ADBE Ramp-0001").setValue([W / 2, 0]);
    rp.property("ADBE Ramp-0002").setValue([0.12, 0.13, 0.25]);
    rp.property("ADBE Ramp-0003").setValue([W / 2, H]);
    rp.property("ADBE Ramp-0004").setValue([0.01, 0.01, 0.03]);
  } catch (e) { L("渐变失败: " + e); }

  function anims(l) { return l.property("ADBE Text Properties").property("ADBE Text Animators"); }
  function addAnim(l, name) { anims(l).addProperty("ADBE Text Animator").name = name; }
  function A(l, name) { return anims(l).property(name); }
  function addProp(l, name, mn, val) {
    A(l, name).property("ADBE Text Animator Properties").addProperty(mn);
    A(l, name).property("ADBE Text Animator Properties").property(mn).setValue(val);
  }
  function addExprSel(l, name, expr) {
    A(l, name).property("ADBE Text Selectors").addProperty("ADBE Text Expressible Selector");
    var sels = A(l, name).property("ADBE Text Selectors");
    sels.property(sels.numProperties).property("ADBE Text Expressible Amount").expression = expr;
  }
  function centerAnchor(l, t) {
    var r = l.sourceRectAtTime(t, false);
    l.property("ADBE Transform Group").property("ADBE Anchor Point").setValue([r.left + r.width / 2, r.top + r.height / 2]);
    l.property("ADBE Transform Group").property("ADBE Position").setValue([W / 2, H / 2]);
    return r;
  }
  function mkText(name, inP, outP, o) {
    var l = comp.layers.addText("Hello AE");
    l.name = name;
    var sp = l.property("ADBE Text Properties").property("ADBE Text Document");
    var td = sp.value;
    td.text = "Hello AE";
    td.fontSize = o.size;
    try { td.font = o.font; } catch (e) { L("字体 " + o.font + " 不可用"); }
    td.applyFill = true; td.fillColor = o.color; td.applyStroke = false;
    td.tracking = 0;
    try { td.allCaps = false; } catch (e) {}
    try { td.fontCapsOption = FontCapsOption.FONT_NORMAL_CAPS; } catch (e) {}
    td.justification = ParagraphJustification.CENTER_JUSTIFY;
    sp.setValue(td);
    l.inPoint = inP; l.outPoint = outP;
    l.motionBlur = true;
    centerAnchor(l, inP);
    l.property("ADBE Transform Group").property("ADBE Opacity").expression =
      "linear(time, outPoint - 0.35, outPoint, 100, 0)";
    return l;
  }

  // ① 弹性弹出 0–2.5s
  var a = mkText("① 弹性弹出", 0, 2.5, { size: 190, font: "Helvetica-Bold", color: [1, 0.82, 0.25] });
  addAnim(a, "弹性");
  addProp(a, "弹性", "ADBE Text Scale 3D", [0, 0, 100]);
  addProp(a, "弹性", "ADBE Text Position 3D", [0, 90, 0]);
  addExprSel(a, "弹性",
    "var t = time - inPoint - 0.2 - (textIndex - 1) * 0.06;\n" +
    "var v = t <= 0 ? 100 : 100 * Math.cos(2.2 * t * Math.PI * 2) * Math.exp(-5.5 * t);\n[v, v, v]");
  addAnim(a, "淡入");
  addProp(a, "淡入", "ADBE Text Opacity", 0);
  addProp(a, "淡入", "ADBE Text Blur", [20, 20]);
  addExprSel(a, "淡入",
    "var t = time - inPoint - 0.2 - (textIndex - 1) * 0.06;\nvar v = linear(t, 0, 0.25, 100, 0);\n[v, v, v]");
  try { a.property("ADBE Text Properties").property("ADBE Text More Options").property("ADBE Text Anchor Point Align").setValue([0, -50]); } catch (e) {}

  // ② 打字机 2.5–5s
  var b = mkText("② 打字机", 2.5, 5, { size: 150, font: "Menlo-Bold", color: [0.55, 0.9, 1] });
  var bsp = b.property("ADBE Text Properties").property("ADBE Text Document");
  var btd = bsp.value; btd.text = "Hello AE_"; btd.justification = ParagraphJustification.LEFT_JUSTIFY; bsp.setValue(btd);
  var br = b.sourceRectAtTime(2.5, false);
  b.property("ADBE Transform Group").property("ADBE Anchor Point").setValue([br.left, br.top + br.height / 2]);
  b.property("ADBE Transform Group").property("ADBE Position").setValue([W / 2 - br.width / 2, H / 2]);
  bsp.expression =
    "var s = 'Hello AE';\nvar t = time - inPoint - 0.25;\n" +
    "var n = Math.max(0, Math.min(s.length, Math.floor(t / 0.11)));\n" +
    "var blink = Math.floor(time * 2.5) % 2 == 0;\n" +
    "s.substr(0, n) + ((n < s.length || blink) ? '_' : ' ');";

  // ③ 散落飞入 5–7.5s
  var c = mkText("③ 散落飞入", 5, 7.5, { size: 190, font: "Helvetica-Bold", color: [1, 0.45, 0.62] });
  var easeC = "var p = Math.min(Math.max(t / 0.7, 0), 1);\nvar e = 1 - Math.pow(1 - p, 3);\n";
  addAnim(c, "飞入");
  addProp(c, "飞入", "ADBE Text Position 3D", [0, -320, 0]);
  addProp(c, "飞入", "ADBE Text Rotation", 220);
  addProp(c, "飞入", "ADBE Text Scale 3D", [260, 260, 100]);
  addProp(c, "飞入", "ADBE Text Opacity", 0);
  addProp(c, "飞入", "ADBE Text Blur", [30, 30]);
  addExprSel(c, "飞入",
    "seedRandom(textIndex, true);\nvar t = time - inPoint - 0.15 - random(0, 0.55);\n" + easeC + "var v = 100 * (1 - e);\n[v, v, v]");
  addAnim(c, "横向散开");
  addProp(c, "横向散开", "ADBE Text Position 3D", [600, 0, 0]);
  addExprSel(c, "横向散开",
    "seedRandom(textIndex, true);\nvar t = time - inPoint - 0.15 - random(0, 0.55);\n" +
    "seedRandom(textIndex + 99, true);\nvar dir = random(-1, 1);\n" + easeC + "var v = 100 * (1 - e) * dir;\n[v, v, v]");

  // ④ 字距展开 + 发光 7.5–10s
  var d = mkText("④ 字距发光", 7.5, 10, { size: 170, font: "Helvetica-Bold", color: [1, 1, 1] });
  addAnim(d, "字距");
  addProp(d, "字距", "ADBE Text Tracking Amount", 100);
  addExprSel(d, "字距", "var v = ease(time - inPoint, 0, 2.0, -60, 45);\n[v, v, v]");
  addAnim(d, "中心展开");
  addProp(d, "中心展开", "ADBE Text Opacity", 0);
  addProp(d, "中心展开", "ADBE Text Blur", [25, 25]);
  addExprSel(d, "中心展开",
    "var c = (textTotal + 1) / 2;\nvar t = time - inPoint - 0.1 - Math.abs(textIndex - c) * 0.08;\nvar v = linear(t, 0, 0.35, 100, 0);\n[v, v, v]");
  d.property("ADBE Transform Group").property("ADBE Scale").expression =
    "var s = ease(time - inPoint, 0, 2.5, 92, 104);\n[s, s]";
  try {
    var g = d.property("ADBE Effect Parade").addProperty("ADBE Glo2");
    g = d.property("ADBE Effect Parade").property(1);
    g.property("ADBE Glo2-0002").setValue(40);
    g.property("ADBE Glo2-0003").setValue(45);
    g.property("ADBE Glo2-0004").expression = "1.2 + 0.6 * Math.sin((time - inPoint) * 4)";
  } catch (e) { L("发光失败: " + e); }

  // 合成标记
  var labels = [[0, "① 弹性弹出"], [2.5, "② 打字机"], [5, "③ 散落飞入"], [7.5, "④ 字距发光"]];
  for (var k = 0; k < labels.length; k++) comp.markerProperty.setValueAtTime(labels[k][0], new MarkerValue(labels[k][1]));

  comp.openInViewer();
  comp.time = 1.2;
  app.endUndoGroup();

  // 预览帧
  var times = [0.35, 0.7, 1.6, 3.0, 3.6, 4.6, 5.35, 5.7, 6.6, 7.8, 8.5, 9.5];
  var n = 0;
  for (var q = 0; q < times.length; q++) {
    try { comp.saveFrameToPng(times[q], new File(WS + "/ae-scripts/preview/hello_fx_" + STAMP + "_f" + (q < 9 ? "0" : "") + (q + 1) + ".png")); n++; }
    catch (e) { L("截帧失败 " + times[q] + ": " + e); break; }
  }
  L("预览帧请求 " + n + " 张");

  // 保存工程：默认不保存（成片就是交付物）；用户要求保存时把 SAVE_PROJECT 改成 true
  var SAVE_PROJECT = false;
  if (SAVE_PROJECT) {
    try {
      var pf = new File(WS + "/ae-projects/Hello_AE_动效.aep");
      if (pf.exists) pf = new File(WS + "/ae-projects/Hello_AE_动效_" + STAMP + ".aep");
      app.project.save(pf);
      L("工程已保存: " + pf.fsName);
    } catch (e) { L("保存失败: " + e); }
  } else { L("按规则不保存工程"); }
  L("全部完成");
  } catch (e) { L("ERROR " + e.toString() + " @line " + e.line); try { app.endUndoGroup(); } catch (x) {} }
})();
