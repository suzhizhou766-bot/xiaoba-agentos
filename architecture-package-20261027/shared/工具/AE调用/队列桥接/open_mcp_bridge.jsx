// 放进 工作区/ae-scripts/queue/：检查 MCP 桥接面板是否开着，结果写日志 logs/open_mcp_bridge.log
// 2026-10-17 修正：脚本打不开可停靠面板。
//   - app.findMenuCommandId 查不到 ScriptUI 面板（返回 0），拿不到菜单命令号；
//   - 用 $.evalFile 直接跑面板脚本会弹出一个不响应的浮动窗口，还会让队列监听器停掉，必须重启 AE。不要这样做。
// 面板没开时：请用户点「窗口」菜单最底下（要往下滚）的 mcp-bridge-auto.jsx；
// 本机 Agent 也可以用电脑操控（全屏模式）点开这个菜单项。打开一次后 AE 通常会在工作区里记住它。
(function () {
  var log = $.global.xkLog || function () {};
  // 面板在主引擎留下全局 autoRunCheckbox；关掉面板后控件失效，读它会抛错（mcpCheckTaskId 关面板后不清空，不能用）
  var running = false, autoRun = false;
  try { if ($.global.autoRunCheckbox) { autoRun = !!$.global.autoRunCheckbox.value; running = true; } } catch (e) {}
  if (running && autoRun) { log("open_mcp_bridge", "桥接面板已在运行，Auto-run 已开"); }
  else if (running) { log("open_mcp_bridge", "桥接面板开着，但 Auto-run commands 没勾选：MCP 命令不会执行，请用户勾上"); }
  else { log("open_mcp_bridge", "桥接面板没开：请在「窗口」菜单最底下（往下滚）点 mcp-bridge-auto.jsx。脚本无法代开。"); }
})();
