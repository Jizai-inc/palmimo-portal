import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import i18n from "@/i18n";
import type { PublicCatalogAppInfo, PublicCatalogResponse } from "@/api/generated/models";
import { DiscoverPanel } from "@/components/DiscoverPanel";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

const COMMUNITY: PublicCatalogAppInfo = {
  public_page_url: "https://apps.palmimo.dev/apps/019a3f2c-0000-7000-8000-000000000001",
  name: "dance", description: "Community dance", translations: {}, tags: [], thumbnail: null, env: [], devices: [],
  public: { app_id: "019a3f2c-0000-7000-8000-000000000001", namespace: "alice", official: false },
  source: { type: "git", url: "https://github.com/alice/dance", ref: "v1", ref_kind: "tag", commit: "a".repeat(40), subdir: null, manifest: null, official: false },
};
const OFFICIAL: PublicCatalogAppInfo = { ...COMMUNITY, public_page_url: "https://apps.palmimo.dev/apps/019a3f2c-0000-7000-8000-000000000002", description: "Official dance", public: { ...COMMUNITY.public, app_id: "019a3f2c-0000-7000-8000-000000000002", namespace: "palmimo", official: true }, source: { ...COMMUNITY.source, official: true } };
const CATALOG: PublicCatalogResponse = { generated_at: "2026-11-09T00:00:00Z", fetched_at: 1700000000, stale: false, reason: null, apps: [OFFICIAL, COMMUNITY] };
const JOB = { id: "public-job", app_id: "alice.dance", kind: "install", state: "done", step: "register", error: null, error_code: null, started_at: 1, finished_at: 2, lock_generated: false, dropped_params: [], dropped_bindings: [] };

function handlers(catalog = CATALOG) {
  server.use(
    http.get("*/api/v1/catalog/public", () => HttpResponse.json(catalog)),
    http.get("*/api/v1/apps", () => HttpResponse.json({ apps: [], default_app: null, autostart_conflict: [] })),
    http.get("*/api/v1/apps/jobs/public-job", () => HttpResponse.json(JOB)),
  );
}
async function choosePublic(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole("heading", { name: OFFICIAL.name });
  await user.selectOptions(screen.getByRole("combobox", { name: i18n.t("appAdd.catalogFilterLabel") }), "public");
}

