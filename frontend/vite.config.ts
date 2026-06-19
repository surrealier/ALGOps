import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built to dist/ which the master serves at "/". Dev server proxies API+WS to the master.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
      "/healthz": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
