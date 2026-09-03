import { writeFileSync } from "node:fs";
import { resolve as resolvePath } from "node:path";
import { tanstackRouter } from "@tanstack/router-plugin/vite";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

// The Portal ships as static files served by the FastAPI backend
// (see palmimo_portal/app.py's SPA-fallback mount), not by a Node server, so
// this config has two jobs: a `dev` server that proxies /api to the backend
// on :8080 for local development, and a `build` output that lands exactly at
// ../palmimo_portal/static, where app.py serves it from and the release
// workflow tars it up as a GitHub Release asset (see doc/guides/releasing.md
// -- this output is not committed).

// `@tailwindcss/vite` compiles Tailwind's CSS (including the `tailwindcss`
// package's own preflight reset, which lands straight in index.css) through
// its own internal pipeline rather than through Rollup's module graph -- it
// never calls resolveId/load/transform/moduleParsed for
// `node_modules/tailwindcss/...` at all (verified empirically: instrumenting
// every one of those hooks during a real build never sees it), so there is
// no plugin-API hook that could observe it generically the way
// `moduleParsed` observes an ordinary JS/CSS import below. Since this config
// is the one place that knows `tailwindcss()` is active, and Tailwind always
// inlines that reset whenever it is, its inclusion is asserted here rather
// than detected.
const ALWAYS_BUNDLED_PACKAGE_NAMES = ["tailwindcss"];

// Records which npm packages actually contributed a module to the build
// (not merely "resolvable from package.json", which frontend/scripts/
// generate-third-party-licenses.mjs's lockfile-closure walk already covers
// on its own) and writes their names to <outDir>/.bundled-packages.json.
// This exists because the lockfile closure alone misses a devDependency
// whose *output* still ships (see ALWAYS_BUNDLED_PACKAGE_NAMES above).
// `moduleParsed` sees every module Rollup includes in the graph, dev-only or
// not; `closeBundle` runs once the bundle is finalized.
function recordBundledPackagesPlugin(): Plugin {
  const packageNames = new Set<string>(ALWAYS_BUNDLED_PACKAGE_NAMES);
  let outDir = "";
  let root = "";

  return {
    name: "record-bundled-packages",
    apply: "build",
    configResolved(config) {
      root = config.root;
      outDir = config.build.outDir;
    },
    moduleParsed(moduleInfo) {
      const marker = "node_modules/";
      const markerIndex = moduleInfo.id.lastIndexOf(marker);
      if (markerIndex === -1) return;

      const rest = moduleInfo.id.slice(markerIndex + marker.length);
      const [first, second] = rest.split("/");
      if (!first) return;
      const name = first.startsWith("@") && second ? `${first}/${second}` : first;
      packageNames.add(name);
    },
    closeBundle() {
      const outputPath = resolvePath(root, outDir, ".bundled-packages.json");
      writeFileSync(outputPath, `${JSON.stringify([...packageNames].sort(), null, 2)}\n`, "utf-8");
    },
  };
}

export default defineConfig({
  plugins: [
    // Must run before @vitejs/plugin-react: it generates src/routeTree.gen.ts
    // from the src/routes/ file tree before the React plugin ever sees it.
    tanstackRouter({ target: "react", autoCodeSplitting: false }),
    react(),
    tailwindcss(),
    recordBundledPackagesPlugin(),
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
