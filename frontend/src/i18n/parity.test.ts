import { describe, expect, it } from "vitest";

import en from "./en.json";
import ja from "./ja.json";

function keyPaths(value: unknown, prefix = ""): string[] {
  if (typeof value !== "object" || value === null) return [prefix];
  return Object.entries(value).flatMap(([key, child]) => keyPaths(child, prefix ? `${prefix}.${key}` : key));
}

describe("i18n locale parity", () => {
  it("ja.json has exactly the same key paths as en.json", () => {
    expect(keyPaths(ja).sort()).toEqual(keyPaths(en).sort());
  });
});
