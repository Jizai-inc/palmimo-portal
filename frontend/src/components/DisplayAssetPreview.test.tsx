import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { DisplayAssetPreview } from "@/components/DisplayAssetPreview";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

const { loadAnimation, destroy, listeners } = vi.hoisted(() => ({
  loadAnimation: vi.fn(), destroy: vi.fn(), listeners: new Map<string, () => void>(),
}));
vi.mock("lottie-web/build/player/lottie_light", () => ({ default: { loadAnimation } }));

beforeEach(() => {
  listeners.clear();
  loadAnimation.mockReturnValue({ destroy, addEventListener: (name: string, fn: () => void) => listeners.set(name, fn) });
  vi.stubGlobal("URL", class extends URL {
    static createObjectURL = vi.fn(() => "blob:preview");
    static revokeObjectURL = vi.fn();
  });
});
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks(); });

it.each(["image/png", "image/jpeg", "image/gif"])("renders %s as an image and handles decode failure", async (mediaType) => {
  server.use(http.get("/source", () => new HttpResponse("image", { headers: { "Content-Type": mediaType } })));
  renderWithProviders(<DisplayAssetPreview src="/source" label="Face" />);
  const image = await screen.findByRole("img", { name: "Face" });
  expect(image.tagName).toBe("IMG");
  expect(loadAnimation).not.toHaveBeenCalled();
  fireEvent.error(image);
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  expect(screen.getByRole("status")).toBeInTheDocument();
});

it("plays Lottie and destroys playback on unmount", async () => {
  server.use(http.get("/source", () => HttpResponse.json({ layers: [], assets: [], segments: [{ time: 1 }] })));
  const view = renderWithProviders(<DisplayAssetPreview src="/source" label="Face" />);
  await waitFor(() => expect(loadAnimation).toHaveBeenCalled());
  expect(loadAnimation.mock.calls[0][0]).toMatchObject({ renderer: "svg", loop: true, autoplay: true, autoloadSegments: false });
  expect(screen.getByRole("img", { name: "Face" }).tagName).toBe("DIV");
  view.unmount();
  expect(destroy).toHaveBeenCalledOnce();
});

it.each(["http", "json", "animation"])("falls back when %s loading fails", async (failure) => {
  server.use(http.get("/source", () => failure === "http" ? new HttpResponse(null, { status: 404 })
    : failure === "json" ? new HttpResponse("{", { headers: { "Content-Type": "application/json" } })
    : HttpResponse.json({ layers: [] })));
  renderWithProviders(<DisplayAssetPreview src="/source" label="Face" />);
  if (failure === "animation") {
    await waitFor(() => expect(listeners.has("data_failed")).toBe(true));
    act(() => listeners.get("data_failed")?.());
  }
  await waitFor(() => expect(screen.queryByRole("img")).not.toBeInTheDocument());
  expect(screen.getByRole("status")).toBeInTheDocument();
});

it.each([
  { assets: [{ p: "https://example.com/image.png" }] },
  { assets: [{ p: "data:image/png;base64,AA==", e: 0, u: "https://example.com/" }] },
  { fonts: { list: [{ fFamily: "x", fName: "x", fOrigin: "g", fPath: "https://example.com/evil.css" }] } },
])("refuses animation data that would fetch external resources: %j", async (data) => {
  server.use(http.get("/source", () => HttpResponse.json({ layers: [], ...data })));
  renderWithProviders(<DisplayAssetPreview src="/source" label="Face" />);
  await screen.findByRole("status");
  expect(loadAnimation).not.toHaveBeenCalled();
});

it.each([false, true])("bounds concurrency and serializes lock-sensitive sources: %s", async (retryBusy) => {
  let active = 0;
  let maximum = 0;
  let calls = 0;
  server.use(http.get("/source/:id", async () => {
    active++;
    maximum = Math.max(active, maximum);
    await new Promise((resolve) => setTimeout(resolve, 5));
    active--;
    calls++;
    return HttpResponse.json({ layers: [] });
  }));
  renderWithProviders(<>{Array.from({ length: 17 }, (_, id) =>
    <DisplayAssetPreview key={id} src={`/source/${id}`} label={`Face ${id}`} retryBusy={retryBusy} />)}</>);
  await waitFor(() => expect(screen.getAllByRole("img")).toHaveLength(17));
  if (!retryBusy) expect(maximum).toBeGreaterThan(1);
  expect(maximum).toBeLessThanOrEqual(retryBusy ? 1 : 4);
  expect(calls).toBe(17);
});

it.each([false, true])("retries busy responses only when enabled: %s", async (retryBusy) => {
  let calls = 0;
  server.use(http.get("/source", () => ++calls === 1 ? new HttpResponse(null, { status: 409 }) : HttpResponse.json({ layers: [] })));
  renderWithProviders(<DisplayAssetPreview src="/source" label="Face" retryBusy={retryBusy} />);
  await screen.findByRole(retryBusy ? "img" : "status");
  expect(calls).toBe(retryBusy ? 2 : 1);
});

it.each(["", undefined])("refuses external image bases even when the image path is %j", async (path) => {
  server.use(http.get("/source", () => HttpResponse.json({ layers: [], assets: [{ id: "x", p: path, u: "https://example.com/image.png", e: 0 }] })));
  renderWithProviders(<DisplayAssetPreview src="/source" label="Face" />);
  await screen.findByRole("status");
  expect(loadAnimation).not.toHaveBeenCalled();
});
