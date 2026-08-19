import { setupServer } from "msw/node";

// Shared MSW server instance; test files layer per-case handlers via
// `server.use(...)` (see src/test/setup.ts for the listen/reset/close
// lifecycle). No default handlers here -- `onUnhandledRequest: "error"`
// makes an unstubbed request fail loudly instead of hitting the network.
export const server = setupServer();
