import { execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";

import {
  collectEntries,
  findLicenseFile,
  loadLockfile,
  packageNameFromPath,
  renderNotice,
  resolveDependencyPath,
} from "./generate-third-party-licenses.mjs";

const SCRIPT_PATH = fileURLToPath(import.meta.url).replace(/test\.mjs$/, "mjs");
const REAL_SCRIPTS_DIR = dirname(SCRIPT_PATH);
const REAL_FRONTEND_DIR = dirname(REAL_SCRIPTS_DIR);

// Builds a minimal fake frontend/ directory: a package-lock.json (v3 shape)
// plus matching node_modules/<name>/{package.json,LICENSE} directories --
// enough for the script to walk without touching the real project's
// node_modules or lockfile.
//
// `packages` values may set `nestedUnder: "<parent-name>"` to install the
// package only at node_modules/<parent-name>/node_modules/<name> (not
// top-level) -- for exercising Node-style upward resolution instead of a
// flat lookup.
function makeFakeFrontend(packages) {
  const frontendDir = mkdtempSync(join(tmpdir(), "third-party-licenses-test-"));
  mkdirSync(join(frontendDir, "src", "components", "ui"), { recursive: true });
  writeFileSync(join(frontendDir, "src", "components", "ui", "LICENSE"), "MIT License (shadcn/ui)\n");

  const lockPackages = { "": { dependencies: {} } };
  for (const [name, spec] of Object.entries(packages)) {
    const lockPath = spec.nestedUnder
      ? `node_modules/${spec.nestedUnder}/node_modules/${name}`
      : `node_modules/${name}`;
    const packageDir = join(frontendDir, lockPath);
    mkdirSync(packageDir, { recursive: true });
    const packageJson = {
      name,
      version: spec.version,
      license: spec.license,
      licenses: spec.licenses,
      repository: spec.repository,
      author: spec.author,
    };
    writeFileSync(join(packageDir, "package.json"), JSON.stringify(packageJson));

    for (const [fileName, contents] of Object.entries(spec.files ?? {})) {
      writeFileSync(join(packageDir, fileName), contents);
    }
    if (spec.licenseDirs) {
      for (const dirName of spec.licenseDirs) mkdirSync(join(packageDir, dirName), { recursive: true });
    }

    lockPackages[lockPath] = {
      version: spec.version,
      dev: Boolean(spec.dev),
      dependencies: spec.dependencies ?? {},
      optionalDependencies: spec.optionalDependencies ?? undefined,
      peerDependencies: spec.peerDependencies ?? undefined,
      peerDependenciesMeta: spec.peerDependenciesMeta ?? undefined,
    };

    if (spec.isRootDependency !== false && !spec.nestedUnder) {
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
      react: { version: "19.0.0", license: "MIT", files: { LICENSE: "react license" } },
      vitest: { version: "4.0.0", license: "MIT", dev: true, files: { LICENSE: "vitest license" } },
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
        files: { LICENSE: "pkg-a license" },
        dependencies: { "pkg-b": "1.0.0" },
      },
      "pkg-b": {
        version: "1.0.0",
        license: "MIT",
        files: { LICENSE: "pkg-b license" },
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
        files: { LICENSE: "Permission is hereby granted, free of charge..." },
      },
    });

    const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir));
    const notice = renderNotice(entries);

    expect(notice).toContain("=== pkg-a@1.0.0 — MIT ===");
    expect(notice).toContain("Permission is hereby granted, free of charge...");
  });

  it("reports every package missing a license field, not just the first", () => {
    const frontendDir = build({
      "pkg-unlicensed-1": { version: "1.0.0", license: undefined },
      "pkg-unlicensed-2": { version: "1.0.0", license: undefined },
      "pkg-fine": { version: "1.0.0", license: "MIT", files: { LICENSE: "ok" } },
    });

    const { missingLicenseField } = collectEntries(frontendDir, loadLockfile(frontendDir));

    expect(missingLicenseField.sort()).toEqual(["pkg-unlicensed-1@1.0.0", "pkg-unlicensed-2@1.0.0"]);
  });

  it("always includes the vendored shadcn/ui entry, marked in-bundle", () => {
    const frontendDir = build({});

    const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir));

    const shadcn = entries.find((entry) => entry.name.includes("shadcn/ui"));
    expect(shadcn).toBeDefined();
    expect(shadcn.licenseText).toContain("MIT License (shadcn/ui)");
    expect(shadcn.inBundle).toBe(true);
  });

  it("renders entries in deterministic, name-sorted order regardless of input order", () => {
    const frontendDir = build({
      zeta: { version: "1.0.0", license: "MIT", files: { LICENSE: "z" } },
      alpha: { version: "1.0.0", license: "MIT", files: { LICENSE: "a" } },
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

  describe("license text fallback chain", () => {
    it("is fatal when a package has a license field but no file, no substantive README, and no override", () => {
      const frontendDir = build({
        "pkg-no-text": {
          version: "1.0.0",
          license: "MIT",
          author: "Someone <someone@example.com>",
          files: { "README.md": "# pkg-no-text\n\n# License\nMIT\n" },
        },
      });

      const { entries, missingLicenseText } = collectEntries(frontendDir, loadLockfile(frontendDir));

      expect(missingLicenseText).toEqual(["pkg-no-text@1.0.0"]);
      expect(entries.map((entry) => entry.name)).not.toContain("pkg-no-text");
    });

    it("falls back to author + a substantive README License section when no LICENSE file exists", () => {
      const licenseBody =
        "Permission is hereby granted, free of charge, to any person obtaining a copy of this software...";
      const frontendDir = build({
        "pkg-readme-license": {
          version: "2.0.0",
          license: "MIT",
          author: "Jane Doe <jane@example.com>",
          files: { "README.md": `# pkg-readme-license\n\n## License\n${licenseBody}\n` },
        },
      });

      const { entries, missingLicenseText } = collectEntries(frontendDir, loadLockfile(frontendDir));

      expect(missingLicenseText).toEqual([]);
      const entry = entries.find((e) => e.name === "pkg-readme-license");
      expect(entry.licenseText).toContain("Copyright (c) Jane Doe");
      expect(entry.licenseText).toContain(licenseBody);
    });

    it("rejects a README License section that just restates the SPDX identifier", () => {
      const frontendDir = build({
        "pkg-bare-readme": {
          version: "1.0.0",
          license: "MIT",
          author: "Someone",
          files: { "README.md": "# pkg-bare-readme\n\n# License\nMIT\n" },
        },
      });

      const { missingLicenseText } = collectEntries(frontendDir, loadLockfile(frontendDir));
      expect(missingLicenseText).toEqual(["pkg-bare-readme@1.0.0"]);
    });

    it("uses a checked-in override when no file or README text is available", () => {
      const frontendDir = build({
        "pkg-override": {
          version: "3.0.0",
          license: "MIT",
        },
      });

      const overrides = {
        "pkg-override@3.0.0": {
          holder: "Override Holder",
          license: "MIT",
          text: "Permission is hereby granted... (override body)",
        },
      };

      const { entries, missingLicenseText } = collectEntries(frontendDir, loadLockfile(frontendDir), {
        overrides,
      });

      expect(missingLicenseText).toEqual([]);
      const entry = entries.find((e) => e.name === "pkg-override");
      expect(entry.licenseText).toContain("Copyright (c) Override Holder");
      expect(entry.licenseText).toContain("override body");
    });

    it("reads the referenced file for a SEE LICENSE IN <file> license field", () => {
      const frontendDir = build({
        "pkg-see-license-in": {
          version: "1.0.0",
          license: "SEE LICENSE IN CUSTOM-LICENSE.txt",
          files: { "CUSTOM-LICENSE.txt": "Custom license body text." },
        },
      });

      const { entries, missingLicenseText } = collectEntries(frontendDir, loadLockfile(frontendDir));
      expect(missingLicenseText).toEqual([]);
      const entry = entries.find((e) => e.name === "pkg-see-license-in");
      expect(entry.licenseText).toContain("Custom license body text.");
    });
  });

  describe("license file selection", () => {
    it("concatenates every matching license file (dual license) rather than picking one", () => {
      const frontendDir = build({
        "pkg-dual": {
          version: "1.0.0",
          license: "(MIT OR CC0-1.0)",
          files: {
            "LICENSE-MIT": "MIT license body",
            "LICENSE-CC0": "CC0 license body",
          },
        },
      });

      const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir));
      const entry = entries.find((e) => e.name === "pkg-dual");
      expect(entry.licenseText).toContain("MIT license body");
      expect(entry.licenseText).toContain("CC0 license body");
    });

    it("ignores a same-named licenses/ directory when selecting license files", () => {
      const frontendDir = build({
        "pkg-license-dir": {
          version: "1.0.0",
          license: "MIT",
          files: { LICENSE: "the real license text" },
          licenseDirs: ["licenses"],
        },
      });

      const packageDir = join(frontendDir, "node_modules", "pkg-license-dir");
      const text = findLicenseFile(packageDir);
      expect(text).toBe("the real license text");
    });
  });

  describe("Node-style dependency resolution", () => {
    it("resolves a dependency installed only nested under its parent, not hoisted to the top level", () => {
      const frontendDir = build({
        "pkg-a": {
          version: "1.0.0",
          license: "MIT",
          files: { LICENSE: "pkg-a license" },
          dependencies: { "pkg-nested": "2.0.0" },
        },
        "pkg-nested": {
          version: "2.0.0",
          license: "MIT",
          files: { LICENSE: "pkg-nested license" },
          nestedUnder: "pkg-a",
        },
      });

      expect(
        resolveDependencyPath(loadLockfile(frontendDir), "node_modules/pkg-a", "pkg-nested"),
      ).toBe("node_modules/pkg-a/node_modules/pkg-nested");

      const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir));
      expect(entries.map((entry) => entry.name)).toContain("pkg-nested");
    });

    it("throws when a required dependency cannot be resolved anywhere in the tree", () => {
      const frontendDir = build({
        "pkg-broken": {
          version: "1.0.0",
          license: "MIT",
          files: { LICENSE: "x" },
          dependencies: { "pkg-missing": "1.0.0" },
        },
      });

      expect(() => collectEntries(frontendDir, loadLockfile(frontendDir))).toThrow(/pkg-missing/);
    });

    it("follows a required peerDependency that is actually installed", () => {
      const frontendDir = build({
        "pkg-a": {
          version: "1.0.0",
          license: "MIT",
          files: { LICENSE: "pkg-a license" },
          peerDependencies: { "pkg-peer": "1.0.0" },
        },
        "pkg-peer": {
          version: "1.0.0",
          license: "MIT",
          files: { LICENSE: "pkg-peer license" },
          isRootDependency: false,
        },
      });

      const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir));
      expect(entries.map((entry) => entry.name)).toContain("pkg-peer");
    });

    it("silently skips an optional peerDependency that is not installed", () => {
      const frontendDir = build({
        "pkg-a": {
          version: "1.0.0",
          license: "MIT",
          files: { LICENSE: "pkg-a license" },
          peerDependencies: { "pkg-optional-peer": "1.0.0" },
          peerDependenciesMeta: { "pkg-optional-peer": { optional: true } },
        },
      });

      expect(() => collectEntries(frontendDir, loadLockfile(frontendDir))).not.toThrow();
    });

    it("follows an optionalDependency that is installed and skips one that is not", () => {
      const frontendDir = build({
        "pkg-a": {
          version: "1.0.0",
          license: "MIT",
          files: { LICENSE: "pkg-a license" },
          optionalDependencies: { "pkg-opt-present": "1.0.0", "pkg-opt-absent": "1.0.0" },
        },
        "pkg-opt-present": {
          version: "1.0.0",
          license: "MIT",
          files: { LICENSE: "present" },
          isRootDependency: false,
        },
      });

      const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir));
      const names = entries.map((entry) => entry.name);
      expect(names).toContain("pkg-opt-present");
      expect(names).not.toContain("pkg-opt-absent");
    });
  });

  describe("bundle-membership union", () => {
    it("errors when the bundled set names a package that cannot be resolved in the lockfile", () => {
      const frontendDir = build({});

      const { unresolvedBundled } = collectEntries(frontendDir, loadLockfile(frontendDir), {
        bundledPackageNames: ["ghost-package"],
      });

      expect(unresolvedBundled).toEqual(["ghost-package"]);
    });

    it("includes a devDependency-only package that is present in the bundled set (e.g. tailwindcss)", () => {
      const frontendDir = build({
        tailwindcss: {
          version: "4.0.0",
          license: "MIT",
          dev: true,
          files: { LICENSE: "tailwind license" },
        },
      });

      const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir), {
        bundledPackageNames: ["tailwindcss"],
      });

      const entry = entries.find((e) => e.name === "tailwindcss");
      expect(entry).toBeDefined();
      expect(entry.inBundle).toBe(true);
    });

    it("marks a production dependency not present in the bundled set as in bundle: no", () => {
      const frontendDir = build({
        "pkg-unbundled": { version: "1.0.0", license: "MIT", files: { LICENSE: "x" } },
      });

      const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir), {
        bundledPackageNames: [],
      });

      const entry = entries.find((e) => e.name === "pkg-unbundled");
      expect(entry.inBundle).toBe(false);
      expect(renderNotice(entries)).toContain("In bundle: no");
    });

    it("marks every entry's bundle status unknown when no .bundled-packages.json was found", () => {
      const frontendDir = build({
        "pkg-a": { version: "1.0.0", license: "MIT", files: { LICENSE: "x" } },
      });

      const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir), {
        bundledPackageNames: null,
      });

      const entry = entries.find((e) => e.name === "pkg-a");
      expect(entry.inBundle).toBeNull();
      expect(renderNotice(entries)).toContain("In bundle: unknown");
    });
  });

  describe("packageNameFromPath", () => {
    it("extracts a scoped package name from a nested lockfile path", () => {
      expect(packageNameFromPath("node_modules/pkg-a/node_modules/@scope/pkg-b")).toBe("@scope/pkg-b");
    });

    it("extracts the plain name from a top-level lockfile path", () => {
      // Regression: an earlier implementation split on "/node_modules/"
      // (leading slash required), which only a *nested* path contains --
      // a top-level path like "node_modules/react" has no "/" before its
      // "node_modules/", so it came back completely unsplit.
      expect(packageNameFromPath("node_modules/react")).toBe("react");
    });
  });

  describe("bundle-union deduplication", () => {
    it("does not duplicate a top-level production dependency that is also in the bundled set", () => {
      const frontendDir = build({
        react: { version: "19.0.0", license: "MIT", files: { LICENSE: "react license" } },
      });

      const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir), {
        bundledPackageNames: ["react"],
      });

      expect(entries.filter((entry) => entry.name === "react")).toHaveLength(1);
    });
  });

  describe("devOptional handling", () => {
    it("excludes a package the lockfile marks devOptional (dev + optional-peer only)", () => {
      const frontendDir = build({
        "pkg-a": {
          version: "1.0.0",
          license: "MIT",
          files: { LICENSE: "pkg-a license" },
          peerDependencies: { "pkg-types": "1.0.0" },
          peerDependenciesMeta: { "pkg-types": { optional: true } },
        },
        "pkg-types": {
          version: "1.0.0",
          license: "MIT",
          files: { LICENSE: "pkg-types license" },
          isRootDependency: false,
          devOptional: true,
        },
      });

      // makeFakeFrontend doesn't thread a bare `devOptional` flag through by
      // itself (only `dev`) -- patch the lockfile entry directly to match
      // real npm output (`dev` is absent, only `devOptional: true` is set).
      const lockfilePath = join(frontendDir, "package-lock.json");
      const lockfile = JSON.parse(readFileSync(lockfilePath, "utf-8"));
      lockfile.packages["node_modules/pkg-types"].devOptional = true;
      writeFileSync(lockfilePath, JSON.stringify(lockfile));

      const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir));
      expect(entries.map((entry) => entry.name)).not.toContain("pkg-types");
    });
  });

  describe("main() guard", () => {
    it("runs main() when invoked directly even from a directory path containing a space", () => {
      const spacedRoot = mkdtempSync(join(tmpdir(), "third party licenses "));
      tempDirs.push(spacedRoot);

      // Copy just enough of the real frontend for the script to run for
      // real: itself, its overrides table, the vendored shadcn/ui LICENSE,
      // and a trivial lockfile with node_modules to match.
      const scriptsDir = join(spacedRoot, "scripts");
      mkdirSync(scriptsDir, { recursive: true });
      cpSync(SCRIPT_PATH, join(scriptsDir, "generate-third-party-licenses.mjs"));
      const overridesSrc = join(REAL_SCRIPTS_DIR, "third-party-license-overrides.json");
      if (existsSync(overridesSrc)) {
        cpSync(overridesSrc, join(scriptsDir, "third-party-license-overrides.json"));
      }

      mkdirSync(join(spacedRoot, "src", "components", "ui"), { recursive: true });
      writeFileSync(join(spacedRoot, "src", "components", "ui", "LICENSE"), "MIT License (shadcn/ui)\n");

      const packageDir = join(spacedRoot, "node_modules", "pkg-a");
      mkdirSync(packageDir, { recursive: true });
      writeFileSync(join(packageDir, "package.json"), JSON.stringify({ name: "pkg-a", version: "1.0.0", license: "MIT" }));
      writeFileSync(join(packageDir, "LICENSE"), "pkg-a license text");

      writeFileSync(
        join(spacedRoot, "package-lock.json"),
        JSON.stringify({
          name: "frontend",
          lockfileVersion: 3,
          packages: {
            "": { dependencies: { "pkg-a": "1.0.0" } },
            "node_modules/pkg-a": { version: "1.0.0", dependencies: {} },
          },
        }),
      );

      const outputPath = join(spacedRoot, "out", "THIRD_PARTY_LICENSES.txt");
      execFileSync(process.execPath, [join(scriptsDir, "generate-third-party-licenses.mjs"), outputPath], {
        cwd: spacedRoot,
      });

      expect(existsSync(outputPath)).toBe(true);
      const contents = readFileSync(outputPath, "utf-8");
      expect(contents).toContain("pkg-a license text");
    });
  });

  // Runs the real `vite build` against the real project, then checks that
  // the "in bundle: yes" set the generator renders is exactly the package
  // set vite.config.ts's bundled-packages plugin wrote out -- unit tests
  // above exercise the union/fallback logic against fake fixtures, but only
  // a real build proves the plugin and the generator agree on what a real
  // bundle actually contains. Slow (a real build), so it's skippable via
  // CI_FAST for a fast local loop; full CI should still run it.
  describe.skipIf(process.env.CI_FAST)("real vite build (slow)", () => {
    it("agrees with vite.config.ts's .bundled-packages.json on bundle membership", () => {
      const outputRelativePath = "../palmimo_portal/static/THIRD_PARTY_LICENSES.txt";
      execFileSync(join(REAL_FRONTEND_DIR, "node_modules", ".bin", "vite"), ["build"], { cwd: REAL_FRONTEND_DIR });
      execFileSync(process.execPath, [SCRIPT_PATH, outputRelativePath], { cwd: REAL_FRONTEND_DIR });

      const staticDir = join(REAL_FRONTEND_DIR, "..", "palmimo_portal", "static");
      const bundledPackageNames = new Set(
        JSON.parse(readFileSync(join(staticDir, ".bundled-packages.json"), "utf-8")),
      );
      const notice = readFileSync(join(staticDir, "THIRD_PARTY_LICENSES.txt"), "utf-8");

      const inBundleYesNames = new Set();
      for (const match of notice.matchAll(/^=== (\S+)@[^\s]+ — .*? ===\nRepository:.*\nIn bundle: yes/gm)) {
        inBundleYesNames.add(match[1]);
      }
      // Entries without a Repository line (the vendored shadcn/ui entry)
      // also need matching -- match those separately rather than
      // complicating the regex above.
      for (const match of notice.matchAll(/^=== (\S+)@[^\s]+ — .*? ===\n(?!Repository:)In bundle: yes/gm)) {
        inBundleYesNames.add(match[1]);
      }

      // Every bundled package name the plugin recorded must show up as
      // "in bundle: yes" in the rendered notice -- the vendored shadcn/ui
      // entry is the one legitimate exception, since it has no
      // node_modules module id for the plugin to have observed.
      for (const name of bundledPackageNames) {
        expect(inBundleYesNames.has(name), `expected "${name}" to be rendered as "In bundle: yes"`).toBe(true);
      }
      // And nothing is rendered "in bundle: yes" that the plugin didn't
      // actually observe, apart from that same vendored exception.
      for (const name of inBundleYesNames) {
        if (name.includes("shadcn/ui")) continue;
        expect(bundledPackageNames.has(name), `"${name}" is rendered "In bundle: yes" but not in .bundled-packages.json`).toBe(
          true,
        );
      }
    }, 120_000);
  });
});
