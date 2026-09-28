import type { TFunction } from "i18next";
import { describe, expect, it } from "vitest";

import { appLogsSubtitle } from "@/lib/appLogsSubtitle";

const t = ((key: string) => key) as unknown as TFunction;

describe("appLogsSubtitle", () => {
  it.each([
    [{ isCurrentInvocation: true, isLive: true }, "appDetail.logsPageSubtitleCurrentLive"],
    [{ isCurrentInvocation: true, isLive: false }, "appDetail.logsPageSubtitleCurrent"],
    [{ isCurrentInvocation: false, isLive: true }, "appDetail.logsPageSubtitlePrevious"],
  ])("claims a refresh cadence only for the live current run: %o", (input, key) => {
    expect(appLogsSubtitle(t, input)).toBe(key);
  });
});
