// Collects the license text of every production npm dependency bundled into
// the built frontend into one text file, for attribution when the bundle
// ships as a binary release asset (see Makefile's `build` target and
// doc/releasing.md). Node's node_modules layout has no attribution file of
// its own, and the device never has node_modules -- so this has to run in
// CI, against the checked-out frontend/, before the bundle is packaged.
//
// The production dependency closure comes from package-lock.json rather
// than a live `npm ls`: the lockfile's "packages" map already marks
// dev-only entries with `dev: true`, so walking package.json's
// `dependencies` through it (skipping anything marked dev-only) reproduces
// exactly what `npm ci --omit=dev` would install, without spawning a
// subprocess or depending on any particular resolver output shape. Once a
// package's identity is known this way, its license *text* still has to
// come from the installed node_modules/<name>/ directory -- the lockfile
// doesn't carry that -- so this script requires node_modules to already be
// installed (`npm ci`).
import { readFileSync, readdirSync, mkdirSync, writeFileSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_FRONTEND_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");

const LICENSE_FILE_PATTERN = /^(licen[sc]e|copying)/i;

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

// Walks package.json `dependencies` through the lockfile's flat
// `node_modules/<name>` entries, skipping anything the lockfile marks
// dev-only, to reproduce the production install closure. Resolving every
// dependency edge to the top-level `node_modules/<name>` entry (rather than
// any nested one) matches how npm dedupes by default, and how Node itself
// would resolve the bare specifier from a package at the tree's root or one
// level down -- the common case for this project's dependency depth. A
// genuinely conflicting nested version would be missed; nothing in this
// dependency tree currently needs one.
function collectProductionPackageNames(lockfile) {
  const rootEntry = lockfile.packages[""];
  if (!rootEntry) {
    throw new Error(`lockfile's "packages" map has no root ("") entry`);
  }

  const visited = new Set();
  const queue = Object.keys(rootEntry.dependencies ?? {});

  while (queue.length > 0) {
    const name = queue.shift();
    if (visited.has(name)) continue;
    visited.add(name);

    const entry = lockfile.packages[`node_modules/${name}`];
    if (!entry || entry.dev) continue;

    for (const depName of Object.keys(entry.dependencies ?? {})) {
      if (!visited.has(depName)) queue.push(depName);
    }
  }

  return [...visited]
    .filter((name) => {
      const entry = lockfile.packages[`node_modules/${name}`];
      return entry && !entry.dev;
    })
    .sort();
}

function findLicenseFile(packageDir) {
  const candidates = readdirSync(packageDir)
    .filter((name) => LICENSE_FILE_PATTERN.test(name))
    .sort();
  return candidates.length > 0 ? join(packageDir, candidates[0]) : null;
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

// Reads each production package's identity, license, and license text
// straight from its installed node_modules/<name>/ directory -- the
// lockfile only decides which packages are in scope (see
// collectProductionPackageNames above), never their attribution. Returns
// every resolvable entry plus the names of any package that declares no
// license field at all, which the caller treats as fatal: attribution to a
// package silently dropped is worse than a loud build failure.
function collectEntries(frontendDir, lockfile) {
  const entries = [...vendoredEntries(frontendDir)];
  const missingLicenseField = [];

  for (const name of collectProductionPackageNames(lockfile)) {
    const packageDir = join(frontendDir, "node_modules", name);
    const packageJson = JSON.parse(readFileSync(join(packageDir, "package.json"), "utf-8"));

    const resolvedName = packageJson.name ?? name;
    const version = packageJson.version ?? "unknown";
    const license = formatLicenseField(packageJson);
    const repository = formatRepository(packageJson);

    if (!license) {
      missingLicenseField.push(`${resolvedName}@${version}`);
      continue;
    }

    const licenseFilePath = findLicenseFile(packageDir);
    entries.push({
      name: resolvedName,
      version,
      license,
      repository,
      licenseText: licenseFilePath ? readFileSync(licenseFilePath, "utf-8") : null,
    });
  }

  return { entries, missingLicenseField };
}

function renderNotice(entries) {
  const sorted = [...entries].sort((a, b) => a.name.localeCompare(b.name));
  const withoutLicenseFile = sorted.filter((entry) => entry.licenseText === null);

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
    lines.push("");
    lines.push(
      entry.licenseText !== null
        ? entry.licenseText.trimEnd()
        : "(no license file found in this package -- see the license field above)",
    );
    lines.push("");
  }

  if (withoutLicenseFile.length > 0) {
    lines.push("Packages without a license file (license field only):");
    for (const entry of withoutLicenseFile) {
      lines.push(`  - ${entry.name}@${entry.version} (${entry.license})`);
    }
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

  const lockfile = loadLockfile(frontendDir);
  const { entries, missingLicenseField } = collectEntries(frontendDir, lockfile);

  if (missingLicenseField.length > 0) {
    console.error(
      "generate-third-party-licenses: the following packages declare no license field at all " +
        "(cannot attribute them even by name):",
    );
    for (const name of missingLicenseField.sort()) console.error(`  - ${name}`);
    process.exit(1);
  }

  const notice = renderNotice(entries);
  mkdirSync(dirname(outputPath), { recursive: true });
  writeFileSync(outputPath, notice, "utf-8");

  const withoutLicenseFile = entries.filter((entry) => entry.licenseText === null);
  console.log(`generate-third-party-licenses: wrote ${entries.length} entries to ${outputPath}`);
  if (withoutLicenseFile.length > 0) {
    console.log(
      `generate-third-party-licenses: ${withoutLicenseFile.length} package(s) had no license file: ` +
        withoutLicenseFile.map((entry) => `${entry.name}@${entry.version}`).join(", "),
    );
  }
}

// Only run when executed directly (`node generate-third-party-licenses.mjs`),
// not when imported by the vitest unit tests.
if (import.meta.url === `file://${process.argv[1]}`) {
  main();
}

export { collectProductionPackageNames, collectEntries, renderNotice, loadLockfile, vendoredEntries };
