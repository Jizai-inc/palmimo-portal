import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, beforeEach } from "vitest";

import i18n from "@/i18n";

import { server } from "./server";
import { stubMatchMedia } from "./matchMedia";

// `@testing-library/react` auto-registers `afterEach(cleanup)` only when it
// detects a global `afterEach`; this project runs vitest without
// `test.globals`, so register it explicitly to unmount DOM trees between tests.
afterEach(cleanup);

// jsdom has no `matchMedia`; default every test to a mobile viewport (no
// query matches) so `AppShell`'s drawer tests keep working without each one
// stubbing it. A test exercising desktop behavior calls `stubMatchMedia(true)`
// itself to override this before rendering.
beforeEach(() => stubMatchMedia(false));

// `i18next-browser-languagedetector` picks a language from the jsdom
// environment; force English once here so tests asserting on rendered copy
// don't depend on jsdom's default.
beforeAll(async () => {
  await i18n.changeLanguage("en");
});

// `onUnhandledRequest: "error"` makes an unstubbed request fail loudly
// instead of hitting the real network. `resetHandlers` after each test drops
// per-test `server.use(...)` overrides so tests don't leak handlers.
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
