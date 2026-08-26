import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies /api to FastAPI so the console runs same-origin in
// development. That keeps the browser's CORS behaviour identical to production,
// where NGINX fronts both -- a dev-only cross-origin setup hides CORS bugs until
// deployment.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8090",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
      "/media": { target: "http://127.0.0.1:8090", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8090", ws: true },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
