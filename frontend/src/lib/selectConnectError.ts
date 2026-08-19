import { PortalApiError } from "@/api/client";

/**
 * The bit of a TanStack Query mutation-cache entry this selector needs --
 * matches the shape `useMutationState`'s `select` returns from a
 * `useConnectApiV1WifiConnectPost` mutation's `state` (routes/wifi.waiting.tsx).
 * Kept narrow so this stays testable without a real mutation cache entry.
 */
export interface ConnectMutationSnapshot {
  error: unknown;
  /** The orval-generated connect hook's variables shape: `{ data: { ssid, psk } }`. */
  variables: { data?: { ssid?: string } } | undefined;
  /** Epoch ms the mutation was (most recently) submitted -- TanStack Query bumps this on every retry. */
  submittedAt: number;
}

/**
 * Picks out the connect-mutation error that belongs to *this* waiting
 * attempt, out of every connect-mutation entry TanStack Query's mutation
 * cache still holds (it never evicts a mutation just because a newer one started).
 *
 * Two things a raw "most recent error in the cache" read would get wrong:
 *
 * - **Wrong error type.** A fetch-level `TypeError` ("Failed to fetch" /
 *   "fetch failed") is the *expected* symptom of a successful
 *   hotspot-to-station transition -- the AP tears itself down mid-request
 *   (see palmimo-portal-technical.md's AP-disconnection-asymmetry section) --
 *   not a real connect failure. Only a {@link PortalApiError} (a real HTTP
 *   error response) is a genuine failure worth surfacing.
 * - **Wrong attempt.** A mutation left over from a previous connect attempt
 *   is still sitting in the cache with its own stale error. Only a mutation
 *   naming this exact `ssid` and whose `submittedAt` is at or after this
 *   attempt's own `submitted` timestamp (captured via `Date.now()` at form
 *   submit, routes/wifi.tsx) belongs to the attempt currently on screen.
 *
 * Returns the most recent matching error, or `undefined` if none match.
 */
export function selectConnectError(
  mutations: readonly ConnectMutationSnapshot[],
  ssid: string,
  submitted: number,
): PortalApiError | undefined {
  const matches = mutations.filter(
    (mutation): mutation is ConnectMutationSnapshot & { error: PortalApiError } =>
      mutation.error instanceof PortalApiError &&
      mutation.variables?.data?.ssid === ssid &&
      mutation.submittedAt >= submitted,
  );
  return matches[matches.length - 1]?.error;
}
