import { screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import { getGetPlatformApiV1PlatformGetQueryKey } from "@/api/generated/platform/platform";
import { DisplayFirmwareAlert } from "@/components/DisplayFirmwareAlert";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

describe("DisplayFirmwareAlert", () => {
  it.each([
    ["1", 1, false, false, true],
    ["1", 2, true, false, true],
    ["1", null, false, true, true],
    ["2", 1, true, false, false],
    ["2", 2, false, false, false],
    ["2", null, false, true, false],
    ["none", 1, false, false, false],
    ["none", 2, false, false, false],
    ["none", null, false, false, false],
    [null, 1, false, false, false],
    [null, 2, false, false, false],
    [null, null, false, false, false],
    [undefined, 1, false, false, false],
    [undefined, 2, false, false, false],
    [undefined, null, false, false, false],
  ] as const)("renders requirement %s with desired %s", async (firmware, desired, warning, unknown, badge) => {
    server.use(http.get("*/api/v1/platform", () => HttpResponse.json({ display_firmware_desired: desired })));
    const { queryClient, container } = renderWithProviders(<DisplayFirmwareAlert firmware={firmware} />);
    if (firmware === "1" || firmware === "2") {
      await waitFor(() => expect(queryClient.getQueryData(getGetPlatformApiV1PlatformGetQueryKey())).toBeDefined());
    }
    await waitFor(() => {
      expect(screen.queryByRole("alert") !== null).toBe(warning);
      expect(screen.queryByRole("status") !== null).toBe(unknown);
      expect(screen.queryByRole("note") !== null).toBe(badge);
      if (!warning && !unknown && !badge) expect(container).toBeEmptyDOMElement();
    });
  });

  it.each(["loading", "error"])("uses the unknown advisory while platform is %s", async (state) => {
    let requested = false;
    let release!: () => void;
    server.use(http.get("*/api/v1/platform", async () => {
      requested = true;
      if (state === "loading") await new Promise<void>((resolve) => { release = resolve; });
      return HttpResponse.json({ error: { code: "internal_error", params: {} } }, { status: 500 });
    }));
    const { queryClient } = renderWithProviders(<DisplayFirmwareAlert firmware="2" />);
    try {
      await waitFor(() => expect(requested).toBe(true));
      if (state === "error") {
        await waitFor(() => expect(queryClient.getQueryState(getGetPlatformApiV1PlatformGetQueryKey())?.status).toBe("error"));
      }
      expect(screen.getByRole("status")).not.toBeEmptyDOMElement();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    } finally {
      release?.();
    }
  });
});
