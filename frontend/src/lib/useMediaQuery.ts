import { useEffect, useState } from "react";

/**
 * Tracks a CSS media query via `matchMedia`, re-rendering on each change --
 * e.g. a phone rotating past the `md` breakpoint -- rather than reading the
 * viewport once at mount. Callers gate behavior that must never straddle two
 * form factors at once (see `AppShell`'s desktop-sidebar/mobile-drawer split).
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);

  useEffect(() => {
    const mql = window.matchMedia(query);
    setMatches(mql.matches);
    function onChange(event: MediaQueryListEvent) {
      setMatches(event.matches);
    }
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}