describe("DiscoverPanel", () => {
  it.each([null, { emoji: "💃" }, { image_url: "https://raw.githubusercontent.com/alice/dance/" + "a".repeat(40) + "/thumb.png" }])("shows catalog thumbnail %j", async (thumbnail) => {
    handlers({ ...CATALOG, apps: [{ ...OFFICIAL, thumbnail }] });
    renderWithProviders(<DiscoverPanel />);
    const image = await screen.findByRole("img", { name: i18n.t("appAdd.catalogThumbnail", { name: OFFICIAL.name }) });
    if (thumbnail?.emoji) expect(image).toHaveTextContent(thumbnail.emoji);
    else if (thumbnail && "image_url" in thumbnail) {
      expect(image).toHaveAttribute("src", thumbnail.image_url);
      expect(image).toHaveAttribute("loading", "lazy");
      expect(image).toHaveAttribute("referrerpolicy", "no-referrer");
    } else expect(image.tagName.toLowerCase()).toBe("svg");
  });

  it("falls back to the package icon when the thumbnail fails", async () => {
    handlers({ ...CATALOG, apps: [{ ...OFFICIAL, thumbnail: { image_url: "https://example.com/thumb.png" } }] });
    renderWithProviders(<DiscoverPanel />);
    const label = i18n.t("appAdd.catalogThumbnail", { name: OFFICIAL.name });
    fireEvent.error(await screen.findByRole("img", { name: label }));
    expect(screen.getByRole("img", { name: label }).tagName.toLowerCase()).toBe("svg");
  });

  it.each(["en", "ja"])("translates tags and falls back to their spelling in %s", async (language) => {
    await i18n.changeLanguage(language);
    handlers({ ...CATALOG, apps: [{ ...OFFICIAL, tags: ["Dance", "Ärger", "Music"], translations: { en: { "tags.dance": "Dancing", "tags.Ärger": "Anger" }, ja: { "tags.dance": "ダンス", "tags.Ärger": "怒り" } } }] });
    renderWithProviders(<DiscoverPanel />);
    await screen.findByRole("heading", { name: OFFICIAL.name });
    const card = within(screen.getByRole("heading", { name: OFFICIAL.name }).closest("article")!);
    expect(card.getByText(language === "ja" ? "ダンス" : "Dancing")).toBeInTheDocument();
    expect(card.getByText(language === "ja" ? "怒り" : "Anger")).toBeInTheDocument();
    expect(card.getByText("Music")).toBeInTheDocument();
  });

  it("filters by ASCII tag key and restores all apps", async () => {
    handlers({ ...CATALOG, apps: [{ ...OFFICIAL, tags: ["Dance"] }, { ...OFFICIAL, name: "music", public: { ...OFFICIAL.public, app_id: "music" }, tags: ["Music"] }] });
    const user = userEvent.setup();
    renderWithProviders(<DiscoverPanel />);
    await user.click(await screen.findByRole("button", { name: "Dance (1)" }));
    expect(screen.getByRole("heading", { name: OFFICIAL.name })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "music" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: i18n.t("appAdd.catalogAllTags") }));
    expect(screen.getByRole("heading", { name: "music" })).toBeInTheDocument();
  });

  it("ranks at most twelve tag filters by app count within the selected catalog", async () => {
    const tags = Array.from({ length: 13 }, (_, i) => `Tag${i}`);
    handlers({ ...CATALOG, apps: [{ ...OFFICIAL, tags: [...tags, "TAG12"] }, { ...OFFICIAL, name: "second", public: { ...OFFICIAL.public, app_id: "second" }, tags: ["tag12"] }, { ...COMMUNITY, tags: ["Community"] }] });
    const user = userEvent.setup();
    renderWithProviders(<DiscoverPanel />);
    await screen.findByRole("heading", { name: OFFICIAL.name });
    const filters = within(screen.getByRole("group", { name: i18n.t("appAdd.catalogTagsLabel") }));
    expect(filters.getAllByRole("button")).toHaveLength(13);
    expect(filters.getAllByRole("button")[1]).toHaveTextContent("Tag12 (2)");
    expect(filters.queryByRole("button", { name: "Tag11 (1)" })).not.toBeInTheDocument();
    await user.click(filters.getByRole("button", { name: "Tag12 (2)" }));
    await choosePublic(user);
    expect(screen.getByRole("heading", { name: "alice/dance" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Tag12 (2)" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Community (1)" })).toBeInTheDocument();
  });

  it("filters official and public apps using catalog identity", async () => {
    handlers();
    const user = userEvent.setup();
    renderWithProviders(<DiscoverPanel />);
    await screen.findByRole("heading", { name: OFFICIAL.name });
    expect(screen.queryByRole("heading", { name: "alice/dance" })).not.toBeInTheDocument();
    await choosePublic(user);
    expect(screen.getByRole("heading", { name: "alice/dance" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: OFFICIAL.name })).not.toBeInTheDocument();
  });

  it("requires a community warning before installing the displayed commit", async () => {
    handlers();
    const user = userEvent.setup();
    const onInstalled = vi.fn();
    const posted = vi.fn();
    server.use(http.post("*/api/v1/apps/install-public", async ({ request }) => { posted(await request.json()); return HttpResponse.json({ job: JOB }, { status: 202 }); }));
    renderWithProviders(<DiscoverPanel onInstalled={onInstalled} />);
    await choosePublic(user);
    await user.click(screen.getByRole("button", { name: i18n.t("appAdd.installButton") }));
    const dialog = within(screen.getByRole("alertdialog"));
    expect(dialog.getByRole("alert")).toHaveTextContent(i18n.t("appAdd.publicWarning", { namespace: "alice" }));
    expect(dialog.getByRole("link", { name: i18n.t("appAdd.publicSourceLink") })).toHaveAttribute("href", COMMUNITY.source.url);
    expect(posted).not.toHaveBeenCalled();
    await user.click(dialog.getByRole("button", { name: i18n.t("appAdd.installButton") }));
    await waitFor(() => expect(posted).toHaveBeenCalledWith({ public_app_id: COMMUNITY.public.app_id, commit: COMMUNITY.source.commit }));
    await waitFor(() => expect(onInstalled).toHaveBeenCalledWith(JOB.app_id));
  });

  it.each([[409, "public_app_changed"], [404, "public_app_not_found"], [503, "public_catalog_unavailable"], [409, "public_app_installed"]])("reports install error %s and refreshes a changed catalog", async (status, code) => {
    handlers();
    let catalogGets = 0;
    server.use(http.get("*/api/v1/catalog/public", () => { catalogGets++; return HttpResponse.json(CATALOG); }), http.post("*/api/v1/apps/install-public", () => HttpResponse.json({ error: { code, params: {} } }, { status })));
    const user = userEvent.setup();
    renderWithProviders(<DiscoverPanel />);
    await choosePublic(user);
    await user.click(screen.getByRole("button", { name: i18n.t("appAdd.installButton") }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: i18n.t("appAdd.installButton") }));
    expect(await screen.findByRole("alert")).toHaveTextContent(i18n.t(`errors.${code}`));
    if (code === "public_app_changed") await waitFor(() => expect(catalogGets).toBeGreaterThan(1));
  });

  it.each([true, false])("shows stale state with a previous fetch %s", async (fetched) => {
    handlers({ ...CATALOG, stale: true, reason: "offline", generated_at: fetched ? CATALOG.generated_at : null, fetched_at: fetched ? CATALOG.fetched_at : null, apps: fetched ? CATALOG.apps : [] });
    renderWithProviders(<DiscoverPanel />);
    const alert = await screen.findByRole("alert");
    if (fetched) {
      expect(await screen.findByRole("heading", { name: OFFICIAL.name })).toBeInTheDocument();
      expect(within(alert).getByRole("time")).toHaveAttribute("dateTime", new Date(CATALOG.fetched_at! * 1000).toISOString());
    } else expect(alert).toHaveTextContent(i18n.t("appAdd.catalogNeverFetched"));
  });

  it("links catalog app details to the public app page", async () => {
    handlers();
    renderWithProviders(<DiscoverPanel />);
    expect(await screen.findByRole("link", { name: i18n.t("appAdd.publicPageLink") })).toHaveAttribute("href", `https://apps.palmimo.dev/apps/${OFFICIAL.public.app_id}`);
  });

  it("hides previous catalog data after a failed refetch", async () => {
    handlers();
    const { queryClient } = renderWithProviders(<DiscoverPanel />);
    await screen.findByRole("heading", { name: OFFICIAL.name });
    server.use(http.get("*/api/v1/catalog/public", () => HttpResponse.json({ error: { code: "public_catalog_unavailable", params: {} } }, { status: 503 })));
    await act(() => queryClient.refetchQueries());
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: OFFICIAL.name })).not.toBeInTheDocument();
  });
});

