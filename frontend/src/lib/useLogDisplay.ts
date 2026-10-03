import { useEffect, useState } from "react";

interface LogDisplay {
  wrap: boolean;
  pretty: boolean;
}

const defaults: LogDisplay = { wrap: true, pretty: false };
const storageKey = "portal.logDisplay";
const changeEvent = "portal:logDisplay";

function readDisplay(): LogDisplay {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) ?? "null");
    if (typeof value?.wrap === "boolean" && typeof value?.pretty === "boolean") {
      return { wrap: value.wrap, pretty: value.pretty };
    }
  } catch {
    return defaults;
  }
  return defaults;
}

export function useLogDisplay() {
  const [display, setDisplay] = useState(readDisplay);
  useEffect(() => {
    const onChange = (event: Event) => setDisplay((event as CustomEvent<LogDisplay>).detail);
    const onStorage = (event: StorageEvent) => {
      if (event.key === storageKey || event.key === null) setDisplay(readDisplay());
    };
    window.addEventListener(changeEvent, onChange);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(changeEvent, onChange);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  function updateDisplay(next: LogDisplay) {
    try {
      localStorage.setItem(storageKey, JSON.stringify(next));
    } catch {
      // Restricted storage must not prevent changing the current view.
    }
    setDisplay(next);
    window.dispatchEvent(new CustomEvent(changeEvent, { detail: next }));
  }

  return { ...display, updateDisplay };
}
