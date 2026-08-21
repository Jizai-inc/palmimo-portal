/**
 * Whether `/wifi/waiting` was reached other than through `/wifi`'s own connect-form submit
 * (routes/wifi.tsx, the only writer of both fields together) -- a direct/stale navigation
 * (bookmark, Back/Forward, hand-typed or shared URL) instead.
 * `validateSearch` in routes/wifi_.waiting.tsx already defaults a missing/malformed `ssid` to
 * `""` and `submitted` to `0`, so either is the signal to check: `ssid` alone is not enough --
 * `/wifi/waiting?ssid=x` supplies it without ever having submitted, and the root guard skips
 * its usual probe for this path (`authGate.ts`'s `shouldSkipAuthGate`), so this route-level
 * check is what stands between that link and rendering the waiting UI in a state (setup,
 * login, corrupt) the guard would otherwise have redirected out of.
 *
 * Kept out of `src/routes/` so it's never mistaken for a route file by
 * TanStack Router's route-tree generator (which warns on any file under
 * `src/routes/` that doesn't export a `Route`).
 */
export function shouldRedirectToWifiScan(search: { ssid: string; submitted: number }): boolean {
  return search.ssid === "" || search.submitted === 0;
}
