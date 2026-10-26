import { createFileRoute } from "@tanstack/react-router";

import { EnvPanel } from "@/components/EnvPanel";

export const Route = createFileRoute("/settings/secrets")({
  component: EnvPanel,
});
