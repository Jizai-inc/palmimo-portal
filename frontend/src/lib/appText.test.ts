import { describe, expect, it } from "vitest";
import { appText } from "./appText";

describe("appText", () => {
  it("uses exact locale, then base language, then original text per field", () => {
    const translations = { ja: { description: "Base", "params.port": "Port translation" }, "ja-jp": { description: "Exact" } };
    expect(appText(translations, "ja-JP", "description", "Original")).toBe("Exact");
    expect(appText(translations, "ja-JP", "params.port", "Port")).toBe("Port translation");
    expect(appText(translations, "ja-JP", "env.KEY", "Key")).toBe("Key");
    expect(appText(translations, "en", "description", "Original")).toBe("Original");
    expect(appText(undefined, "ja", "description", "Original")).toBe("Original");
  });
});
