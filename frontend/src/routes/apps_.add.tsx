import { createFileRoute, redirect } from "@tanstack/react-router";

/** Old add-app page: kept so bookmarks and doc links open the add-app dialog on the Apps page. */
export const Route = createFileRoute("/apps_/add")({
  beforeLoad: () => {
    throw redirect({ to: "/apps", search: { add: "github" }, replace: true });
  },
});
