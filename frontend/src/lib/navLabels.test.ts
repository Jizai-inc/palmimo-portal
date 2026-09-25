import { describe, expect, it } from "vitest";

import i18n from "@/i18n";
import { NAV_ITEMS } from "@/lib/navigation";
import { navDescription, navLabel } from "@/lib/navLabels";

// A NAV_ITEMS entry with no matching case in navLabel/navDescription's switch falls back to the
// raw key string (e.g. "nav.gitCredentials" shown verbatim in the drawer) instead of a
// translation -- silent because nothing throws, so only a check against every declared item
// catches an entry the switch statement was never updated for.
describe("navLabel/navDescription", () => {
  it.each(NAV_ITEMS)("resolves a translation for $labelKey, not the raw key", (item) => {
    expect(navLabel(i18n.t, item.labelKey)).not.toBe(item.labelKey);
  });

  it.each(NAV_ITEMS.filter((item) => item.descriptionKey))("resolves a translation for $descriptionKey, not the raw key", (item) => {
    expect(navDescription(i18n.t, item.descriptionKey)).not.toBe(item.descriptionKey);
  });
});
