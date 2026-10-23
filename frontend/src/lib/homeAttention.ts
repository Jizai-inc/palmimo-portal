import type { AppSummary } from "@/api/generated/models";

export type AttentionItem =
  | { kind: "appBroken"; appId: string; appName: string }
  | { kind: "credentialRejected"; appId: string; appName: string }
  | { kind: "missingKeys"; appId: string; appName: string }
  | { kind: "chooseDefault" }
  | { kind: "appUpdate"; appId: string; appName: string }
  | { kind: "platformUpdate" }
  | { kind: "portalUpdate" };

/** Problems first, then available updates; each app contributes at most one item per kind. */
export function attentionItems({
  apps,
  platformReady,
  portalUpdateAvailable,
  autostartConflict = [],
}: {
  apps: readonly AppSummary[];
  autostartConflict?: readonly string[];
  platformReady: boolean | undefined;
  portalUpdateAvailable: boolean | undefined;
}): AttentionItem[] {
  const items: AttentionItem[] = [];
  for (const app of apps) {
    if (app.broken_reason) items.push({ kind: "appBroken", appId: app.id, appName: app.name });
  }
  for (const app of apps) {
    if (app.credential_rejected) items.push({ kind: "credentialRejected", appId: app.id, appName: app.name });
  }
  for (const app of apps) {
    if (app.missing_env && app.missing_env.length > 0) items.push({ kind: "missingKeys", appId: app.id, appName: app.name });
  }
  if (autostartConflict.length > 0) items.push({ kind: "chooseDefault" });
  if (platformReady === false) items.push({ kind: "platformUpdate" });
  for (const app of apps) {
    if (app.update_available) items.push({ kind: "appUpdate", appId: app.id, appName: app.name });
  }
  if (portalUpdateAvailable) items.push({ kind: "portalUpdate" });
  return items;
}
