import type { AppSourceInfo } from "@/api/generated/models";

/** `owner/repo` from a git URL's first two path segments, `.git` suffix stripped. `null` for a
 * URL with fewer than two path segments (not a shape any real app source has). */
function ownerRepo(url: string): string | null {
  let parts: string[];
  try {
    parts = new URL(url).pathname.split("/").filter(Boolean);
  } catch {
    return null;
  }
  if (parts.length < 2) return null;
  return `${parts[0]}/${parts[1].replace(/\.git$/, "")}`;
}

/**
 * One-line summary of where an app's code came from (design doc 3.7's apps-list row): a git
 * source as `git: owner/repo (subdir) @ref`, a zip upload as plain `zip`.
 */
export function formatSourceSummary(source: Pick<AppSourceInfo, "type" | "url" | "subdir" | "ref">): string {
  if (source.type === "zip") return "zip";
  if (source.type !== "git") return source.type;
  const repo = (source.url && ownerRepo(source.url)) ?? source.url ?? "git";
  const subdir = source.subdir ? ` (${source.subdir})` : "";
  const ref = source.ref ? ` @${source.ref}` : "";
  return `git: ${repo}${subdir}${ref}`;
}
