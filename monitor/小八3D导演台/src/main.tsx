import { createRoot } from "react-dom/client";
import App from "./App";
import "./style.css";
import "./glass.css";
// 嵌在监看中心里时：隐藏重复的品牌栏；监看中心是深色时背景透出它的壁纸，浅色时保留自己的深色壁纸
if (window.self !== window.top) {
  const root = document.documentElement;
  root.classList.add("embedded");
  const sync = () => {
    let dark = false;
    try {
      dark = getComputedStyle(window.parent.document.documentElement).colorScheme.includes("dark");
    } catch {
      dark = false;
    }
    root.classList.toggle("see-through", dark);
  };
  sync();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", sync);
}
createRoot(document.getElementById("root")!).render(<App />);
