import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// The built SPA is served by the module at `/designer/` (StaticFiles mount), so assets must resolve
// under that prefix. The dev server proxies `/design` and `/core` to the running module so the
// canvas talks to the real engine while developing. The proxy target is `VITE_PROXY_TARGET` so the
// same config works on the host (localhost:9107) and inside docker compose (the service name).
const proxyTarget = process.env.VITE_PROXY_TARGET ?? "http://localhost:9107";

export default defineConfig({
  base: "/designer/",
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    host: true, // listen on 0.0.0.0 so the dev server is reachable from outside the container
    port: 5173,
    proxy: {
      "/design": proxyTarget,
      "/core": proxyTarget,
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
