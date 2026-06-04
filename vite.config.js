import { defineConfig } from "vite";

const catalogTarget = process.env.PORTMAP_CATALOG_TARGET || "http://127.0.0.1:80";

export default defineConfig({
  root: "frontend",
  publicDir: "public",
  build: {
    outDir: "../src/portmap/catalog_static",
    // Keep __init__.py so catalog_static remains included as package data.
    emptyOutDir: false,
    minify: false,
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
    proxy: {
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
