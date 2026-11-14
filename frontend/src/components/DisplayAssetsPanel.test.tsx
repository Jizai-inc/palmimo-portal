import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import type { DisplayAssetPreviewResponse, StandardAssetInfo } from "@/api/generated/models";
import { DisplayAssetsPanel } from "@/components/DisplayAssetsPanel";
import i18n from "@/i18n";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

const asset: StandardAssetInfo = {
  name: "happy", description: "Catalog description", translations: {}, overridden: false,
  kind: "vector", default_kind: "vector", bytes: 4096, default_bytes: 4096,
  params: ["eye_color"], default_params: ["eye_color"],
};
const preview: DisplayAssetPreviewResponse = {
  token: "preview-token", kind: "gif", bytes: 8192, crc32: "12345678",
  warnings: [{ code: "approximation", message: "Fixture conversion warning" }], params_lost: ["eye_color"],
  capacity: { ok: true, short_by: 0, apps_no_longer_fit: [] },
};
const text = (key: string) => i18n.t(key);
const file = () => new File(["source"], "face.gif", { type: "image/gif" });

function stubList(rows = [asset], orphans: string[] = []) {
  server.use(http.get("*/api/v1/display-assets/standard", () => HttpResponse.json({ standard: rows, orphan_overrides: orphans })));
}

async function upload(user: ReturnType<typeof userEvent.setup>) {
  await user.upload(await screen.findByLabelText(i18n.t("settings.display.chooseFile", { name: text("settings.display.names.happy") })), file());
  return screen.findByRole("alertdialog");
}

