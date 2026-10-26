import type { AppSummary } from "@/api/generated/models";

export type ChecklistStep = "connectGithub" | "tryOfficialApp" | "buildOwnApp";

export interface ChecklistItem {
  step: ChecklistStep;
  done: boolean;
}

/** "Build your own app" counts any non-official install: an app made from the template lives at the owner's own URL, so it can't be matched against `TEMPLATE_REPO_URL`. */
export function checklistItems({
  apps,
  gitCredentialCount,
}: {
  apps: readonly AppSummary[];
  gitCredentialCount: number;
}): ChecklistItem[] {
  return [
    { step: "connectGithub", done: gitCredentialCount > 0 },
    { step: "tryOfficialApp", done: apps.some((app) => app.source.official) },
    { step: "buildOwnApp", done: apps.some((app) => !app.source.official) },
  ];
}
