import { act, screen, waitFor, within } from "@testing-library/react";
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
  name: "dance", description: "Community dance", translations: {}, env: [], devices: [],
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