describe("DisplayAssetsPanel", () => {
  it("shows preview warnings, lost slots, size increase and capacity blockers before saving", async () => {
    stubList();
    let saves = 0;
    server.use(
      http.post("*/api/v1/display-assets/standard/happy/preview", () => HttpResponse.json({
        ...preview, capacity: { ok: false, short_by: 4096, apps_no_longer_fit: ["zip.large"] },
      })),
      http.put("*/api/v1/display-assets/standard/happy", () => { saves++; return HttpResponse.json({}); }),
    );
    renderWithProviders(<DisplayAssetsPanel />);
    const dialog = await upload(userEvent.setup());

    expect(within(dialog).getByText(preview.warnings[0].message)).toBeVisible();
    expect(within(dialog).getByText("eye_color")).toBeVisible();
    expect(within(dialog).getByText("zip.large")).toBeVisible();
    expect(within(dialog).getByLabelText(text("settings.display.sizeChange"))).toHaveTextContent("4");
    expect(within(dialog).getByRole("button", { name: text("settings.display.save") })).toBeDisabled();
    expect(saves).toBe(0);
  });

  it("saves only after confirmation, refetches the list and shows a busy sync result", async () => {
    let lists = 0;
    let saved: unknown;
    server.use(
      http.get("*/api/v1/display-assets/standard", () => {
        lists++;
        return HttpResponse.json({ standard: [{ ...asset, overridden: lists > 1 }], orphan_overrides: [] });
      }),
      http.post("*/api/v1/display-assets/standard/happy/preview", () => HttpResponse.json(preview)),
      http.put("*/api/v1/display-assets/standard/happy", async ({ request }) => {
        saved = await request.json();
        return HttpResponse.json({ sync: { result: "busy", put: [], deleted: [], reason: "occupied" } });
      }),
    );
    renderWithProviders(<DisplayAssetsPanel />);
    const user = userEvent.setup();
    const dialog = await upload(user);
    expect(saved).toBeUndefined();
    await user.click(within(dialog).getByRole("button", { name: text("settings.display.save") }));

    await waitFor(() => expect(saved).toEqual({ token: preview.token }));
    expect(await screen.findByText(text("settings.display.syncBusy"))).toBeVisible();
    expect(await screen.findByText(text("settings.display.overridden"))).toBeVisible();
    expect(lists).toBeGreaterThan(1);
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it.each(["happy", "retired"])("resets %s and refetches while orphan rows offer deletion only", async (name) => {
    let lists = 0;
    let deleted = false;
    server.use(
      http.get("*/api/v1/display-assets/standard", () => {
        lists++;
        return HttpResponse.json({ standard: [{ ...asset, overridden: !deleted }], orphan_overrides: deleted ? [] : ["retired"] });
      }),
      http.delete(`*/api/v1/display-assets/standard/${name}`, () => {
        deleted = true;
        return HttpResponse.json({ sync: { result: "synced", put: [], deleted: [], reason: null } });
      }),
    );
    renderWithProviders(<DisplayAssetsPanel />);
    const orphan = (await screen.findByText("retired")).closest("li")!;
    expect(within(orphan).getAllByRole("button")).toHaveLength(1);
    expect(orphan.querySelector('input[type="file"]')).toBeNull();
    const user = userEvent.setup();
    const button = name === "retired"
      ? within(orphan).getByRole("button", { name: text("settings.display.deleteOrphan") })
      : screen.getByRole("button", { name: text("settings.display.reset") });
    await user.click(button);
    const dialog = await screen.findByRole("alertdialog");
    expect(deleted).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: text("settings.display.confirmReset") }));
    await waitFor(() => expect(deleted).toBe(true));
    await waitFor(() => expect(lists).toBeGreaterThan(1));
  });

  it("discards a cancelled preview without saving", async () => {
    stubList();
    let saves = 0;
    server.use(
      http.post("*/api/v1/display-assets/standard/happy/preview", () => HttpResponse.json(preview)),
      http.put("*/api/v1/display-assets/standard/happy", () => { saves++; return HttpResponse.json({}); }),
    );
    renderWithProviders(<DisplayAssetsPanel />);
    const user = userEvent.setup();
    const dialog = await upload(user);
    await user.click(within(dialog).getByRole("button", { name: text("common.cancel") }));

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(saves).toBe(0);
  });

  it("discards expired previews and allows selecting the same file again", async () => {
    stubList();
    let previews = 0;
    server.use(
      http.post("*/api/v1/display-assets/standard/happy/preview", () => { previews++; return HttpResponse.json(preview); }),
      http.put("*/api/v1/display-assets/standard/happy", () => HttpResponse.json({ error: { code: "display_asset_preview_expired", params: {} } }, { status: 409 })),
    );
    renderWithProviders(<DisplayAssetsPanel />);
    const user = userEvent.setup();
    const dialog = await upload(user);
    await user.click(within(dialog).getByRole("button", { name: text("settings.display.save") }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(screen.getByRole("alert")).toBeVisible();
    await upload(user);
    expect(previews).toBe(2);
  });

  it("keeps save disabled until the in-flight request finishes", async () => {
    stubList();
    let release!: () => void;
    server.use(
      http.post("*/api/v1/display-assets/standard/happy/preview", () => HttpResponse.json(preview)),
      http.put("*/api/v1/display-assets/standard/happy", async () => {
        await new Promise<void>((resolve) => { release = resolve; });
        return HttpResponse.json({ sync: { result: "synced", put: [], deleted: [], reason: null } });
      }),
    );
    renderWithProviders(<DisplayAssetsPanel />);
    const user = userEvent.setup();
    const dialog = await upload(user);
    await user.click(within(dialog).getByRole("button", { name: text("settings.display.save") }));
    try {
      expect(within(dialog).getByRole("button", { name: text("settings.display.saving") })).toBeDisabled();
      expect(within(dialog).getByRole("button", { name: text("common.cancel") })).toBeDisabled();
    } finally {
      await waitFor(() => expect(release).toBeDefined());
      release();
    }
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  });
});

it("warns for image replacements even when the standard asset has no slots", async () => {
  stubList([{ ...asset, params: [], default_params: [] }]);
  server.use(http.post("*/api/v1/display-assets/standard/happy/preview", () => HttpResponse.json({ ...preview, params_lost: [] })));
  renderWithProviders(<DisplayAssetsPanel />);
  const dialog = await upload(userEvent.setup());
  expect(within(dialog).getByText(text("settings.display.rasterWarning"))).toBeVisible();
});

it("shows conversion diagnostics and permits retrying without a stale confirmation", async () => {
  stubList();
  server.use(http.post("*/api/v1/display-assets/standard/happy/preview", () => HttpResponse.json({
    error: { code: "display_asset_invalid", params: { errors: [{ code: "invalid", message: "Fixture layer error", path: "$.layers[0]" }] } },
  }, { status: 422 })));
  renderWithProviders(<DisplayAssetsPanel />);
  const user = userEvent.setup();
  await user.upload(await screen.findByLabelText(i18n.t("settings.display.chooseFile", { name: text("settings.display.names.happy") })), file());
  expect(await screen.findByText(/Fixture layer error/)).toBeVisible();
  expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
});

it("uses catalog translations and preserves unknown asset names", async () => {
  await i18n.changeLanguage("ja-JP");
  try {
    stubList([{ ...asset, name: "new-expression", translations: { ja: { description: "Translated fixture description" } } }]);
    renderWithProviders(<DisplayAssetsPanel />);
    expect(await screen.findByRole("heading", { name: "new-expression" })).toBeVisible();
    expect(screen.getByText("Translated fixture description")).toBeVisible();
    expect(screen.queryByText(asset.description!)).not.toBeInTheDocument();
  } finally {
    await i18n.changeLanguage("en");
  }
});
