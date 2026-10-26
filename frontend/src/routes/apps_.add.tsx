import { createFileRoute, redirect } from "@tanstack/react-router";

/** Legacy `/apps/add` URL: opens the add-app dialog on the Apps page. */
export const Route = createFileRoute("/apps_/add")({
  beforeLoad: () => {
    throw redirect({ to: "/apps", search: { add: "github" }, replace: true });
  },
});
