import type { TFunction } from "i18next";

import type { WifiAttemptInfo } from "@/api/generated/models";

/** Zero-pads `n` to two digits, e.g. `5` -> `"05"`. */
function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** Formats a Unix timestamp (seconds) as `MM-DD HH:mm` in the browser's local time. */
function formatDateTime(timestampSeconds: number): string {
  const date = new Date(timestampSeconds * 1000);
  return `${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

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
  const datetime = formatDateTime(attempt.timestamp);
  if (attempt.result === "connected") {
    return t("wifiSettings.lastAttemptConnected", { datetime });
  }
  return t("wifiSettings.lastAttemptFailed", { datetime });
}
