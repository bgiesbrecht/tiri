import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

// Tiri UI build config. Output lands in ui/dist/ which FastAPI mounts at
// `/app`. The dev server proxies API calls to the backend at port 8000;
// this is for `npm run dev` only — production builds hit same-origin.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  base: "/app/",
  server: {
    proxy: {
      "/rooms": "http://localhost:8000",
      "/config": "http://localhost:8000",
      "/conversations": "http://localhost:8000",
      "/mcp": "http://localhost:8000",
    },
  },
  build: {
    outDir: "dist",
    assetsDir: "assets",
    sourcemap: true,
  },
});
