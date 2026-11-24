import { waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { DisplayAssetPreview } from "@/components/DisplayAssetPreview";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

// Lottie initializes a placeholder bitmap even when rendering only SVG.
beforeEach(() => vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ fillRect: () => undefined } as unknown as CanvasRenderingContext2D));
afterEach(() => vi.restoreAllMocks());

function animationData(start: number | undefined, rotation: unknown = { a: 0, k: 0 }) {
  return {
    v: "5.12.2", w: 480, h: 480, fr: 30, ip: 0, op: 60, ddd: 0,
    layers: [{
      ty: 4, ind: 1, nm: "body #glow(r=0)", ip: 0, op: 60, st: start,
      ks: { p: { a: 0, k: [240, 240] }, s: { a: 0, k: [100, 100] }, r: rotation },
      shapes: [
        { ty: "sh", ks: { a: 0, k: { i: [[0, 0], [0, 0], [0, 0]], o: [[0, 0], [0, 0], [0, 0]], v: [[0, 0], [50, 0], [0, 50]], c: true } } },
        { ty: "fl", c: { a: 0, k: [1, 0, 0, 1] }, o: { a: 0, k: 100 } },
      ],
    }],
  };
}

it.each([undefined, 0])("draws Lottie paths when layer start time is %s", async (start) => {
  server.use(http.get("/source", () => HttpResponse.json(animationData(start))));
  const view = renderWithProviders(<DisplayAssetPreview src="/source" label="Face" retryBusy={false} />);
  await waitFor(() => {
    const path = view.container.querySelector("svg path[d]");
    expect(path).not.toBeNull();
    expect(path?.getAttribute("d")).toContain("M");
    expect(path?.parentElement).not.toHaveStyle({ display: "none" });
  });
});

it.each([["scalar", true], ["array", true], ["scalar", false], ["array", false]] as const)("renders animated rotation from %s values with terminal start %s", async (format, terminalStart) => {
  const { default: lottie } = await import("lottie-web/build/player/lottie_light");
  const playback = vi.spyOn(lottie, "loadAnimation");
  const value = (angle: number) => format === "scalar" ? angle : [angle];
  const ease = { o: { x: 1 / 3, y: 1 / 3 }, i: { x: 2 / 3, y: 2 / 3 } };
  const rotation = { a: 1, k: [
    { t: 0, s: value(0), e: value(90), ...ease },
    { t: 10, s: value(90), e: value(0), ...ease },
    { t: 20, ...(terminalStart ? { s: value(0) } : {}) },
  ] };
  server.use(http.get("/source", () => HttpResponse.json(animationData(0, rotation))));
  const view = renderWithProviders(<DisplayAssetPreview src="/source" label="Face" retryBusy={false} />);
  await waitFor(() => expect(view.container.querySelector("svg path[d]")).not.toBeNull());
  const animation = playback.mock.results[0].value;
  for (const [frame, expected] of [[0, 0], [5, 45], [10, 90], [15, 45], [20, 0]]) {
    animation.goToAndStop(frame, true);
    const transform = view.container.querySelector("svg path[d]")?.closest("g[transform]")?.getAttribute("transform");
    const matrix = transform?.match(/matrix\(([^)]+)\)/)?.[1].split(",").map(Number);
    expect(matrix).toHaveLength(6);
    expect(Math.atan2(matrix![1], matrix![0]) * 180 / Math.PI).toBeCloseTo(expected, 3);
  }
});
