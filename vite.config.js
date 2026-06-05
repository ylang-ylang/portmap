import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { readFileSync } from "node:fs";

const catalogTarget = process.env.PORTMAP_CATALOG_TARGET || "http://127.0.0.1:80";
const mockCatalog = process.env.PORTMAP_CATALOG_MOCK === "1";

function mockCatalogPlugin() {
  return {
    name: "portmap-mock-catalog",
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const path = (request.url || "").split("?")[0];
        if (path === "/registry.json") {
          const body = readFileSync(new URL("./frontend/mock/registry.json", import.meta.url));
          response.statusCode = 200;
          response.setHeader("content-type", "application/json; charset=utf-8");
          response.end(body);
          return;
        }
        if (path.startsWith("/actions/compose-")) {
          response.statusCode = 200;
          response.setHeader("content-type", "application/json; charset=utf-8");
          response.end(JSON.stringify({
            ok: true,
            message: "mock action accepted",
          }));
          return;
        }
        next();
      });
    },
  };
}

export default defineConfig({
  root: "frontend",
  publicDir: "public",
  plugins: [react(), ...(mockCatalog ? [mockCatalogPlugin()] : [])],
  build: {
    outDir: "../src/portmap/catalog_static",
    cssMinify: false,
    // Keep __init__.py so catalog_static remains included as package data.
    emptyOutDir: false,
    rollupOptions: {
      output: {
        entryFileNames: "assets/catalog.js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: (assetInfo) => {
          const name = assetInfo.name || "";
          if (name.endsWith(".css")) return "assets/catalog.css";
          return "assets/[name][extname]";
        },
      },
    },
  },
  server: {
    port: 5174,
    proxy: mockCatalog ? {} : {
      "/registry.json": {
        target: catalogTarget,
        changeOrigin: true,
      },
      "/actions": {
        target: catalogTarget,
        changeOrigin: true,
      },
      "/healthz": {
        target: catalogTarget,
        changeOrigin: true,
      },
      "/readyz": {
        target: catalogTarget,
        changeOrigin: true,
      },
    },
  },
});
