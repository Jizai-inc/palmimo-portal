/**
 * Installs a fake `window.matchMedia` -- jsdom has none -- returning one
 * shared, mutable `MediaQueryList` per query string, so a test can flip
 * `.matches` and fire a `change` event to simulate a viewport crossing a
 * breakpoint (e.g. a phone rotating past `md`). `src/test/setup.ts` installs
 * a default (mobile: no query matches) before every test; call this again in
 * a test body to start desktop, or to grab the `setMatches` handle for a
 * flip.
 */
export function stubMatchMedia(initialMatches: boolean) {
  const lists = new Map<string, { mql: MediaQueryList; listeners: Set<(event: MediaQueryListEvent) => void> }>();

  window.matchMedia = ((query: string) => {
    let entry = lists.get(query);
    if (!entry) {
      const listeners = new Set<(event: MediaQueryListEvent) => void>();
      const mql = {
        media: query,
        matches: initialMatches,
        addEventListener: (_type: string, callback: (event: MediaQueryListEvent) => void) => listeners.add(callback),
        removeEventListener: (_type: string, callback: (event: MediaQueryListEvent) => void) => listeners.delete(callback),
        addListener: () => {},
        removeListener: () => {},
        dispatchEvent: () => false,
        onchange: null,
      } as unknown as MediaQueryList;
      entry = { mql, listeners };
      lists.set(query, entry);
    }
    return entry.mql;
  }) as typeof window.matchMedia;

  return {
    /** Flips `query`'s `.matches` and notifies every `change` listener registered for it. */
    setMatches(query: string, matches: boolean) {
      const entry = lists.get(query);
      if (!entry) {
        return;
      }
      (entry.mql as { matches: boolean }).matches = matches;
      const event = { matches } as MediaQueryListEvent;
      for (const callback of entry.listeners) {
        callback(event);
      }
    },
  };
}
