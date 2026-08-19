// Regenerates src/routeTree.gen.ts before `tsc -b` runs.
//
// `routeTree.gen.ts` is gitignored (it is derived, purely mechanical output
// -- see src/routeTree.gen.ts's own "@generated" banner) and is normally
// produced as a side effect of the `@tanstack/router-plugin` Vite plugin
// configured in vite.config.ts. That plugin only runs inside a Vite
// build/dev pass, but `npm run build` type-checks with `tsc -b` *before*
// calling `vite build` (a deliberate fast-fail: catch type errors before
// spending time bundling) -- so on a fresh clone, with no routeTree.gen.ts
// yet on disk, `tsc -b` fails on every route import
// (`Cannot find module './routeTree.gen'`) before Vite ever gets the chance
// to generate it.
//
// This script drives the same `@tanstack/router-generator` the Vite plugin
// itself uses, with the same options (see the `tanstackRouter(...)` call in
// vite.config.ts), so the file exists before `tsc -b` looks for it. Keep
// these options identical to vite.config.ts's -- a mismatch would make this
// step generate a route tree the later `vite build` pass immediately
// regenerates differently, which is exactly the kind of drift `make check`
// (packages/palmimo_portal/Makefile) exists to catch.
import { Generator, getConfig } from "@tanstack/router-generator";

const config = getConfig({ target: "react", autoCodeSplitting: false }, process.cwd());
const generator = new Generator({ config, root: process.cwd() });
await generator.run();
