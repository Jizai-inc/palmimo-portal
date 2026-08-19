import { QueryClient } from "@tanstack/react-query";

// One client for the whole app -- imported directly by route modules
// (including `beforeLoad`s) rather than threaded through router context.
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Default no-retry so a 401/403 from the auth gate surfaces immediately
      // instead of retrying a request the server will keep rejecting.
      // (system/status polling sets its own refetchInterval in routes/__root.tsx.)
      retry: false,
    },
  },
});
