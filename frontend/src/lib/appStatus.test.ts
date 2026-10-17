import { describe, expect, it } from "vitest";

import i18n from "@/i18n";
import { appStatusLabel, shouldPollAppStatus } from "@/lib/appStatus";

describe("shouldPollAppStatus", () => {
  it.each(["running", "starting", "stopping", "installing", "updating", "deleting"])("polls an app that is %s", (status) => {
    expect(shouldPollAppStatus(status)).toBe(true);
  });

  it.each(["stopped", "failed", "needs_repair", "broken"])("does not poll an app that is %s", (status) => {
    expect(shouldPollAppStatus(status)).toBe(false);
  });
});

// exit-code sentence with an empty/undefined placeholder instead of falling back to the plain
// "Failed" label.
describe("appStatusLabel", () => {
  it.each([
    [5, "Failed (exit code 5)"],
    [null, "Failed"],
    [undefined, "Failed"],
  ] as const)("formats a failed app with exit code %s as %s", (exitCode, expected) => {
    expect(appStatusLabel(i18n.t.bind(i18n), "failed", exitCode)).toBe(expected);
  });
});
