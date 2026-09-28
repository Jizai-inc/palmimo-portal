import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PortalApiError } from "@/api/client";
import { useRetryCountdown } from "@/lib/useRetryCountdown";

function remaining(value: unknown): unknown {
  return value instanceof PortalApiError ? value.params.retry_after_seconds : value;
}

describe("useRetryCountdown", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("counts a 429's retry-after down each second and drops it at zero", () => {
    const error = new PortalApiError(429, "update_check_rate_limited", { retry_after_seconds: 3 });
    const { result } = renderHook(() => useRetryCountdown(error));

    expect(remaining(result.current)).toBe(3);
    act(() => vi.advanceTimersByTime(1000));
    expect(remaining(result.current)).toBe(2);
    act(() => vi.advanceTimersByTime(2000));
    expect(result.current).toBeNull();
  });

  it("shows a fractional retry-after as whole seconds", () => {
    const error = new PortalApiError(429, "auth_rate_limited", { retry_after_seconds: 4.2731 });
    const { result } = renderHook(() => useRetryCountdown(error));

    expect(remaining(result.current)).toBe(5);
  });

  it("returns any other error unchanged", () => {
    const error = new PortalApiError(409, "app_running", {});
    const { result } = renderHook(() => useRetryCountdown(error));

    expect(result.current).toBe(error);
  });
});
