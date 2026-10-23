import type { AppSummary } from "@/api/generated/models";

export type ChecklistStep = "connectGithub" | "tryOfficialApp" | "buildOwnApp";

export interface ChecklistItem {
  step: ChecklistStep;
  done: boolean;
}

/**
 * "Build your own app" counts any non-official install (own GitHub repo or zip): the template
 * repo URL is still a stand-in (`TEMPLATE_REPO_URL`), so an exact source match can't be made yet.
 */
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
