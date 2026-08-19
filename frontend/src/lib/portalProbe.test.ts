import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { startPortalProbe } from "@/lib/portalProbe";

describe("startPortalProbe", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("never calls onFound when every probe resolves (fail-first rule: still on the captive AP)", async () => {
    const fetchMock = vi.fn(() => Promise.resolve({} as Response));
    vi.stubGlobal("fetch", fetchMock);
    const onFound = vi.fn();

    startPortalProbe("palmimo-405", onFound, 1000);

    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(1000);

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(onFound).not.toHaveBeenCalled();
  });

  it("calls onFound exactly once once a resolve follows a reject, then stops probing", async () => {
    let callCount = 0;
    const fetchMock = vi.fn(() => {
      callCount += 1;
      if (callCount === 1) return Promise.resolve({} as Response);
      if (callCount === 2) return Promise.reject(new Error("network error"));
      return Promise.resolve({} as Response);
    });
    vi.stubGlobal("fetch", fetchMock);
    const onFound = vi.fn();

    startPortalProbe("palmimo-405", onFound, 1000);

    // 1st probe resolves -- still on the captive AP, no earlier failure yet.
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onFound).not.toHaveBeenCalled();

    // 2nd probe rejects -- the AP went down / the device re-associated.
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(onFound).not.toHaveBeenCalled();

    // 3rd probe resolves -- now trusted, since an earlier probe failed.
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(onFound).toHaveBeenCalledTimes(1);

    // Further ticks must neither call fetch again nor call onFound again.
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(onFound).toHaveBeenCalledTimes(1);
  });

  it("stops probing once the returned stop() is called", async () => {
    const fetchMock = vi.fn(() => Promise.reject(new Error("network error")));
    vi.stubGlobal("fetch", fetchMock);

    const stop = startPortalProbe("palmimo-405", vi.fn(), 1000);
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    stop();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("prevents any later onFound once stop() is called, even mid-resolution", async () => {
    let callCount = 0;
    const fetchMock = vi.fn(() => {
      callCount += 1;
      if (callCount === 1) return Promise.reject(new Error("network error"));
      return Promise.resolve({} as Response);
    });
    vi.stubGlobal("fetch", fetchMock);
    const onFound = vi.fn();

    const stop = startPortalProbe("palmimo-405", onFound, 1000);
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(onFound).toHaveBeenCalledTimes(1);

    stop();
    onFound.mockClear();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(onFound).not.toHaveBeenCalled();
  });

  it("requests the hostname's .local origin with {mode: 'no-cors', cache: 'no-store'} and a per-request AbortSignal", async () => {
    const fetchMock = vi.fn((_url: string, _init: RequestInit) => Promise.resolve({} as Response));
    vi.stubGlobal("fetch", fetchMock);

    startPortalProbe("palmimo-405", vi.fn(), 1000);
    await vi.advanceTimersByTimeAsync(0);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://palmimo-405.local/");
    expect(init.mode).toBe("no-cors");
    expect(init.cache).toBe("no-store");
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });
});

describe("startPortalProbe -- hung fetch", () => {
  // Real timers: `AbortSignal.timeout` isn't driven by vitest's fake-timer
  // patch of `setTimeout`, so this uses small real intervals instead.
  it("treats a hanging fetch as a failure via the per-request timeout, then recovers", async () => {
    let callCount = 0;
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      callCount += 1;
      if (callCount === 1) {
        // Never settles on its own -- only `init.signal`'s abort (fired by
        // `AbortSignal.timeout(intervalMs)`) can reject it.
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new Error("timeout")));
        });
      }
      return Promise.resolve({} as Response);
    });
    vi.stubGlobal("fetch", fetchMock);
    const onFound = vi.fn();

    startPortalProbe("palmimo-405", onFound, 20);

    await new Promise((resolve) => setTimeout(resolve, 150));

    // The 1st probe's timeout registered it as a failure; the 2nd resolved
    // and is now trusted because of it.
    expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(onFound).toHaveBeenCalledTimes(1);
  });
});
