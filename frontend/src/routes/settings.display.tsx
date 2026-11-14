import { createFileRoute } from "@tanstack/react-router";

import { DisplayAssetsPanel } from "@/components/DisplayAssetsPanel";

export const Route = createFileRoute("/settings/display")({
  component: DisplayAssetsPanel,
});
