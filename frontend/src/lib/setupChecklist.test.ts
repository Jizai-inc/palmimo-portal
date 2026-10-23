import { describe, expect, it } from "vitest";

import { checklistItems } from "@/lib/setupChecklist";
import { appSummary } from "@/test/appSummaryFixture";

const official = appSummary({ id: "palmimo.teleop", source: { ...appSummary().source, official: true } });

describe("checklistItems", () => {
  it("starts with nothing done", () => {
    expect(checklistItems({ apps: [], gitCredentialCount: 0 }).map((item) => item.done)).toEqual([false, false, false]);
  });

  it("marks Connect GitHub done once a git credential exists", () => {
    expect(checklistItems({ apps: [], gitCredentialCount: 1 })[0]).toEqual({ step: "connectGithub", done: true });
  });

  it("marks Try an official app done for an official install only", () => {
    const items = checklistItems({ apps: [official], gitCredentialCount: 0 });

    expect(items[1].done).toBe(true);
    expect(items[2].done).toBe(false);
  });

  it("marks Build your own app done for any non-official install", () => {
    const items = checklistItems({ apps: [appSummary({ source: { ...appSummary().source, type: "zip", url: null } })], gitCredentialCount: 0 });

    expect(items[1].done).toBe(false);
    expect(items[2].done).toBe(true);
  });
});
