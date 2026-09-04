// Collects license text for every third-party package that ends up in the
// built frontend bundle, for attribution when the bundle ships as a release
// asset (see Makefile's `build` target). Scope is the production dependency
// closure from package-lock.json, unioned with whatever vite.config.ts's
// bundled-packages plugin recorded as actually bundled. Requires
// node_modules to be installed (`npm ci`); writes the result to argv[2].
import { readFileSync, readdirSync, mkdirSync, realpathSync, writeFileSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const SCRIPT_FRONTEND_DIR = resolve(SCRIPT_DIR, "..");
const BUNDLED_PACKAGES_FILE_NAME = ".bundled-packages.json";

const LICENSE_FILE_PATTERN = /^(licen[sc]e|copying)/i;
const README_FILE_PATTERN = /^readme(\.md)?$/i;
// Minimum length for README license text to count as real permission text
// rather than a one-word restating of the `license` field.
const MIN_SUBSTANTIVE_LICENSE_TEXT_LENGTH = 40;

// shadcn/ui is vendored by hand (see THIRD_PARTY_NOTICES.md), so it has no
// lockfile entry -- attribute it directly instead.
function vendoredEntries(frontendDir) {
  return [
    {
      name: "shadcn/ui (frontend/src/components/ui)",
      version: "vendored",
      license: "MIT",
      repository: "https://ui.shadcn.com",
      licenseText: readFileSync(join(frontendDir, "src", "components", "ui", "LICENSE"), "utf-8"),
      // Vendored source is copied straight into src/, so it is bundled by
      // construction -- there is no node_modules module id for the
      // bundled-packages plugin (see vite.config.ts) to have observed.
      inBundle: true,
    },
  ];
}

function loadLockfile(frontendDir) {
  const lockfilePath = join(frontendDir, "package-lock.json");
  const lockfile = JSON.parse(readFileSync(lockfilePath, "utf-8"));
  if (!lockfile.packages) {
    throw new Error(`${lockfilePath} has no "packages" map -- expected npm lockfileVersion 3`);
  }
  return lockfile;
}

// Checked-in fallback license text table (see its own header for the
// schema). Missing entirely is fine; a malformed file is not.
function loadOverrides(scriptDir = SCRIPT_DIR) {
  const overridesPath = join(scriptDir, "third-party-license-overrides.json");
  try {
    return JSON.parse(readFileSync(overridesPath, "utf-8"));
  } catch (err) {
    if (err.code === "ENOENT") return {};
    throw err;
  }
}

// Package names vite.config.ts's plugin recorded as actually bundled. Null
// (not []) means no build has run yet, so bundle-membership checks are skipped.
function loadBundledPackageNames(staticDir) {
  const bundledPath = join(staticDir, BUNDLED_PACKAGES_FILE_NAME);
  try {
    return JSON.parse(readFileSync(bundledPath, "utf-8"));
  } catch (err) {
    if (err.code === "ENOENT") return null;
    throw err;
  }
}

// True when the lockfile marks this package unreachable from a production
// install (`dev` or `devOptional`).
function isDevOnly(entry) {
  return Boolean(entry?.dev) || Boolean(entry?.devOptional);
}

// A lockfile key is "" (root) or "node_modules/<name>[/node_modules/<name>...]";
// the package name is the segment after the last "node_modules/" (this also
// handles scoped names correctly).
function packageNameFromPath(path) {
  const segments = path.split("node_modules/");
  return segments[segments.length - 1];
}

// Resolves `depName` the way Node's own require/import would from a package
// installed at lockfile path `fromPath` (`""` for the root): try
// `<fromPath>/node_modules/<depName>` first, then walk up one ancestor
// node_modules directory at a time.
function resolveDependencyPath(lockfile, fromPath, depName) {
  let path = fromPath;
  for (;;) {
    const candidate = path === "" ? `node_modules/${depName}` : `${path}/node_modules/${depName}`;
    if (lockfile.packages[candidate]) return candidate;
    if (path === "") return null;
    const parentBoundary = path.lastIndexOf("/node_modules/");
    path = parentBoundary === -1 ? "" : path.slice(0, parentBoundary);
  }
}

// Finds an installed copy of `name` anywhere in the lockfile (used for names
// that only came from the bundled-packages set). Prefers the top-level
// install, else the first nested match in sorted order.
function findAnyEntryPathByName(lockfile, name) {
  const topLevel = `node_modules/${name}`;
  if (lockfile.packages[topLevel]) return topLevel;
  const suffix = `/node_modules/${name}`;
  const matches = Object.keys(lockfile.packages)
    .filter((key) => key.endsWith(suffix))
    .sort();
  return matches.length > 0 ? matches[0] : null;
}

// Walks production `dependencies`, plus installed `peerDependencies` and
// `optionalDependencies`, through the lockfile. A required edge that fails
// to resolve throws; an allowed-absent one is skipped.
function collectProductionClosure(lockfile) {
  const rootEntry = lockfile.packages[""];
  if (!rootEntry) {
    throw new Error(`lockfile's "packages" map has no root ("") entry`);
  }

  const visited = new Map();
  const queue = Object.keys(rootEntry.dependencies ?? {}).map((name) => ({
    fromPath: "",
    name,
    required: true,
  }));

  while (queue.length > 0) {
    const { fromPath, name, required } = queue.shift();
    const path = resolveDependencyPath(lockfile, fromPath, name);

    if (!path) {
      if (required) {
        throw new Error(
          `could not resolve dependency "${name}" from "${fromPath || "<root>"}" -- expected ` +
            `node_modules/${name} at or above that path in package-lock.json`,
        );
      }
      continue; // an allowed-absent optional/peer edge that simply isn't installed
    }

    if (visited.has(path)) continue;
    const entry = lockfile.packages[path];
    visited.set(path, entry);

    if (isDevOnly(entry)) continue; // not part of the production runtime graph

    for (const depName of Object.keys(entry.dependencies ?? {})) {
      queue.push({ fromPath: path, name: depName, required: true });
    }
    for (const depName of Object.keys(entry.optionalDependencies ?? {})) {
      queue.push({ fromPath: path, name: depName, required: false });
    }
    for (const depName of Object.keys(entry.peerDependencies ?? {})) {
      const optional = Boolean(entry.peerDependenciesMeta?.[depName]?.optional);
      queue.push({ fromPath: path, name: depName, required: !optional });
    }
  }

  return visited;
}

// Lists files (not directories) matching LICENSE_FILE_PATTERN; concatenates
// all matches so a dual-licensed package doesn't lose either license's terms.
function findLicenseFile(packageDir) {
  let dirents;
  try {
    dirents = readdirSync(packageDir, { withFileTypes: true });
  } catch {
    return null;
  }

  const candidates = dirents
    .filter((dirent) => dirent.isFile() && LICENSE_FILE_PATTERN.test(dirent.name))
    .map((dirent) => dirent.name)
    .sort();

  if (candidates.length === 0) return null;

  return candidates
    .map((name) => readFileSync(join(packageDir, name), "utf-8").trimEnd())
    .join("\n\n----------\n\n");
}

// Extracts the body of a Markdown "# License" or "## License" section (up to
// the next heading of level 1 or 2, or end of file), or null if the README
// has no such section at all.
function extractReadmeLicenseSection(readmeText) {
  const lines = readmeText.split(/\r?\n/);
  const start = lines.findIndex((line) => /^#{1,2}\s*license\s*$/i.test(line.trim()));
  if (start === -1) return null;

  let end = lines.length;
  for (let i = start + 1; i < lines.length; i++) {
    if (/^#{1,2}\s+\S/.test(lines[i])) {
      end = i;
      break;
    }
  }
  return lines
    .slice(start + 1, end)
    .join("\n")
    .trim();
}

function readReadmeLicenseSection(packageDir) {
  let dirents;
  try {
    dirents = readdirSync(packageDir, { withFileTypes: true });
  } catch {
    return null;
  }
  const readme = dirents.find((dirent) => dirent.isFile() && README_FILE_PATTERN.test(dirent.name));
  if (!readme) return null;

  const content = readFileSync(join(packageDir, readme.name), "utf-8");
  return extractReadmeLicenseSection(content);
}

// Rejects README license text that's too short or just restates the SPDX
// identifier -- that alone omits the required copyright/permission text.
function isSubstantiveReadmeLicenseText(text, licenseField) {
  if (!text) return false;
  const normalized = text.trim();
  if (normalized.length < MIN_SUBSTANTIVE_LICENSE_TEXT_LENGTH) return false;
  const bareLicenseName = String(licenseField ?? "").trim();
  if (bareLicenseName && normalized.toLowerCase() === bareLicenseName.toLowerCase()) return false;
  return true;
}

function formatAuthor(author) {
  if (!author) return null;
  if (typeof author === "string") {
    const match = author.match(/^([^<(]+)/);
    return (match ? match[1] : author).trim() || null;
  }
  if (typeof author === "object" && typeof author.name === "string") {
    return author.name.trim() || null;
  }
  return null;
}

function formatLicenseField(packageJson) {
  if (typeof packageJson.license === "string") return packageJson.license;
  if (packageJson.license && typeof packageJson.license === "object") {
    // The legacy `{ "type": "MIT", "url": "..." }` shape.
    return packageJson.license.type ?? null;
  }
  if (Array.isArray(packageJson.licenses) && packageJson.licenses.length > 0) {
    return packageJson.licenses.map((entry) => entry.type ?? entry).join(" OR ");
  }
  return null;
}

function formatRepository(packageJson) {
  const repository = packageJson.repository;
  if (typeof repository === "string") return repository;
  if (repository && typeof repository.url === "string") return repository.url;
  return null;
}

// Resolves a package's attribution text, trying in order: a `SEE LICENSE IN
// <file>` reference, an installed LICENSE/COPYING file, author + a
// substantive README license section, then a checked-in override. Returns
// { text: null } if none produce anything -- the caller treats that as fatal.
function resolveLicenseText(packageDir, packageJson, overrides, name, version) {
  const licenseField = formatLicenseField(packageJson);
  const seeLicenseMatch = typeof licenseField === "string" && licenseField.match(/^see license in (.+)$/i);
  if (seeLicenseMatch) {
    try {
      return { text: readFileSync(join(packageDir, seeLicenseMatch[1].trim()), "utf-8").trimEnd(), source: "file" };
    } catch {
      // Referenced file doesn't exist -- fall through to the rest of the chain.
    }
  }

  const fromFile = findLicenseFile(packageDir);
  if (fromFile !== null) return { text: fromFile, source: "file" };

  const holder = formatAuthor(packageJson.author);
  const readmeSection = readReadmeLicenseSection(packageDir);
  if (holder && isSubstantiveReadmeLicenseText(readmeSection, licenseField)) {
    return { text: `Copyright (c) ${holder}\n\n${readmeSection.trim()}`, source: "readme" };
  }

  const override = overrides[`${name}@${version}`];
  if (override && typeof override.text === "string" && override.text.trim().length > 0) {
    const overrideHolder = override.holder ?? holder;
    const text = overrideHolder
      ? `Copyright (c) ${overrideHolder}\n\n${override.text.trim()}`
      : override.text.trim();
    return { text, source: "override" };
  }

  return { text: null, source: null };
}

// Reads each in-scope package's license text from its installed
// node_modules/<name>/ directory, unioning the lockfile's production closure
// with the bundled-packages set (a name only in the bundle, e.g. a
// devDependency whose output still ships, is still attributed; a name only
// in the closure is attributed but flagged `inBundle: false`; a bundled name
// unresolvable in the lockfile is reported in `unresolvedBundled`).
function collectEntries(frontendDir, lockfile, { bundledPackageNames = null, overrides = {} } = {}) {
  const entries = [];
  const missingLicenseField = [];
  const missingLicenseText = [];
  const unresolvedBundled = [];

  const closure = collectProductionClosure(lockfile);
  const productionPaths = [...closure.entries()].filter(([, entry]) => !isDevOnly(entry)).map(([path]) => path);

  const pathByName = new Map();
  for (const path of productionPaths) {
    const name = packageNameFromPath(path);
    if (!pathByName.has(name)) pathByName.set(name, path);
  }

  const bundledSet = bundledPackageNames ? new Set(bundledPackageNames) : null;
  const extraPaths = [];

  if (bundledSet) {
    for (const name of bundledSet) {
      if (pathByName.has(name)) continue;
      const path = findAnyEntryPathByName(lockfile, name);
      if (!path) {
        unresolvedBundled.push(name);
        continue;
      }
      extraPaths.push(path);
      pathByName.set(name, path);
    }
  }

  for (const path of [...productionPaths, ...extraPaths]) {
    const packageDir = join(frontendDir, path);
    const packageJson = JSON.parse(readFileSync(join(packageDir, "package.json"), "utf-8"));

    const name = packageJson.name ?? packageNameFromPath(path);
    const version = packageJson.version ?? "unknown";
    const license = formatLicenseField(packageJson);
    const repository = formatRepository(packageJson);
    const inBundle = bundledSet ? bundledSet.has(name) : null;

    if (!license) {
      missingLicenseField.push(`${name}@${version}`);
      continue;
    }

    const { text: licenseText } = resolveLicenseText(packageDir, packageJson, overrides, name, version);
    if (licenseText === null) {
      missingLicenseText.push(`${name}@${version}`);
      continue;
    }

    entries.push({ name, version, license, repository, licenseText, inBundle });
  }

  entries.push(...vendoredEntries(frontendDir));

  return { entries, missingLicenseField, missingLicenseText, unresolvedBundled };
}

function renderNotice(entries) {
  const sorted = [...entries].sort((a, b) => a.name.localeCompare(b.name));

  const lines = [
    "Third-party licenses for the Palmimo Portal frontend bundle",
    "Generated from frontend/package-lock.json by",
    "frontend/scripts/generate-third-party-licenses.mjs -- do not edit by hand,",
    "regenerate with `npm run licenses` (or `make build`) from frontend/.",
    "",
    `${sorted.length} packages included.`,
    "",
  ];

  for (const entry of sorted) {
    lines.push(`=== ${entry.name}@${entry.version} — ${entry.license} ===`);
    if (entry.repository) lines.push(`Repository: ${entry.repository}`);
    lines.push(`In bundle: ${entry.inBundle === null ? "unknown" : entry.inBundle ? "yes" : "no"}`);
    lines.push("");
    lines.push(entry.licenseText.trimEnd());
    lines.push("");
  }

  return lines.join("\n");
}

function main() {
  const outputArg = process.argv[2];
  if (!outputArg) {
    console.error("Usage: generate-third-party-licenses.mjs <output-file>");
    process.exit(1);
  }
  const outputPath = resolve(process.cwd(), outputArg);
  const frontendDir = SCRIPT_FRONTEND_DIR;
  const staticDir = dirname(outputPath);

  const lockfile = loadLockfile(frontendDir);
  const overrides = loadOverrides();
  const bundledPackageNames = loadBundledPackageNames(staticDir);

  const { entries, missingLicenseField, missingLicenseText, unresolvedBundled } = collectEntries(
    frontendDir,
    lockfile,
    { bundledPackageNames, overrides },
  );

  if (unresolvedBundled.length > 0) {
    console.error(
      `generate-third-party-licenses: the following packages appear in ${BUNDLED_PACKAGES_FILE_NAME} ` +
        "(actually bundled by vite build) but could not be resolved in package-lock.json at all:",
    );
    for (const name of [...unresolvedBundled].sort()) console.error(`  - ${name}`);
    process.exit(1);
  }

  if (missingLicenseField.length > 0) {
    console.error(
      "generate-third-party-licenses: the following packages declare no license field at all " +
        "(cannot attribute them even by name):",
    );
    for (const name of missingLicenseField.sort()) console.error(`  - ${name}`);
    process.exit(1);
  }

  if (missingLicenseText.length > 0) {
    console.error(
      "generate-third-party-licenses: could not find license text for the following packages -- " +
        "no installed LICENSE file, no substantive README \"# License\" section with an author, and " +
        "no entry in frontend/scripts/third-party-license-overrides.json:",
    );
    for (const name of missingLicenseText.sort()) console.error(`  - ${name}`);
    process.exit(1);
  }

  const notice = renderNotice(entries);
  mkdirSync(dirname(outputPath), { recursive: true });
  writeFileSync(outputPath, notice, "utf-8");

  console.log(`generate-third-party-licenses: wrote ${entries.length} entries to ${outputPath}`);
  if (bundledPackageNames === null) {
    console.log(
      `generate-third-party-licenses: no ${BUNDLED_PACKAGES_FILE_NAME} found next to the output file -- ` +
        "run `vite build` first for bundle-membership checking (skipped this run, every entry is " +
        '"In bundle: unknown").',
    );
  }
}

// True only when this module was invoked directly (not imported by tests).
// realpathSync on both sides avoids mismatches from a symlinked tmp dir
// (macOS's /tmp -> /private/tmp) or a checkout path containing a space.
function isMainModule() {
  if (!process.argv[1]) return false;
  try {
    return import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;
  } catch {
    return false;
  }
}

if (isMainModule()) {
  main();
}

export {
  collectEntries,
  collectProductionClosure,
  findAnyEntryPathByName,
  findLicenseFile,
  loadBundledPackageNames,
  loadLockfile,
  loadOverrides,
  packageNameFromPath,
  renderNotice,
  resolveDependencyPath,
  resolveLicenseText,
  vendoredEntries,
};
