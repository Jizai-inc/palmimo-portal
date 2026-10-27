import type { AppSummary, WifiAttemptInfo } from "@/api/generated/models";

export type ActivityItem =
  | { kind: "appJob"; appId: string; appName: string; job: "install" | "update" | "delete"; ok: boolean; at: number }
  | { kind: "wifi"; ssid: string; ok: boolean; at: number };

const MAX_ITEMS = 5;
const JOB_KINDS = new Set(["install", "update", "delete"]);

/** Each app's last finished job plus the last settled Wi-Fi attempt, newest first. */
export function recentActivity({
  apps,
  lastWifiAttempt,
}: {
  apps: readonly AppSummary[];
  lastWifiAttempt: WifiAttemptInfo | null | undefined;
}): ActivityItem[] {
  const items: ActivityItem[] = [];
  for (const app of apps) {
    const job = app.last_job;
    if (!job || job.finished_at === null || job.state === "running" || !JOB_KINDS.has(job.kind)) continue;
    items.push({
      kind: "appJob",
      appId: app.id,
      appName: app.name,
      job: job.kind as "install" | "update" | "delete",
      ok: job.state === "done",
      at: job.finished_at,
    });
  }
  if (lastWifiAttempt && lastWifiAttempt.result !== "attempting") {
    items.push({ kind: "wifi", ssid: lastWifiAttempt.ssid, ok: lastWifiAttempt.result === "connected", at: lastWifiAttempt.timestamp });
  }
  return items.sort((a, b) => b.at - a.at).slice(0, MAX_ITEMS);
}
