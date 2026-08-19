import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createRouter } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./i18n";
import "./index.css";
import { DefaultErrorScreen } from "./components/DefaultErrorScreen";
import { queryClient } from "./lib/queryClient";
import { routeTree } from "./routeTree.gen";

// `defaultErrorComponent`: see src/components/DefaultErrorScreen.tsx's
// docstring for what reaching it means.
const router = createRouter({ routeTree, defaultPreload: "intent", defaultErrorComponent: DefaultErrorScreen });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("#root element not found");
}

createRoot(rootElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
