// 队列桥接：让放进 工作区/ae-scripts/queue/ 的脚本调用 adobe-agent-skills 的 34 个现成脚本。
// 用法（在队列脚本里）：
//   $.evalFile(new File("/Users/suzhizhou/Documents/私人助理/工具/AE调用/队列桥接/xk_lib_call.jsx"));
//   xkLibCall("comp-detail.jsx", {compName: "主合成"}, "comp-detail");
// 结果 JSON 复制到 工作区/ae-scripts/logs/<输出名>.json，远程 Agent 读这个文件即可。
$.global.xkLibCall = function (scriptName, args, outName) {
  var LIB = "/Users/suzhizhou/Documents/私人助理/工具/AE调用/adobe-agent-skills/skills/after-effects/scripts/";
  var OUT = "/Users/suzhizhou/Documents/私人助理/工作区/ae-scripts/logs/";
  var argF = new File("/tmp/ae-assistant-args.json");
  argF.encoding = "UTF-8"; argF.open("w"); if (typeof JSON === "undefined") $.evalFile(new File(LIB + "lib/json2.jsx"));
  argF.write(typeof args === "string" ? args : JSON.stringify(args || {})); argF.close();
  var resF = new File("/tmp/ae-assistant-result.json");
  if (resF.exists) resF.remove();
  var script = new File(LIB + scriptName);
  if (!script.exists) throw new Error("脚本不存在: " + scriptName);
  $.evalFile(script);
  var out = new File(OUT + (outName || scriptName.replace(/\.jsx$/, "")) + ".json");
  out.encoding = "UTF-8";
  if (resF.exists) {
    resF.encoding = "UTF-8"; resF.open("r"); var txt = resF.read(); resF.close();
    out.open("w"); out.write(txt); out.close();
    return txt;
  }
  out.open("w"); out.write('{"error":"脚本没有产生结果文件"}'); out.close();
  return null;
};
