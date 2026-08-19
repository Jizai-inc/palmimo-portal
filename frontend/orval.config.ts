import { defineConfig } from "orval";

// FastAPI (Pydantic models) -> openapi.json -> here -> src/api/generated/:
// typed fetchers + TanStack Query hooks + MSW mocks, all generated -- see
// palmimo-portal-technical.md's "API client autogeneration" section and
// ../Makefile's `generate` target. Committed as a build artifact; never
// hand-edited.
export default defineConfig({
  portal: {
    input: "openapi.json",
    output: {
      mode: "tags-split",
      target: "src/api/generated",
      schemas: "src/api/generated/models",
      client: "react-query",
      httpClient: "fetch",
      clean: true,
      mock: { generators: [{ type: "msw" }] },
      override: {
        mutator: { path: "src/api/client.ts", name: "customFetch" },
        // Our mutator resolves straight to the parsed response body (see
        // client.ts), not orval's default `{data, status, headers}`
        // wrapper -- match the generated types to what it actually returns.
        fetch: { includeHttpResponseReturnType: false },
      },
    },
  },
});
