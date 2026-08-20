/** How often {@link startPortalProbe} retries reaching the device's own `.local` origin. */
const DEFAULT_INTERVAL_MS = 3_000;

/**
 * Probes `http://<hostname>.local/` every `intervalMs` until reachable,
 * then calls `onFound()` once and stops. `{mode: "no-cors"}`: the
 * wifi-waiting screen is served cross-origin from the setup AP, which
 * won't send CORS headers, and the body isn't needed -- the promise
 * resolves on reachability and rejects on any network error.
 *
 * **Fail-first rule**: while still on Palmimo's own setup AP, its
 * captive-portal DNS resolves *every* hostname to its own gateway, so a
 * probe would false-positive immediately. A probe is only trusted once an
 * earlier probe on this run has *failed* -- which happens only once the AP
 * has actually gone down or the device re-associated elsewhere.
 *
 * Probes run sequentially via `setTimeout` (never `setInterval`), each with
 * its own `AbortSignal.timeout(intervalMs)` so a hung handshake (AP
 * mid-teardown) counts as a failure rather than blocking the fail-first
 * flag forever (mirrors PowerPanel.tsx's reboot poller). Returns a
 * `stop()`; callers must invoke it on cleanup so probing doesn't outlive
 * the screen that started it.
 */
export function startPortalProbe(hostname: string, onFound: () => void, intervalMs: number = DEFAULT_INTERVAL_MS): () => void {
  let stopped = false;
  let hasFailed = false;
  let timeoutId: ReturnType<typeof setTimeout> | undefined;

  function stop() {
    stopped = true;
    clearTimeout(timeoutId);
  }

  async function tick() {
    if (stopped) return;
    try {
      await fetch(`http://${hostname}.local/`, { mode: "no-cors", cache: "no-store", signal: AbortSignal.timeout(intervalMs) });
      if (stopped) return;
      if (hasFailed) {
        // Stop before calling out, so a slow `onFound` can't race a later tick into firing twice.
        stop();
        onFound();
        return;
      }
      // Still on the captive AP (fail-first rule) -- keep probing.
    } catch {
      // Not reachable yet, or a probe timed out against a hung handshake.
      if (!stopped) {
        hasFailed = true;
      }
    }
    if (!stopped) {
      timeoutId = setTimeout(() => void tick(), intervalMs);
    }
  }

  void tick();

  return stop;
}
