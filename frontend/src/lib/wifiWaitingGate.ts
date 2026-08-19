/**
 * Whether `/wifi/waiting` was reached without an `ssid` -- i.e. not from
 * `/wifi`'s own connect-form submit (routes/wifi.tsx, which always sets it),
 * but by a direct/stale navigation (bookmark, Back/Forward, typed URL).
 * `validateSearch` in routes/wifi.waiting.tsx already defaults a
 * missing/malformed `ssid` to `""`, so an empty string is the signal to check.
 *
 * Kept out of `src/routes/` so it's never mistaken for a route file by
 * TanStack Router's route-tree generator (which warns on any file under
 * `src/routes/` that doesn't export a `Route`).
 */
export function shouldRedirectToWifiScan(search: { ssid: string }): boolean {
  return search.ssid === "";
}
