import { describe, expect, it } from "vitest";

import { parseCatalogSource } from "@/lib/catalogSource";
import type { AppSourceInfo } from "@/api/generated/models";

const BASE_SOURCE: AppSourceInfo = {
  type: "git",
  url: "https://github.com/Jizai-inc/palmimo-devkit",
  ref: "v1.0.0",
  ref_kind: "tag",
  subdir: null,
  commit: null,
  manifest: null,
};

describe("parseCatalogSource", () => {
  it("carries a non-default manifest filename through to the installable source", () => {
    const source = parseCatalogSource({ ...BASE_SOURCE, manifest: "palmimo.realtime.toml" });

    expect(source).toEqual({
      type: "git",
      url: BASE_SOURCE.url,
      ref: "v1.0.0",
      ref_kind: "tag",
      manifest: "palmimo.realtime.toml",
    });
  });

  it("omits manifest from the installable source when the catalog entry has none", () => {
    const source = parseCatalogSource(BASE_SOURCE);

    expect(source).not.toHaveProperty("manifest");
  });
});
