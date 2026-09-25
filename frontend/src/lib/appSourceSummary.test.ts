import { describe, expect, it } from "vitest";

import { formatSourceSummary } from "@/lib/appSourceSummary";

describe("formatSourceSummary", () => {
  // the same manifest name apart by where their code actually came from.
  it("summarizes a git source as owner/repo, subdir, and ref", () => {
    expect(
      formatSourceSummary({ type: "git", url: "https://github.com/alice/teleop", subdir: "examples/teleop", ref: "main" }),
    ).toBe("git: alice/teleop (examples/teleop) @main");
  });

  it("omits the subdir segment when the source has none", () => {
    expect(formatSourceSummary({ type: "git", url: "https://github.com/alice/teleop", subdir: null, ref: "main" })).toBe(
      "git: alice/teleop @main",
    );
  });

  it("summarizes a zip source as plain 'zip'", () => {
    expect(formatSourceSummary({ type: "zip", url: null, subdir: null, ref: null })).toBe("zip");
  });
});
