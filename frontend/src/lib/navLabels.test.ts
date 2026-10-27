import { describe, expect, it } from "vitest";

import i18n from "@/i18n";
import { NAV_ITEMS, SETTINGS_ITEMS } from "@/lib/navigation";
import { navDescription, navLabel } from "@/lib/navLabels";

describe("navLabel/navDescription", () => {
  const items = [...NAV_ITEMS, ...SETTINGS_ITEMS];

  it.each(items)("resolves a translation for $labelKey, not the raw key", (item) => {
    expect(navLabel(i18n.t, item.labelKey)).not.toBe(item.labelKey);
  });

  it.each(items.filter((item) => item.descriptionKey))("resolves a translation for $descriptionKey, not the raw key", (item) => {
    expect(navDescription(i18n.t, item.descriptionKey)).not.toBe(item.descriptionKey);
  });
});
