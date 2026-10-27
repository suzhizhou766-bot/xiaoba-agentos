// 放进 工作区/ae-scripts/queue/：输出 AE 状态到 logs/ae-health.json
// 桥接面板检测（2026-10-17 修正）：app.findMenuCommandId 查不到任何 ScriptUI 面板（连 AE 自带的也返回 0），不能用。
//   bridgePanelInstalled：面板文件在不在 ScriptUI Panels 目录（1/0）
//   bridgePanelRunning：面板开着（1/0）。面板在主引擎留下全局 autoRunCheckbox；关掉面板后控件失效，读它会抛错。
//     不能只看 mcpCheckTaskId：关掉面板后它不会被清空。
//   bridgeAutoRun：面板里 Auto-run commands 是否勾选（1/0，面板没开时为 0）。不勾选时 MCP 命令不会执行。
(function () {
  var panelName = "mcp-bridge-auto.jsx";
  var panelDirs = [Folder.appPackage.parent.fsName + "/Scripts/ScriptUI Panels",
                   new Folder("~/Library/Preferences").fsName + "/Adobe/After Effects/" + app.version.split(".").slice(0, 2).join(".") + "/Scripts/ScriptUI Panels"];
  var installed = 0;
  for (var i = 0; i < panelDirs.length; i++) { if (new File(panelDirs[i] + "/" + panelName).exists) installed = 1; }
  var running = 0, autoRun = 0;
  try { if ($.global.autoRunCheckbox) { autoRun = $.global.autoRunCheckbox.value ? 1 : 0; running = 1; } } catch (e) {}
  var r = { time: new Date().toString(), aeVersion: app.version, project: app.project.file ? app.project.file.fsName : null,
            items: app.project.numItems, activeComp: (app.project.activeItem && app.project.activeItem instanceof CompItem) ? app.project.activeItem.name : null,
            bridgePanelInstalled: installed, bridgePanelRunning: running, bridgeAutoRun: autoRun,
            scriptsCanWrite: app.preferences.getPrefAsLong("Main Pref Section", "Pref_SCRIPTING_FILE_NETWORK_SECURITY") };
  var s = "{"; var first = true;
  for (var k in r) { s += (first ? "" : ",") + '"' + k + '":' + (r[k] === null ? "null" : (typeof r[k] === "number" ? r[k] : '"' + String(r[k]).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"')); first = false; }
  s += "}";
  var f = new File("/Users/suzhizhou/Documents/私人助理/工作区/ae-scripts/logs/ae-health.json");
  f.encoding = "UTF-8"; f.open("w"); f.write(s); f.close();
})();
