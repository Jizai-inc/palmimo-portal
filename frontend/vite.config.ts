import { tanstackRouter } from "@tanstack/router-plugin/vite";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The Portal ships as static files served by the FastAPI backend
// (see palmimo_portal/app.py's SPA-fallback mount), not by a Node server, so
// this config has two jobs: a `dev` server that proxies /api to the backend
// on :8080 for local development, and a `build` output that lands exactly at
// ../palmimo_portal/static, where app.py serves it from and the release
// workflow tars it up as a GitHub Release asset (see doc/guides/releasing.md
// -- this output is not committed).
export default defineConfig({
  plugins: [
    // Must run before @vitejs/plugin-react: it generates src/routeTree.gen.ts
    // from the src/routes/ file tree before the React plugin ever sees it.
    tanstackRouter({ target: "react", autoCodeSplitting: false }),
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: {
      "@": new URL("./src", import.meta.url).pathname,
    },
  },
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8080",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "../palmimo_portal/static",
    emptyOutDir: true,
    // No sourcemaps: they would double the committed artifact for no
    // runtime benefit on a device image, and a source map's suffix is not
    // one this tree's comment-language ratchet has ever had to classify.
    sourcemap: false,
    rollupOptions: {
      output: {
        // The drift gate (`make check`) commits this build output and diffs
        // it against a fresh rebuild. Vite's default asset names embed a
        // content hash, which is intentionally deterministic (same input
        // bytes -> same hash) but Rollup's default chunking can still vary
        // module iteration order across environments. Fixed, unhashed names
        // keep the diff meaningful (a real content change, not chunking
        // noise) — cache-busting does not matter here since the backend
        // serves one pinned build per release, not a rolling deployment.
        entryFileNames: "assets/[name].js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name][extname]",
      },
    },
  },
});
