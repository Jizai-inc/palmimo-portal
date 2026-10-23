import { describe, expect, it } from "vitest";

import { attentionItems } from "@/lib/homeAttention";
import { appSummary } from "@/test/appSummaryFixture";

describe("attentionItems", () => {
  it("is empty when nothing needs attention", () => {
    expect(attentionItems({ apps: [appSummary()], platformReady: true, portalUpdateAvailable: false })).toEqual([]);
  });

  it("lists problems before updates", () => {
    const apps = [
      appSummary({ id: "a.one", name: "one", update_available: true }),
      appSummary({ id: "a.two", name: "two", broken_reason: "venv_missing", credential_rejected: true }),
    ];

    const kinds = attentionItems({ apps, platformReady: false, portalUpdateAvailable: true }).map((item) => item.kind);

    expect(kinds).toEqual(["appBroken", "credentialRejected", "platformUpdate", "appUpdate", "portalUpdate"]);
  });

  it("does not flag the platform while its readiness is unknown", () => {
    expect(attentionItems({ apps: [], platformReady: undefined, portalUpdateAvailable: undefined })).toEqual([]);
  });

  it("flags an app with a missing required key and a default-app conflict", () => {
    const items = attentionItems({
      apps: [appSummary({ name: "robot", missing_env: ["API_KEY"] })],
      platformReady: true,
      portalUpdateAvailable: false,
      autostartConflict: ["a.one", "a.two"],
    });

    expect(items).toEqual([
      { kind: "missingKeys", appId: "alice.robot", appName: "robot" },
      { kind: "chooseDefault" },
    ]);
  });
});
