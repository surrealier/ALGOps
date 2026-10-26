import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built into the Python package (src/algops/master/static/ui) so `algops master` serves the UI
// with no Node toolchain at deploy time. `npm run dev` proxies API + WS to a local master.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "../src/algops/master/static/ui", emptyOutDir: true },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
      "/healthz": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
