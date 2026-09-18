/**
 * Compares two `MAJOR.MINOR.PATCH`-ish version strings numerically, dot-segment by dot-segment
 * (a non-numeric segment, or a missing one, counts as `0`). Good enough for gating "Portal is
 * too old for this platform bundle" (design doc 2.8's `requires_portal`) against the plain
 * numeric tags this project uses -- not a full semver implementation (no pre-release/build
 * metadata ordering).
 */
export function compareVersions(a: string, b: string): number {
  const partsA = a.split(".");
  const partsB = b.split(".");
  const length = Math.max(partsA.length, partsB.length);
  for (let i = 0; i < length; i++) {
    const numA = Number.parseInt(partsA[i] ?? "0", 10) || 0;
    const numB = Number.parseInt(partsB[i] ?? "0", 10) || 0;
    if (numA !== numB) return numA - numB;
  }
  return 0;
}

export function isVersionAtLeast(current: string, required: string): boolean {
  return compareVersions(current, required) >= 0;
}
