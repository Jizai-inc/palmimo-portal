/** Zero-pads `n` to two digits, e.g. `5` -> `"05"`. */
function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

export interface FormatUtcTimestampOptions {
  /** Include the year in the date portion. Default `true`. */
  withYear?: boolean;
  /** Include the `HH:mm` time portion. Default `true`. */
  withTime?: boolean;
}

/**
 * Formats a Unix timestamp (seconds) in UTC with an explicit `UTC` suffix --
 * the one home for every absolute timestamp this app renders, so a device
 * with no RTC and an operator in an unknown timezone never has to guess
 * which clock a time is in. Callers pick their own date shape
 * (`withYear`/`withTime`); the UTC rendering and suffix are not optional.
 */
export function formatUtcTimestamp(timestampSeconds: number, { withYear = true, withTime = true }: FormatUtcTimestampOptions = {}): string {
  const date = new Date(timestampSeconds * 1000);
  const month = pad2(date.getUTCMonth() + 1);
  const day = pad2(date.getUTCDate());
  const datePart = withYear ? `${date.getUTCFullYear()}-${month}-${day}` : `${month}-${day}`;
  if (!withTime) {
    return `${datePart} UTC`;
  }
  return `${datePart} ${pad2(date.getUTCHours())}:${pad2(date.getUTCMinutes())} UTC`;
}
