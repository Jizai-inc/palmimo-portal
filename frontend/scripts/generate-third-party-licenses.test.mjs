import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { collectEntries, loadLockfile, renderNotice } from "./generate-third-party-licenses.mjs";

// Builds a minimal fake frontend/ directory: a package-lock.json (v3 shape)
// plus matching node_modules/<name>/{package.json,LICENSE} directories --
// enough for the script to walk without touching the real project's
// node_modules or lockfile.
function makeFakeFrontend(packages) {
  const frontendDir = mkdtempSync(join(tmpdir(), "third-party-licenses-test-"));
  mkdirSync(join(frontendDir, "src", "components", "ui"), { recursive: true });
  writeFileSync(join(frontendDir, "src", "components", "ui", "LICENSE"), "MIT License (shadcn/ui)\n");

  const lockPackages = { "": { dependencies: {} } };
  for (const [name, spec] of Object.entries(packages)) {
    const packageDir = join(frontendDir, "node_modules", name);
    mkdirSync(packageDir, { recursive: true });
    writeFileSync(
      join(packageDir, "package.json"),
      JSON.stringify({ name, version: spec.version, license: spec.license, repository: spec.repository }),
    );
    if (spec.licenseFileName) {
      writeFileSync(join(packageDir, spec.licenseFileName), spec.licenseText ?? "");
    }
    lockPackages[`node_modules/${name}`] = {
      version: spec.version,
      dev: Boolean(spec.dev),
      dependencies: spec.dependencies ?? {},
    };
    if (spec.isRootDependency !== false) {
      lockPackages[""].dependencies[name] = spec.version;
    }
  }
  writeFileSync(
    join(frontendDir, "package-lock.json"),
    JSON.stringify({ name: "frontend", lockfileVersion: 3, packages: lockPackages }),
  );

  return frontendDir;
}

describe("generate-third-party-licenses", () => {
  const tempDirs = [];

  afterEach(() => {
    while (tempDirs.length > 0) rmSync(tempDirs.pop(), { recursive: true, force: true });
  });

  function build(packages) {
    const frontendDir = makeFakeFrontend(packages);
    tempDirs.push(frontendDir);
    return frontendDir;
  }

  it("includes only production dependencies, not dev-only ones", () => {
    const frontendDir = build({
      react: { version: "19.0.0", license: "MIT", licenseFileName: "LICENSE", licenseText: "react license" },
      vitest: { version: "4.0.0", license: "MIT", dev: true, licenseFileName: "LICENSE", licenseText: "vitest license" },
    });

    const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir));
    const names = entries.map((entry) => entry.name);

    expect(names).toContain("react");
    expect(names).not.toContain("vitest");
  });

  it("includes a transitive production dependency reached through another production package", () => {
    const frontendDir = build({
      "pkg-a": {
        version: "1.0.0",
        license: "MIT",
        licenseFileName: "LICENSE",
        licenseText: "pkg-a license",
        dependencies: { "pkg-b": "1.0.0" },
      },
      "pkg-b": {
        version: "1.0.0",
        license: "MIT",
        licenseFileName: "LICENSE",
        licenseText: "pkg-b license",
        isRootDependency: false,
      },
    });

    const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir));
    expect(entries.map((entry) => entry.name)).toContain("pkg-b");
  });

  it("carries the full license file body into the rendered notice", () => {
    const frontendDir = build({
      "pkg-a": {
        version: "1.0.0",
        license: "MIT",
        licenseFileName: "LICENSE",
        licenseText: "Permission is hereby granted, free of charge...",
      },
    });

    const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir));
    const notice = renderNotice(entries);

    expect(notice).toContain("=== pkg-a@1.0.0 — MIT ===");
    expect(notice).toContain("Permission is hereby granted, free of charge...");
  });

  it("records a package with no license file under the without-license-file summary", () => {
    const frontendDir = build({
      "pkg-no-file": { version: "1.0.0", license: "MIT" },
    });

    const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir));
    const notice = renderNotice(entries);

    expect(entries.find((entry) => entry.name === "pkg-no-file").licenseText).toBeNull();
    expect(notice).toContain("Packages without a license file (license field only):");
    expect(notice).toContain("- pkg-no-file@1.0.0 (MIT)");
  });

  it("reports every package missing a license field, not just the first", () => {
    const frontendDir = build({
      "pkg-unlicensed-1": { version: "1.0.0", license: undefined },
      "pkg-unlicensed-2": { version: "1.0.0", license: undefined },
      "pkg-fine": { version: "1.0.0", license: "MIT", licenseFileName: "LICENSE", licenseText: "ok" },
    });

    const { missingLicenseField } = collectEntries(frontendDir, loadLockfile(frontendDir));

    expect(missingLicenseField.sort()).toEqual(["pkg-unlicensed-1@1.0.0", "pkg-unlicensed-2@1.0.0"]);
  });

  it("always includes the vendored shadcn/ui entry", () => {
    const frontendDir = build({});

    const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir));

    const shadcn = entries.find((entry) => entry.name.includes("shadcn/ui"));
    expect(shadcn).toBeDefined();
    expect(shadcn.licenseText).toContain("MIT License (shadcn/ui)");
  });

  it("renders entries in deterministic, name-sorted order regardless of input order", () => {
    const frontendDir = build({
      zeta: { version: "1.0.0", license: "MIT", licenseFileName: "LICENSE", licenseText: "z" },
      alpha: { version: "1.0.0", license: "MIT", licenseFileName: "LICENSE", licenseText: "a" },
    });

    const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir));
    const notice = renderNotice(entries);

    const alphaIndex = notice.indexOf("=== alpha@");
    const zetaIndex = notice.indexOf("=== zeta@");
    expect(alphaIndex).toBeGreaterThan(-1);
    expect(zetaIndex).toBeGreaterThan(alphaIndex);

    // Re-rendering the same entries in a different array order must produce
    // byte-identical output -- renderNotice, not collection order, owns the
    // sort.
    const shuffled = [...entries].reverse();
    expect(renderNotice(shuffled)).toBe(notice);
  });
});
