/** Zero-pads `n` to two digits, e.g. `5` -> `"05"`. */
function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/**
 * Formats a GitHub Release's `published_at` (ISO 8601) as `YYYY-MM-DD` for
 * the update screen's "latest release" row. Falls back to the raw string if
 * it doesn't parse.
 */
export function formatReleaseDate(publishedAt: string): string {
  const date = new Date(publishedAt);
  if (Number.isNaN(date.getTime())) {
    return publishedAt;
  }
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

/**
 * Formats `UpdateStatusResponse.checked_at` (a Unix timestamp in seconds,
 * or `null` before the first check) as `YYYY-MM-DD HH:mm` in the browser's
 * local time, for the update screen's "last checked" row.
 */
export function formatCheckedAt(checkedAt: number | null): string {
  if (checkedAt === null) {
    return "—";
  }
  const date = new Date(checkedAt * 1000);
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}
