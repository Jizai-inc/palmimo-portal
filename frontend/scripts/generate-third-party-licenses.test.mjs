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
// plus matching node_modules/<name>/{package.json,LICENSE} dirs, enough for
// the script to walk without touching the real project's node_modules.
// `nestedUnder: "<parent>"` installs a package only under the parent's own
// node_modules, to exercise Node-style upward resolution.
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
    writeFileSync(
      join(packageDir, "package.json"),
      JSON.stringify({ name, version: spec.version, license: spec.license, author: spec.author }),
    );
    for (const [fileName, contents] of Object.entries(spec.files ?? {})) {
      writeFileSync(join(packageDir, fileName), contents);
    }
    for (const dirName of spec.licenseDirs ?? []) mkdirSync(join(packageDir, dirName), { recursive: true });

    lockPackages[lockPath] = {
      version: spec.version,
      dev: Boolean(spec.dev),
      devOptional: spec.devOptional,
      dependencies: spec.dependencies ?? {},
      optionalDependencies: spec.optionalDependencies,
      peerDependencies: spec.peerDependencies,
      peerDependenciesMeta: spec.peerDependenciesMeta,
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

  function names(entries) {
    return entries.map((entry) => entry.name);
  }

  it("includes production and transitive-production deps, excludes dev-only ones", () => {
    const frontendDir = build({
      react: { version: "19.0.0", license: "MIT", files: { LICENSE: "x" }, dependencies: { "react-dep": "1.0.0" } },
      "react-dep": { version: "1.0.0", license: "MIT", files: { LICENSE: "x" }, isRootDependency: false },
      vitest: { version: "4.0.0", license: "MIT", dev: true, files: { LICENSE: "x" } },
    });

    const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir));

    expect(names(entries)).toEqual(expect.arrayContaining(["react", "react-dep"]));
    expect(names(entries)).not.toContain("vitest");
  });

  it("carries the full license file body into the rendered notice", () => {
    const frontendDir = build({ "pkg-a": { version: "1.0.0", license: "MIT", files: { LICENSE: "Permission..." } } });

    const notice = renderNotice(collectEntries(frontendDir, loadLockfile(frontendDir)).entries);

    expect(notice).toContain("=== pkg-a@1.0.0 — MIT ===");
    expect(notice).toContain("Permission...");
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

    expect(shadcn?.licenseText).toContain("MIT License (shadcn/ui)");
    expect(shadcn?.inBundle).toBe(true);
  });

  it("renders entries in deterministic, name-sorted order regardless of input order", () => {
    const frontendDir = build({
      zeta: { version: "1.0.0", license: "MIT", files: { LICENSE: "z" } },
      alpha: { version: "1.0.0", license: "MIT", files: { LICENSE: "a" } },
    });
    const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir));
    const notice = renderNotice(entries);

    expect(notice.indexOf("=== zeta@")).toBeGreaterThan(notice.indexOf("=== alpha@"));
    // renderNotice, not collection order, owns the sort.
    expect(renderNotice([...entries].reverse())).toBe(notice);
  });

  describe("license text fallback chain", () => {
    it("is fatal with a license field but no file, no substantive README, and no override", () => {
      const frontendDir = build({
        "pkg-no-text": {
          version: "1.0.0",
          license: "MIT",
          author: "Someone",
          files: { "README.md": "# pkg-no-text\n\n# License\nMIT\n" },
        },
      });

      const { entries, missingLicenseText } = collectEntries(frontendDir, loadLockfile(frontendDir));

      expect(missingLicenseText).toEqual(["pkg-no-text@1.0.0"]);
      expect(names(entries)).not.toContain("pkg-no-text");
    });

    it("falls back to author + a substantive README License section when no LICENSE file exists", () => {
      const body = "Permission is hereby granted, free of charge, to any person obtaining a copy...";
      const frontendDir = build({
        "pkg-readme": {
          version: "2.0.0",
          license: "MIT",
          author: "Jane Doe",
          files: { "README.md": `# pkg-readme\n\n## License\n${body}\n` },
        },
      });

      const { entries, missingLicenseText } = collectEntries(frontendDir, loadLockfile(frontendDir));

      expect(missingLicenseText).toEqual([]);
      const entry = entries.find((e) => e.name === "pkg-readme");
      expect(entry.licenseText).toContain("Copyright (c) Jane Doe");
      expect(entry.licenseText).toContain(body);
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

      expect(collectEntries(frontendDir, loadLockfile(frontendDir)).missingLicenseText).toEqual([
        "pkg-bare-readme@1.0.0",
      ]);
    });

    it("uses a checked-in override when no file or README text is available", () => {
      const frontendDir = build({ "pkg-override": { version: "3.0.0", license: "MIT" } });
      const overrides = {
        "pkg-override@3.0.0": { holder: "Override Holder", text: "Permission is hereby granted... (override)" },
      };

      const { entries, missingLicenseText } = collectEntries(frontendDir, loadLockfile(frontendDir), { overrides });

      expect(missingLicenseText).toEqual([]);
      const entry = entries.find((e) => e.name === "pkg-override");
      expect(entry.licenseText).toContain("Copyright (c) Override Holder");
      expect(entry.licenseText).toContain("override");
    });

    it("reads the referenced file for a SEE LICENSE IN <file> license field", () => {
      const frontendDir = build({
        "pkg-see-license-in": {
          version: "1.0.0",
          license: "SEE LICENSE IN CUSTOM-LICENSE.txt",
          files: { "CUSTOM-LICENSE.txt": "Custom license body text." },
        },
      });

      const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir));
      expect(entries.find((e) => e.name === "pkg-see-license-in").licenseText).toContain("Custom license body text.");
    });
  });

  describe("license file selection", () => {
    it("concatenates every matching license file (dual license) rather than picking one", () => {
      const frontendDir = build({
        "pkg-dual": {
          version: "1.0.0",
          license: "(MIT OR CC0-1.0)",
          files: { "LICENSE-MIT": "MIT license body", "LICENSE-CC0": "CC0 license body" },
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

      expect(findLicenseFile(join(frontendDir, "node_modules", "pkg-license-dir"))).toBe("the real license text");
    });
  });

  describe("Node-style dependency resolution", () => {
    it("resolves a dependency installed only nested under its parent, not hoisted to the top level", () => {
      const frontendDir = build({
        "pkg-a": { version: "1.0.0", license: "MIT", files: { LICENSE: "x" }, dependencies: { "pkg-nested": "2.0.0" } },
        "pkg-nested": { version: "2.0.0", license: "MIT", files: { LICENSE: "x" }, nestedUnder: "pkg-a" },
      });

      expect(resolveDependencyPath(loadLockfile(frontendDir), "node_modules/pkg-a", "pkg-nested")).toBe(
        "node_modules/pkg-a/node_modules/pkg-nested",
      );
      expect(names(collectEntries(frontendDir, loadLockfile(frontendDir)).entries)).toContain("pkg-nested");
    });

    it("throws when a required dependency cannot be resolved anywhere in the tree", () => {
      const frontendDir = build({
        "pkg-broken": { version: "1.0.0", license: "MIT", files: { LICENSE: "x" }, dependencies: { "pkg-missing": "1.0.0" } },
      });

      expect(() => collectEntries(frontendDir, loadLockfile(frontendDir))).toThrow(/pkg-missing/);
    });

    it("follows a required peerDependency that is actually installed", () => {
      const frontendDir = build({
        "pkg-a": { version: "1.0.0", license: "MIT", files: { LICENSE: "x" }, peerDependencies: { "pkg-peer": "1.0.0" } },
        "pkg-peer": { version: "1.0.0", license: "MIT", files: { LICENSE: "x" }, isRootDependency: false },
      });

      expect(names(collectEntries(frontendDir, loadLockfile(frontendDir)).entries)).toContain("pkg-peer");
    });

    it("silently skips an optional peerDependency that is not installed", () => {
      const frontendDir = build({
        "pkg-a": {
          version: "1.0.0",
          license: "MIT",
          files: { LICENSE: "x" },
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
          files: { LICENSE: "x" },
          optionalDependencies: { "pkg-opt-present": "1.0.0", "pkg-opt-absent": "1.0.0" },
        },
        "pkg-opt-present": { version: "1.0.0", license: "MIT", files: { LICENSE: "x" }, isRootDependency: false },
      });

      const found = names(collectEntries(frontendDir, loadLockfile(frontendDir)).entries);
      expect(found).toContain("pkg-opt-present");
      expect(found).not.toContain("pkg-opt-absent");
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

    it("includes a devDependency-only bundled package once, e.g. tailwindcss", () => {
      const frontendDir = build({ tailwindcss: { version: "4.0.0", license: "MIT", dev: true, files: { LICENSE: "x" } } });

      const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir), {
        bundledPackageNames: ["tailwindcss"],
      });

      const matches = entries.filter((e) => e.name === "tailwindcss");
      expect(matches).toHaveLength(1);
      expect(matches[0].inBundle).toBe(true);
    });

    it("marks a production dependency not present in the bundled set as in bundle: no", () => {
      const frontendDir = build({ "pkg-unbundled": { version: "1.0.0", license: "MIT", files: { LICENSE: "x" } } });

      const { entries } = collectEntries(frontendDir, loadLockfile(frontendDir), { bundledPackageNames: [] });

      expect(entries.find((e) => e.name === "pkg-unbundled").inBundle).toBe(false);
      expect(renderNotice(entries)).toContain("In bundle: no");
    });
  });

  it("extracts the package name from a lockfile path, scoped or not", () => {
    expect(packageNameFromPath("node_modules/pkg-a/node_modules/@scope/pkg-b")).toBe("@scope/pkg-b");
    expect(packageNameFromPath("node_modules/react")).toBe("react");
  });

  it("excludes a package the lockfile marks devOptional (dev + optional-peer only)", () => {
    const frontendDir = build({
      "pkg-a": {
        version: "1.0.0",
        license: "MIT",
        files: { LICENSE: "x" },
        peerDependencies: { "pkg-types": "1.0.0" },
        peerDependenciesMeta: { "pkg-types": { optional: true } },
      },
      "pkg-types": { version: "1.0.0", license: "MIT", files: { LICENSE: "x" }, isRootDependency: false, devOptional: true },
    });

    expect(names(collectEntries(frontendDir, loadLockfile(frontendDir)).entries)).not.toContain("pkg-types");
  });

  it("runs main() when invoked directly, even from a directory path containing a space", () => {
    const spacedRoot = mkdtempSync(join(tmpdir(), "third party licenses "));
    tempDirs.push(spacedRoot);

    // Copy just enough of the real frontend for the script to run for real:
    // itself, its overrides table, the vendored shadcn/ui LICENSE, and a
    // trivial lockfile with node_modules to match.
    const scriptsDir = join(spacedRoot, "scripts");
    mkdirSync(scriptsDir, { recursive: true });
    cpSync(SCRIPT_PATH, join(scriptsDir, "generate-third-party-licenses.mjs"));
    const overridesSrc = join(REAL_SCRIPTS_DIR, "third-party-license-overrides.json");
    if (existsSync(overridesSrc)) cpSync(overridesSrc, join(scriptsDir, "third-party-license-overrides.json"));

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

    expect(readFileSync(outputPath, "utf-8")).toContain("pkg-a license text");
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
      for (const match of notice.matchAll(/^=== (\S+)@[^\s]+ — .*? ===\n(?!Repository:)In bundle: yes/gm)) {
        inBundleYesNames.add(match[1]);
      }

      // Every bundled name the plugin recorded shows up as "in bundle: yes",
      // and nothing else does -- apart from the vendored shadcn/ui entry,
      // which has no node_modules module id for the plugin to observe.
      for (const name of bundledPackageNames) {
        expect(inBundleYesNames.has(name), `expected "${name}" to be rendered as "In bundle: yes"`).toBe(true);
      }
      for (const name of inBundleYesNames) {
        if (name.includes("shadcn/ui")) continue;
        expect(bundledPackageNames.has(name), `"${name}" is rendered "In bundle: yes" but not in .bundled-packages.json`).toBe(
          true,
        );
      }
    }, 120_000);
  });
});
