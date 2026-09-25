import { afterEach, describe, expect, it, vi } from "vitest";

import { copyText } from "@/lib/copyText";

describe("copyText", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // the textarea/execCommand path, which is unnecessary there and would fail silently in a
  // sandboxed iframe that blocks execCommand but allows the Clipboard API.
  it("uses the clipboard API when available", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    const result = await copyText("hello");

    expect(writeText).toHaveBeenCalledWith("hello");
    expect(result).toBe(true);
  });

  // `navigator.clipboard` is undefined) would have no way to copy text at all -- "Copy logs"
  // would silently do nothing on a real device.
  it("falls back to execCommand when navigator.clipboard is undefined", async () => {
    vi.stubGlobal("navigator", {});
    const execCommand = vi.fn().mockReturnValue(true);
    document.execCommand = execCommand;

    const result = await copyText("hello");

    expect(execCommand).toHaveBeenCalledWith("copy");
    expect(document.querySelector("textarea")).toBeNull();
    expect(result).toBe(true);
  });

  it("returns false when both the clipboard API and the fallback fail", async () => {
    vi.stubGlobal("navigator", {});
    document.execCommand = vi.fn().mockReturnValue(false);

    const result = await copyText("hello");

    expect(result).toBe(false);
  });
});
