import { formatUtcTimestamp } from "@/lib/formatTimestamp";

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
