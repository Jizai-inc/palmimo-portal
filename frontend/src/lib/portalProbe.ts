/** How often {@link startPortalProbe} retries reaching the device's own `.local` origin. */
const DEFAULT_INTERVAL_MS = 3_000;

/**
 * Probes `http://<hostname>.local/` every `intervalMs` until it is
 * reachable, then calls `onFound()` once and stops.
 *
 * Uses `{mode: "no-cors"}` because the wifi-waiting screen is served
 * cross-origin from the setup AP: a normal `fetch` would need CORS headers
 * this static server won't send, and the response body isn't needed anyway.
 * The promise resolves once the host answers and rejects on a network
 * error (DNS failure, connection refused) -- exactly the
 * reachable/not-yet-reachable signal needed.
 *
 * **Fail-first rule**: while still associated with Palmimo's own setup AP,
 * that AP's captive-portal DNS resolves *every* hostname to its own gateway,
 * so a probe to `<hostname>.local` would resolve immediately against the AP
 * itself -- a false positive that would fire `onFound()` before Palmimo has
 * switched networks. A probe is therefore only trusted once at least one
 * earlier probe on this run has *failed*, which only happens once the AP
 * has actually gone down or the device has re-associated elsewhere.
 *
 * Probes run sequentially via `setTimeout` (never `setInterval`) so a slow
 * probe can't overlap the next tick. Each probe carries its own
 * `AbortSignal.timeout(intervalMs)` so a hung TCP handshake (AP mid-teardown)
 * counts as a failure instead of blocking forever -- otherwise the fail-first
 * flag would never flip (mirrors PowerPanel.tsx's reboot poller).
 *
 * Returns a `stop()` function; callers must invoke it on cleanup/unmount so
 * probing doesn't outlive the screen that started it.
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
