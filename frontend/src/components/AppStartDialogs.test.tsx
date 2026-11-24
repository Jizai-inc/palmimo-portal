import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { AppSummary } from "@/api/generated/models";
import { getGetPlatformApiV1PlatformGetQueryKey } from "@/api/generated/platform/platform";
import { getGetAppApiV1AppsNameGetQueryKey, getListAppsApiV1AppsGetQueryKey } from "@/api/generated/apps/apps";
import { getGetStatusApiV1SystemStatusGetQueryKey } from "@/api/generated/system/system";
import { AppStartDialogs } from "@/components/AppStartDialogs";
import { useAppStart } from "@/lib/useAppStart";
import { renderWithProviders } from "@/test/render";
import { startJob as job } from "@/test/startJob";
import { server } from "@/test/server";

const app = { id: "face", name: "Face", display_firmware: "2" as AppSummary["display_firmware"] };


function Control({ firmware = app.display_firmware }: { firmware?: AppSummary["display_firmware"] }) {
  const target = { ...app, display_firmware: firmware };
  const start = useAppStart([target]);
  return <><button onClick={() => start.request(target)} disabled={start.isPending}>Start</button><AppStartDialogs start={start} /></>;
}

describe("app starts", () => {
  it.each([["2", 1, true], ["1", 2, true], ["2", null, true], ["2", 2, false], ["none", 1, false], [null, 2, false]] as const)(
    "confirms firmware %s against desired %s only when needed", async (firmware, desired, confirm) => {
      const post = vi.fn();
      server.use(
        http.get("*/api/v1/platform", () => HttpResponse.json({ ready: true, display_firmware_desired: desired })),
        http.post("*/api/v1/apps/face/start", () => { post(); return HttpResponse.json({ job }, { status: 202 }); }),
        http.get("*/api/v1/apps/jobs/start-1", () => HttpResponse.json(job)),
      );
      const { queryClient } = renderWithProviders(<Control firmware={firmware} />);
      if (firmware === "1" || firmware === "2") {
        await waitFor(() => expect(queryClient.getQueryData(getGetPlatformApiV1PlatformGetQueryKey())).toBeDefined());
      }
      await userEvent.click(screen.getByRole("button", { name: "Start" }));
      if (confirm) {
        const dialog = await screen.findByRole("alertdialog");
        expect(post).not.toHaveBeenCalled();
        await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
        expect(post).not.toHaveBeenCalled();
        await userEvent.click(screen.getByRole("button", { name: "Start" }));
        await userEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Start app" }));
      }
      await waitFor(() => expect(post).toHaveBeenCalledOnce());
      expect(await screen.findByRole("progressbar")).toBeInTheDocument();
    },
  );

  it.each([["done", false, null], ["failed", false, null], ["done", true, "absent"], ["failed", true, null], ["done", true, null]] as const)("refreshes dependent queries for %s, closed=%s, warning=%s", async (state, closed, warning) => {
    let current = job;
    server.use(
      http.get("*/api/v1/platform", () => HttpResponse.json({ ready: true, display_firmware_desired: 2 })),
      http.post("*/api/v1/apps/face/start", () => HttpResponse.json({ job }, { status: 202 })),
      http.get("*/api/v1/apps/jobs/start-1", () => HttpResponse.json(current)),
    );
    const { queryClient } = renderWithProviders(<Control firmware="none" />);
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    await userEvent.click(screen.getByRole("button", { name: "Start" }));
    await screen.findByRole("progressbar");
    if (closed) {
      await userEvent.click(screen.getByRole("button", { name: "Close" }));
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    }
    invalidate.mockClear();
    current = { ...job, state, step: "launch", warning };
    await act(() => queryClient.invalidateQueries({ queryKey: ["/api/v1/apps/jobs/start-1"] }));
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: getGetPlatformApiV1PlatformGetQueryKey() }));
    if (!closed || warning || state === "failed") {
      expect(await screen.findByRole("alertdialog")).toBeInTheDocument();
      if (warning) expect(screen.getByRole("alert")).not.toBeEmptyDOMElement();
      await userEvent.click(screen.getByRole("button", { name: "Close" }));
      await act(() => queryClient.refetchQueries({ queryKey: ["/api/v1/apps/jobs/start-1"] }));
    }
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    for (const queryKey of [getListAppsApiV1AppsGetQueryKey(), getGetAppApiV1AppsNameGetQueryKey("face"), getGetStatusApiV1SystemStatusGetQueryKey()]) {
      expect(invalidate).toHaveBeenCalledWith({ queryKey });
    }
  });
});

it("refreshes caches after an accepted start even when its control unmounts", async () => {
  let release!: () => void;
  let received = false;
  server.use(http.post("*/api/v1/apps/face/start", async () => {
    received = true;
    await new Promise<void>((resolve) => { release = resolve; });
    return HttpResponse.json({ job }, { status: 202 });
  }));
  const { unmount, queryClient } = renderWithProviders(<Control firmware="none" />);
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  await userEvent.click(screen.getByRole("button", { name: "Start" }));
  await waitFor(() => expect(received).toBe(true));
  unmount();
  release();
  await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: getGetAppApiV1AppsNameGetQueryKey("face") }));
});

it("requires confirmation when cached platform state can no longer be read", async () => {
  const post = vi.fn();
  server.use(
    http.get("*/api/v1/platform", () => HttpResponse.json({ error: { code: "internal_error", params: {} } }, { status: 500 })),
    http.post("*/api/v1/apps/face/start", () => { post(); return HttpResponse.json({ job }, { status: 202 }); }),
  );
  const { queryClient } = renderWithProviders(<Control />);
  queryClient.setQueryData(getGetPlatformApiV1PlatformGetQueryKey(), { ready: true, display_firmware_desired: 2 });
  await waitFor(() => expect(queryClient.getQueryState(getGetPlatformApiV1PlatformGetQueryKey())?.status).toBe("error"));
  await userEvent.click(screen.getByRole("button", { name: "Start" }));
  expect(await screen.findByRole("alertdialog")).toBeInTheDocument();
  expect(post).not.toHaveBeenCalled();
});
