import { describe, expect, it } from "vitest";

import { PortalApiError } from "@/api/client";
import type { ConnectMutationSnapshot } from "@/lib/selectConnectError";
import { selectConnectError } from "@/lib/selectConnectError";

function snapshot(overrides: Partial<ConnectMutationSnapshot>): ConnectMutationSnapshot {
  return {
    error: new PortalApiError(503, "network_backend_unavailable", {}),
    variables: { data: { ssid: "Home Wi-Fi" } },
    submittedAt: 100,
    ...overrides,
  };
}

describe("selectConnectError", () => {
  it("excludes a TypeError-shaped error even when it is present in the mutation cache", () => {
    // Only PortalApiError is ever considered -- a fetch-level TypeError is
    // the expected symptom of a successful hotspot-to-station transition.
    const mutations = [snapshot({ error: new TypeError("Failed to fetch") })];

    expect(selectConnectError(mutations, "Home Wi-Fi", 100)).toBeUndefined();
  });

  it("excludes a mutation for a different ssid", () => {
    const mutations = [snapshot({ variables: { data: { ssid: "Neighbor's Wi-Fi" } } })];

    expect(selectConnectError(mutations, "Home Wi-Fi", 100)).toBeUndefined();
  });

  it("excludes a stale mutation submitted before this attempt (pre-retry)", () => {
    const mutations = [snapshot({ submittedAt: 50 })];

    expect(selectConnectError(mutations, "Home Wi-Fi", 100)).toBeUndefined();
  });

  it("selects a matching-ssid, matching-or-later-submittedAt PortalApiError mutation", () => {
    const error = new PortalApiError(503, "network_backend_unavailable", {});
    const mutations = [snapshot({ error, submittedAt: 100 })];

    expect(selectConnectError(mutations, "Home Wi-Fi", 100)).toBe(error);
  });

  it("selects the most recent matching error when several qualify", () => {
    const stale = new PortalApiError(503, "network_backend_unavailable", {});
    const fresh = new PortalApiError(500, "internal_error", {});
    const mutations = [snapshot({ error: stale, submittedAt: 100 }), snapshot({ error: fresh, submittedAt: 200 })];

    expect(selectConnectError(mutations, "Home Wi-Fi", 100)).toBe(fresh);
  });

  it("ignores a mutation with no variables at all", () => {
    const mutations = [snapshot({ variables: undefined })];

    expect(selectConnectError(mutations, "Home Wi-Fi", 100)).toBeUndefined();
  });
});
