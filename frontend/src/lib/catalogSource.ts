import type { AppSourceInfo } from "@/api/generated/models";
import type { GitInstallSource } from "@/lib/appInstall";

/**
 * Narrows a catalog entry's {@link AppSourceInfo} into an installable git source.
 *
 * `AppSourceInfo` types `type`/`ref_kind` as plain `string` (shared with the installed-app
 * ledger, where they mirror `AppSourceType`/`AppRefKind`) -- design doc 4.1 guarantees every
 * catalog entry is `type: "git"` with `ref_kind` one of `branch`/`tag`, but nothing in the
 * generated type enforces that at compile time, so this still checks it at runtime.
 */
export function parseCatalogSource(source: AppSourceInfo): GitInstallSource | null {
  const { type, url, ref, ref_kind, subdir } = source;
  if (type !== "git" || typeof url !== "string" || typeof ref !== "string") return null;
  if (ref_kind !== "branch" && ref_kind !== "tag") return null;
  return { type: "git", url, ref, ref_kind, ...(typeof subdir === "string" ? { subdir } : {}) };
}
