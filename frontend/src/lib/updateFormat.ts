/** Zero-pads `n` to two digits, e.g. `5` -> `"05"`. */
function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/**
 * Formats a Unix timestamp (seconds) in UTC with an explicit `UTC` suffix --
 * the one home for every timestamp this screen (and any screen reusing this
 * helper) renders, so a device with no RTC and an operator in an unknown
 * timezone never has to guess which clock a time is in.
 */
export function formatUtcTimestamp(timestampSeconds: number, { withTime = true }: { withTime?: boolean } = {}): string {
  const date = new Date(timestampSeconds * 1000);
  const datePart = `${date.getUTCFullYear()}-${pad2(date.getUTCMonth() + 1)}-${pad2(date.getUTCDate())}`;
  if (!withTime) {
    return `${datePart} UTC`;
  }
  return `${datePart} ${pad2(date.getUTCHours())}:${pad2(date.getUTCMinutes())} UTC`;
}

/**
 * Formats a GitHub Release's `published_at` (ISO 8601) as `YYYY-MM-DD UTC`
 * for the update screen's "latest release" row. Falls back to the raw
 * string if it doesn't parse.
 */
export function formatReleaseDate(publishedAt: string): string {
  const date = new Date(publishedAt);
  if (Number.isNaN(date.getTime())) {
    return publishedAt;
  }
  return formatUtcTimestamp(date.getTime() / 1000, { withTime: false });
}

/**
 * Formats `UpdateStatusResponse.checked_at` (a Unix timestamp in seconds,
 * or `null` before the first check) as `YYYY-MM-DD HH:mm UTC` for the
 * update screen's "last checked" row.
 */
export function formatCheckedAt(checkedAt: number | null): string {
  if (checkedAt === null) {
    return "—";
  }
  return formatUtcTimestamp(checkedAt);
}
