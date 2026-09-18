import type { TFunction } from "i18next";

/**
 * Subtitle for the full-page app logs view: which invocation is shown and, while the current
 * invocation of a running app is on screen, the refresh cadence. No start time: the logs API
 * returns a tail, so the earliest loaded line is not the run's start.
 */
export function appLogsSubtitle(
  t: TFunction,
  { isCurrentInvocation, isLive }: { isCurrentInvocation: boolean; isLive: boolean },
): string {
  if (!isCurrentInvocation) return t("appDetail.logsPageSubtitlePrevious");
  return isLive ? t("appDetail.logsPageSubtitleCurrentLive") : t("appDetail.logsPageSubtitleCurrent");
}
