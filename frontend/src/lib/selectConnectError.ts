import { PortalApiError } from "@/api/client";

/** Narrow shape of a TanStack Query mutation-cache entry, kept testable without a real cache entry. */
export interface ConnectMutationSnapshot {
  error: unknown;
  variables: { data?: { ssid?: string } } | undefined;
  /** Epoch ms of the (most recent) submission -- TanStack Query bumps this on every retry. */
  submittedAt: number;
}

/**
 * Picks the connect-mutation error belonging to *this* waiting attempt, out of every entry
 * TanStack Query's mutation cache holds (it never evicts one just because a newer one started).
 * A fetch-level `TypeError` is the *expected* symptom of a successful hotspot-to-station
 * transition (the AP tears itself down mid-request), not a real failure -- only a
 * {@link PortalApiError} counts. A stale mutation from a previous attempt is excluded via
 * `submittedAt >= submitted` (captured at form submit).
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
