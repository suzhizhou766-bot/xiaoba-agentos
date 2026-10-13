import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({
  // 由小八监看中心在 /stage/ 下提供（http://127.0.0.1:8799/stage/）
  base: "/stage/",
  plugins: [react()],
  clearScreen: false,
  server: { port: 5173, strictPort: true },
  build: {
    chunkSizeWarningLimit: 1800,
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (id.includes("/src/builtinModels")) return "models";
          if (id.includes("/node_modules/three/")) return "three";
          if (id.includes("/node_modules/@react-three/")) return "r3f";
          if (
            id.includes("/node_modules/react/") ||
            id.includes("/react-dom/") ||
            id.includes("/scheduler/")
          ) {
            return "react";
          }
        },
      },
    },
  },
});
