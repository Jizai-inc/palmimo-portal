import type { TFunction } from "i18next";

/**
 * Translated label for one device id from an app's manifest (`camera`, `audio`,
 * `motor_display`), via explicit `t("devices.…")` calls so the i18n-parity static scan (see
 * `src/lib/navLabels.ts` for the same pattern) can see every key used here. Falls back to the
 * raw id for a value this build doesn't recognize -- a newer backend can add a device id before
 * the Portal ships a label for it.
 */
export function deviceLabel(t: TFunction, id: string): string {
  switch (id) {
    case "camera":
      return t("devices.camera");
    case "audio":
      return t("devices.audio");
    case "motor_display":
      return t("devices.motor_display");
    default:
      return id;
  }
}
