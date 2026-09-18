import { describe, expect, it } from "vitest";

import i18n from "@/i18n";
import { deviceLabel } from "@/lib/deviceLabel";

// Without this, a device id a newer backend added before the Portal shipped a label for it
// would render as an empty string instead of the raw id.
describe("deviceLabel", () => {
  it.each([
    ["camera", "Camera"],
    ["audio", "Microphone & speaker"],
    ["motor_display", "Motors & display"],
    ["lidar", "lidar"],
  ] as const)("labels %s as %s", (id, expected) => {
    expect(deviceLabel(i18n.t.bind(i18n), id)).toBe(expected);
  });
});
