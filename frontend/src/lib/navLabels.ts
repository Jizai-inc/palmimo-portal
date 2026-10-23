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
    case "nav.apps":
      return t("nav.apps");
    case "nav.settings":
      return t("nav.settings");
    case "nav.network":
      return t("nav.network");
    case "nav.secrets":
      return t("nav.secrets");
    case "nav.softwareUpdate":
      return t("nav.softwareUpdate");
    case "nav.device":
      return t("nav.device");
    case "nav.developer":
      return t("nav.developer");
    default:
      return labelKey;
  }
}

export function navDescription(t: TFunction, descriptionKey: NavItem["descriptionKey"]): string | undefined {
  switch (descriptionKey) {
    case "nav.appsDescription":
      return t("nav.appsDescription");
    case "nav.networkDescription":
      return t("nav.networkDescription");
    case "nav.secretsDescription":
      return t("nav.secretsDescription");
    case "nav.softwareUpdateDescription":
      return t("nav.softwareUpdateDescription");
    case "nav.deviceDescription":
      return t("nav.deviceDescription");
    case "nav.developerDescription":
      return t("nav.developerDescription");
    default:
      return undefined;
  }
}
