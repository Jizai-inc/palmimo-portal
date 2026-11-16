import { waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { DisplayAssetPreview } from "@/components/DisplayAssetPreview";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

// Lottie initializes a placeholder bitmap even when rendering only SVG.
beforeEach(() => vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ fillRect: () => undefined } as unknown as CanvasRenderingContext2D));
afterEach(() => vi.restoreAllMocks());

it.each([undefined, 0])("draws Lottie paths when layer start time is %s", async (start) => {
  server.use(http.get("/source", () => HttpResponse.json({
    v: "5.12.2", w: 480, h: 480, fr: 30, ip: 0, op: 60, ddd: 0,
    layers: [{
      ty: 4, ind: 1, nm: "body #glow(r=0)", ip: 0, op: 60, st: start,
      ks: { p: { a: 0, k: [240, 240] }, s: { a: 0, k: [100, 100] }, r: { a: 0, k: 0 } },
      shapes: [
        { ty: "sh", ks: { a: 0, k: { i: [[0, 0], [0, 0], [0, 0]], o: [[0, 0], [0, 0], [0, 0]], v: [[0, 0], [50, 0], [0, 50]], c: true } } },
        { ty: "fl", c: { a: 0, k: [1, 0, 0, 1] }, o: { a: 0, k: 100 } },
      ],
    }],
  })));
  const view = renderWithProviders(<DisplayAssetPreview src="/source" label="Face" retryBusy={false} />);
  await waitFor(() => {
    const path = view.container.querySelector("svg path[d]");
    expect(path).not.toBeNull();
    expect(path?.getAttribute("d")).toContain("M");
    expect(path?.parentElement).not.toHaveStyle({ display: "none" });
  });
});
