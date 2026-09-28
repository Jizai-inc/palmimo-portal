import { useEffect, useState } from "react";

import { PortalApiError } from "@/api/client";

/**
 * Turns a 429's fixed `retry_after_seconds` into one that counts down once a second, and drops
 * the error once the wait is over. Any other error is returned unchanged.
 */
export function useRetryCountdown(error: unknown): unknown {
  const deadline = retryDeadline(error);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (deadline === null) {
      return;
    }
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [deadline]);

  if (deadline === null || !(error instanceof PortalApiError)) {
    return error;
  }
  const remaining = Math.ceil((deadline - now) / 1000);
  if (remaining <= 0) {
    return null;
  }
  return new PortalApiError(error.status, error.code, { ...error.params, retry_after_seconds: remaining });
}

const deadlines = new WeakMap<PortalApiError, number>();

function retryDeadline(error: unknown): number | null {
  if (!(error instanceof PortalApiError) || error.status !== 429) {
    return null;
  }
  const seconds = Number(error.params.retry_after_seconds);
  if (!Number.isFinite(seconds)) {
    return null;
  }
  let deadline = deadlines.get(error);
  if (deadline === undefined) {
    deadline = Date.now() + seconds * 1000;
    deadlines.set(error, deadline);
  }
  return deadline;
}
