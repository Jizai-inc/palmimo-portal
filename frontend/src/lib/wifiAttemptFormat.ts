import type { TFunction } from "i18next";

import type { WifiAttemptInfo } from "@/api/generated/models";
import { formatUtcTimestamp } from "@/lib/formatTimestamp";

/**
 * Renders the Wi-Fi settings screen's "last connection" row
 * (components/WifiSettingsPanel.tsx) via the `wifiSettings.lastAttempt*`
 * locale keys, or "—" when there is no attempt on record.
 *
 * Pure function (not a hook) so it's unit-testable against a fixed
 * `WifiAttemptInfo` without rendering a component.
 */
export function formatLastWifiAttempt(attempt: WifiAttemptInfo | null | undefined, t: TFunction): string {
  if (!attempt) {
    return "—";
  }
  if (attempt.result === "attempting") {
    return t("wifiSettings.lastAttemptConnecting");
  }
  const datetime = formatUtcTimestamp(attempt.timestamp, { withYear: false });
  if (attempt.result === "connected") {
    return t("wifiSettings.lastAttemptConnected", { datetime });
  }
  return t("wifiSettings.lastAttemptFailed", { datetime });
}