it.each([false, true])("refetches app and catalog results after install, with read failure %s", async (fail) => {
  handlers();
  let installed = false;
  let appGets = 0;
  let catalogGets = 0;
  const error = () => HttpResponse.json({ error: { code: "public_catalog_unavailable", params: {} } }, { status: 503 });
  server.use(
    http.get("*/api/v1/apps", () => { appGets++; return installed && fail ? error() : HttpResponse.json({ apps: [], default_app: null, autostart_conflict: [] }); }),
    http.get("*/api/v1/catalog/public", () => { catalogGets++; return installed && fail ? error() : HttpResponse.json(CATALOG); }),
    http.post("*/api/v1/apps/install-public", () => { installed = true; return HttpResponse.json({ job: JOB }, { status: 202 }); }),
  );
  const user = userEvent.setup();
  renderWithProviders(<DiscoverPanel />);
  await screen.findByRole("heading", { name: OFFICIAL.name });
  await user.click(screen.getByRole("button", { name: i18n.t("appAdd.installButton") }));
  await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: i18n.t("appAdd.installButton") }));
  await waitFor(() => { expect(catalogGets).toBeGreaterThan(1); expect(appGets).toBeGreaterThan(1); });
  if (fail) {
    await waitFor(() => expect(screen.queryByRole("heading", { name: OFFICIAL.name })).not.toBeInTheDocument());
    expect(screen.getAllByRole("alert").length).toBeGreaterThan(0);
  } else expect(await screen.findByRole("heading", { name: OFFICIAL.name })).toBeInTheDocument();
});

it("keeps the confirmed commit when the catalog changes while confirmation is open", async () => {
  handlers();
  const posted = vi.fn();
  server.use(http.post("*/api/v1/apps/install-public", async ({ request }) => { posted(await request.json()); return HttpResponse.json({ job: JOB }, { status: 202 }); }));
  const user = userEvent.setup();
  const { queryClient } = renderWithProviders(<DiscoverPanel />);
  await screen.findByRole("heading", { name: OFFICIAL.name });
  await user.click(screen.getByRole("button", { name: i18n.t("appAdd.installButton") }));
  server.use(http.get("*/api/v1/catalog/public", () => HttpResponse.json({ ...CATALOG, apps: [{ ...OFFICIAL, source: { ...OFFICIAL.source, ref: "v2", commit: "b".repeat(40) } }] })));
  await act(() => queryClient.refetchQueries());
  await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: i18n.t("appAdd.installButton") }));
  await waitFor(() => expect(posted).toHaveBeenCalledWith({ public_app_id: OFFICIAL.public.app_id, commit: OFFICIAL.source.commit }));
});

it("uses the public page URL supplied by the backend", async () => {
  handlers({ ...CATALOG, apps: [{ ...OFFICIAL, public_page_url: "https://catalog.example/apps/official-id" }] });
  renderWithProviders(<DiscoverPanel />);
  expect(await screen.findByRole("link", { name: i18n.t("appAdd.publicPageLink") })).toHaveAttribute("href", "https://catalog.example/apps/official-id");
});

it("opens the existing app instead of installing the same public identity", async () => {
  handlers();
  const onInstalled = vi.fn();
  const posted = vi.fn();
  let appGets = 0;
  server.use(
    http.get("*/api/v1/apps", () => HttpResponse.json({ apps: [{ id: "palmimo.old-name", status: "stopped", source: { public_app_id: ++appGets > 1 ? OFFICIAL.public.app_id : undefined } }], default_app: null, autostart_conflict: [] })),
    http.post("*/api/v1/apps/install-public", () => { posted(); return HttpResponse.json({ job: JOB }, { status: 202 }); }),
  );
  const user = userEvent.setup();
  renderWithProviders(<DiscoverPanel onInstalled={onInstalled} />);
  await user.click(await screen.findByRole("button", { name: i18n.t("appAdd.openInstalledButton") }));
  expect(onInstalled).toHaveBeenCalledWith("palmimo.old-name");
  expect(screen.getByText(i18n.t("appAdd.installedBadge"))).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: i18n.t("appAdd.installButton") })).not.toBeInTheDocument();
  expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  expect(posted).not.toHaveBeenCalled();
});
