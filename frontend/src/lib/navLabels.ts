import type { TFunction } from "i18next";

import type { NavItem } from "@/lib/navigation";

/**
 * Resolves a {@link NavItem}'s label/description through explicit
 * `t("nav.…")` calls rather than the dynamic `t(item.labelKey)` callers
 * would otherwise write: the i18n-parity contract
 * (test_i18n_parity.py's `test_every_locale_key_is_used_somewhere`)
 * statically scans for literal-string `t()` calls, and a dynamic call is
 * invisible to it -- this is the one place that turns keys back into
 * literal calls the scan can see.
 */
export function navLabel(t: TFunction, labelKey: NavItem["labelKey"]): string {
  switch (labelKey) {
    case "nav.dashboard":
      return t("nav.dashboard");
    case "nav.wifi":
      return t("nav.wifi");
    case "nav.sshKeys":
      return t("nav.sshKeys");
    case "nav.power":
      return t("nav.power");
    case "nav.update":
      return t("nav.update");
    default:
      return labelKey;
  }
}

export function navDescription(t: TFunction, descriptionKey: NavItem["descriptionKey"]): string | undefined {
  switch (descriptionKey) {
    case "nav.wifiDescription":
      return t("nav.wifiDescription");
    case "nav.sshKeysDescription":
      return t("nav.sshKeysDescription");
    case "nav.powerDescription":
      return t("nav.powerDescription");
    case "nav.updateDescription":
      return t("nav.updateDescription");
    default:
      return undefined;
  }
}
