import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRootRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";

/**
 * `render(ui)` wrapped in a fresh {@link QueryClient} per call, so tests
 * don't share `src/lib/queryClient.ts`'s app-wide singleton and leak cached
 * query state between cases. `retry: false` mirrors the singleton so a
 * stubbed error resolves on the first try.
 *
 * Returns `rerender` bound to the same `QueryClient` (so re-rendering with
 * new props keeps cache state, like a real re-render) and the `queryClient`
 * itself (so a test can assert on cache-hygiene behavior).
 */
export function renderWithProviders(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  const result = render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);

  return {
    ...result,
    queryClient,
    rerender: (nextUi: ReactElement) => result.rerender(<QueryClientProvider client={queryClient}>{nextUi}</QueryClientProvider>),
  };
}

/**
 * Like {@link renderWithProviders}, plus an in-memory router so a tree that
 * reaches `AppHeader`'s wordmark `Link` (e.g. via `AuthShell`/`AppShell`)
 * resolves instead of throwing. The router's initial load is async, so
 * callers must query with `findBy*`, not the synchronous `getBy*`.
 */
export function renderWithRouter(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  const rootRoute = createRootRoute({
    component: () => <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>,
  });
  const router = createRouter({ routeTree: rootRoute, history: createMemoryHistory({ initialEntries: ["/"] }) });

  const result = render(<RouterProvider router={router} />);

  return { ...result, queryClient, router };
}
