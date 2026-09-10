import { describe, expect, it } from "vitest";

import en from "./en.json";
import ja from "./ja.json";

/** Every leaf key of a nested translation object, as a dotted path. */
function keyPaths(value: unknown, prefix = ""): string[] {
  if (typeof value !== "object" || value === null) return [prefix];
  return Object.entries(value).flatMap(([key, child]) => keyPaths(child, prefix ? `${prefix}.${key}` : key));
}

describe("translations", () => {
  it("defines the same keys in every language", () => {
    const enKeys = keyPaths(en);
    const jaKeys = keyPaths(ja);

    expect(enKeys.filter((key) => !jaKeys.includes(key))).toEqual([]);
    expect(jaKeys.filter((key) => !enKeys.includes(key))).toEqual([]);
  });
});
