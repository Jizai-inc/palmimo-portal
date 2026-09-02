import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Deliberately separate from vite.config.ts: the TanStack Router plugin
// generates src/routeTree.gen.ts from the route file tree, and Tailwind's
// Vite plugin processes CSS -- neither is relevant to (or safe to run
// during) a unit-test pass, so this config only wires up what tests need
// (the React plugin, for JSX, and the `@` alias mirrored from
// tsconfig.app.json / vite.config.ts).
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": new URL("./src", import.meta.url).pathname,
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    // `scripts/**/*.test.mjs` covers build-time Node scripts (e.g.
    // generate-third-party-licenses.mjs) -- they need no jsdom environment,
    // but a separate vitest project just for them would be overkill for one
    // script; the jsdom environment above is a no-op for plain Node code.
    include: ["src/**/*.test.{ts,tsx}", "scripts/**/*.test.mjs"],
  },
});
