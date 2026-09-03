// Collects the license text of every production npm dependency bundled into
// the built frontend into one text file, for attribution when the bundle
// ships as a binary release asset (see Makefile's `build` target and
// doc/releasing.md). Node's node_modules layout has no attribution file of
// its own, and the device never has node_modules -- so this has to run in
// CI, against the checked-out frontend/, before the bundle is packaged.
//
// Scope is "the production dependency closure from package-lock.json" UNION
// "whatever vite.config.ts's bundled-packages plugin says actually ended up
// in the built JS/CSS" (see loadBundledPackageNames below). The lockfile
// closure alone misses a devDependency whose *output* still ships (Tailwind
// CSS's preflight reset, pulled in by `@tailwindcss/vite` and landing in
// index.css even though `tailwindcss` itself is a devDependency); the
// bundle-membership set alone misses a runtime dependency that resolves but
// whose code Rollup tree-shook away entirely. Taking the union and flagging
// each entry's actual bundle membership keeps both directions honest without
// either silently dropping attribution or renders attributing dead code.
//
// Dependency resolution follows Node's own upward node_modules search
// (see resolveDependencyPath) rather than assuming everything hoists to a
// flat top-level node_modules/<name> -- npm nests a dependency instead of
// hoisting it whenever two packages need incompatible versions of the same
// name, and a purely-flat lookup would silently resolve to the wrong
// version (or nothing) whenever that happens.
//
// Once a package's identity is known this way, its license *text* still has
// to come from the installed node_modules/<name>/ directory -- the
// lockfile doesn't carry that -- so this script requires node_modules to
// already be installed (`npm ci`). When no LICENSE file is installed, a
// package can still supply the required copyright + permission text via its
// package.json `author` plus a substantive README "# License" section, or
// (last resort) a checked-in entry in third-party-license-overrides.json;
// if none of those produce real text, the build fails loudly rather than
// shipping an MIT/BSD package with no attribution at all.
import { readFileSync, readdirSync, mkdirSync, realpathSync, writeFileSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const SCRIPT_FRONTEND_DIR = resolve(SCRIPT_DIR, "..");
const BUNDLED_PACKAGES_FILE_NAME = ".bundled-packages.json";

const LICENSE_FILE_PATTERN = /^(licen[sc]e|copying)/i;
const README_FILE_PATTERN = /^readme(\.md)?$/i;
// A README "# License" / "## License" section's body must clear this length
// (after collapsing to the SPDX identifier alone gets rejected below) before
// it is trusted as real permission text rather than just a one-word restating
// of the `license` field ("MIT", "Apache-2.0", ...) -- see
// isSubstantiveReadmeLicenseText.
const MIN_SUBSTANTIVE_LICENSE_TEXT_LENGTH = 40;

// shadcn/ui's components are vendored by hand into the source tree (see
// THIRD_PARTY_NOTICES.md) rather than installed as a package, so they have
// no package-lock.json entry of their own -- but they do ship in the built
// bundle, so their license travels alongside the installed packages' here.
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

// Reads the checked-in fallback license text table (see the file's own
// header comment for the schema). Missing entirely is fine -- most projects
// never need an override; a malformed file is not.
function loadOverrides(scriptDir = SCRIPT_DIR) {
  const overridesPath = join(scriptDir, "third-party-license-overrides.json");
  try {
    return JSON.parse(readFileSync(overridesPath, "utf-8"));
  } catch (err) {
    if (err.code === "ENOENT") return {};
    throw err;
  }
}

// Reads the package-name set vite.config.ts's bundled-packages plugin wrote
// next to the build output. Returns null (not []) when the file is absent --
// distinct from "the bundle contains nothing" -- so callers can tell "no
// build has run yet" from "the build ran and bundled zero third-party
// packages", and skip bundle-membership checking rather than treating an
// empty set as ground truth.
function loadBundledPackageNames(staticDir) {
  const bundledPath = join(staticDir, BUNDLED_PACKAGES_FILE_NAME);
  try {
    return JSON.parse(readFileSync(bundledPath, "utf-8"));
  } catch (err) {
    if (err.code === "ENOENT") return null;
    throw err;
  }
}

// npm's lockfile marks a package `dev: true` when only reachable from
// devDependencies, and `devOptional: true` when it is reachable *only*
// through some combination of devDependency and optional-dependency/peer
// edges -- i.e. it would be pruned by `npm ci --omit=dev`'s production
// install just as surely as a plain `dev: true` entry, even though a
// non-omitting install (or one that also keeps optional deps) would still
// place it on disk. Either flag means "not part of the production runtime
// graph" for this script's purposes.
function isDevOnly(entry) {
  return Boolean(entry?.dev) || Boolean(entry?.devOptional);
}

// A lockfile "packages" key is either "" (the workspace root) or
// "node_modules/<name>[/node_modules/<name>...]". The package's own name is
// always the segment after the last "node_modules/" -- this also handles
// scoped names ("@scope/name") correctly, since the scope/name split never
// contains the literal substring "node_modules/". Deliberately splits on
// "node_modules/" without a leading slash: a top-level path
// ("node_modules/react") has no "/" *before* its "node_modules/", only a
// nested path does, and requiring the leading slash would leave every
// top-level path unsplit.
function packageNameFromPath(path) {
  const segments = path.split("node_modules/");
  return segments[segments.length - 1];
}

// Resolves `depName` the way Node's own require/import resolution would from
// a package installed at lockfile path `fromPath` (`""` for the workspace
// root): try `<fromPath>/node_modules/<depName>` first, then walk up one
// ancestor node_modules directory at a time until the root is reached.
// Skipping straight from a nested package's own node_modules entry to its
// parent's (rather than checking every filesystem directory in between)
// is equivalent here because only directories npm actually installed into
// are ever lockfile "packages" keys -- there is nothing else upward
// resolution could find along the way.
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

// Finds *some* installed copy of `name` anywhere in the lockfile, without a
// starting point to resolve upward from -- used only for names that came
// from the bundled-packages set (vite.config.ts's plugin records a bare
// package name, not the module id's full nested path). Prefers the
// top-level install (what almost every case in practice is) and otherwise
// takes the first nested match in sorted order, for determinism.
function findAnyEntryPathByName(lockfile, name) {
  const topLevel = `node_modules/${name}`;
  if (lockfile.packages[topLevel]) return topLevel;
  const suffix = `/node_modules/${name}`;
  const matches = Object.keys(lockfile.packages)
    .filter((key) => key.endsWith(suffix))
    .sort();
  return matches.length > 0 ? matches[0] : null;
}

// Walks package.json `dependencies` (the production ones only -- the
// lockfile root's "dependencies" key never includes devDependencies) through
// the lockfile, additionally following `peerDependencies` (including ones
// npm's install marks optional in `peerDependenciesMeta` -- if a peer is
// actually installed, its code can end up in the bundle just as easily as a
// regular dependency's) and `optionalDependencies`. A required edge
// (`dependencies`, or a peer not marked optional) that fails to resolve is a
// broken lockfile/node_modules and throws; an edge legitimately allowed to
// be absent (`optionalDependencies`, or an optional peer) is skipped instead.
// Returns every visited path (including dev-only ones, so cycles through them
// are not re-walked) -- callers filter dev-only back out.
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

// Lists only *files* whose name matches LICENSE_FILE_PATTERN, ignoring any
// same-named directory (some packages ship a `licenses/` subdirectory of
// per-file notices, which is not itself a license file) -- and, when more
// than one matches (dual-licensed packages sometimes ship
// `LICENSE-MIT` + `LICENSE-APACHE` side by side), concatenates all of them
// rather than picking one arbitrarily, so neither license's terms get
// silently dropped.
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

// A README "# License" section is only useful as MIT/BSD-style attribution
// text if it is more than the SPDX identifier restated (e.g. just "MIT") --
// that alone omits the copyright/permission/warranty text those licenses
// require to travel with the distribution. Rejects anything too short, or
// that (once whitespace-normalized) is exactly the `license` field's value.
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

// Resolves a package's full attribution text, trying (in order):
//   1. `"license": "SEE LICENSE IN <file>"` -- read that file verbatim.
//   2. An installed LICENSE/COPYING file (see findLicenseFile).
//   3. package.json `author` + a substantive README "# License" section.
//   4. A checked-in entry in third-party-license-overrides.json.
// Returns { text: null } if none of these produce anything -- the caller
// treats that as fatal.
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

// Reads each in-scope package's identity, license, and license text straight
// from its installed node_modules/<name>/ directory -- the lockfile only
// decides which packages are in scope, never their attribution.
//
// `bundledPackageNames` (from loadBundledPackageNames -- null means "no
// build has run yet, skip bundle-membership checking") is unioned with the
// lockfile's production closure: a name present in the bundle but not
// reachable from the closure (e.g. a devDependency whose CSS/output still
// ships) is still resolved and attributed; a name in the closure absent from
// the bundle (dead code Rollup dropped) is still attributed but flagged
// `inBundle: false`. A bundled name that cannot be resolved anywhere in the
// lockfile at all is reported back as `unresolvedBundled` -- the caller
// treats that as fatal, since it means the bundle contains something this
// script has no way to identify.
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

// Only run when executed directly (`node generate-third-party-licenses.mjs`),
// not when imported by the vitest unit tests.
//
// This needs care on two fronts a naive comparison gets wrong, both of which
// fail *silently* (the script exits 0 having done nothing, rather than
// erroring):
//   - A raw string/URL comparison (`import.meta.url === \`file://${argv[1]}\``)
//     never matches once the checkout path contains a space or other
//     non-ASCII character: `import.meta.url` percent-encodes those,
//     `process.argv[1]` never does.
//   - A plain filesystem-path comparison
//     (`fileURLToPath(import.meta.url) === resolve(argv[1])`) never matches
//     when any ancestor directory is a symlink (e.g. macOS's `/tmp` ->
//     `/private/tmp`): Node's ESM loader resolves `import.meta.url` through
//     the real path, but `path.resolve(argv[1])` never touches the
//     filesystem, so it keeps the symlinked form.
// Running both sides through `realpathSync` before building a `file://` URL
// (via `pathToFileURL`) makes the comparison immune to both.
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
