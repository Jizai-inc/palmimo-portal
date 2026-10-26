import { X } from "lucide-react";
import type * as React from "react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { useListAppsApiV1AppsGet } from "@/api/generated/apps/apps";
import { useGetStatusApiV1WifiStatusGet } from "@/api/generated/wifi/wifi";
import { useGetPlatformApiV1PlatformGet } from "@/api/generated/platform/platform";
import { useGetStatusApiV1SystemStatusGet } from "@/api/generated/system/system";
import { useGetStatusApiV1UpdateStatusGet } from "@/api/generated/update/update";
import { AppHeader } from "@/components/AppHeader";
import { Breadcrumbs } from "@/components/Breadcrumbs";
import { DeviceMenu } from "@/components/HeaderMenus";
import { PageHeader } from "@/components/PageHeader";
import { PlatformUpdatePill } from "@/components/PlatformUpdatePill";
import { RunningAppControl } from "@/components/RunningAppControl";
import { SidebarFooter, SidebarNav } from "@/components/SidebarNav";
import { Button } from "@/components/ui/button";
import { useMediaQuery } from "@/lib/useMediaQuery";
import { cn } from "@/lib/utils";

/** How often to re-poll `update/status` for the nav badge -- cheap, auth-gated, no need for tighter than window-focus + a slow floor. */
const UPDATE_BADGE_REFETCH_INTERVAL_MS = 5 * 60 * 1000;

/** Fast while the running app is starting or stopping so the header settles; slow otherwise, to notice an app that exits on its own. */
const RUNNING_APP_SETTLING_POLL_MS = 2_000;
const RUNNING_APP_IDLE_POLL_MS = 15_000;

/** Tailwind's `md` breakpoint -- the same width at which the desktop sidebar replaces the mobile drawer in the markup below. */
const DESKTOP_QUERY = "(min-width: 768px)";

/**
 * Chrome for the dashboard family (Home, Apps, Settings). {@link SidebarNav} renders the nav as
 * a desktop sidebar (collapsible to an icon rail) and as a mobile slide-in drawer opened from
 * the header toggle. The header carries the update pills, the running app and the device menu;
 * the account menu and language switch sit in the sidebar footer.
 */
export function AppShell({
  title,
  description,
  actions,
  breadcrumbs,
  fitViewport = false,
  children,
}: {
  title?: string;
  description?: React.ReactNode;
  /** The page's primary action, right-aligned in the page header. */
  actions?: React.ReactNode;
  breadcrumbs?: React.ReactNode[];
  fitViewport?: boolean;
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  const isDesktop = useMediaQuery(DESKTOP_QUERY);
  // Desktop-only: shrinks the static sidebar to an icon rail. Never touched by the mobile drawer path.
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  // Mobile-only: opens the slide-in drawer. Never touched by the desktop sidebar path, and
  // forced closed the moment the viewport crosses into desktop width (see the effect below) --
  // e.g. rotating a phone to landscape closes the drawer instead of leaking its state into the
  // desktop sidebar.
  const [drawerOpen, setDrawerOpen] = useState(false);
  const drawerRef = useRef<HTMLDivElement>(null);
  const focusBeforeDrawerRef = useRef<HTMLElement | null>(null);
  const { data: wifiStatus } = useGetStatusApiV1WifiStatusGet();
  const { data: appsData } = useListAppsApiV1AppsGet();
  const defaultApp = appsData?.apps.find((app) => app.id === appsData.default_app) ?? null;
  const { data: systemStatus } = useGetStatusApiV1SystemStatusGet({
    query: {
      refetchInterval: (query) => {
        const state = query.state.data?.running_app?.state;
        return state && state !== "running" ? RUNNING_APP_SETTLING_POLL_MS : RUNNING_APP_IDLE_POLL_MS;
      },
    },
  });
  const { data: updateStatus } = useGetStatusApiV1UpdateStatusGet({
    query: {
      staleTime: UPDATE_BADGE_REFETCH_INTERVAL_MS,
      refetchInterval: UPDATE_BADGE_REFETCH_INTERVAL_MS,
      refetchOnWindowFocus: true,
    },
  });

  // `update/status` is a plain read; the badge never fires `update/check` itself, so it never
  // touches that endpoint's own rate limit (see UpdatePanel's "Check now" button).
  const updateAvailable = updateStatus?.update_available ?? false;
  const { data: platform } = useGetPlatformApiV1PlatformGet();
  const updateAttention = updateAvailable || platform?.ready === false;

  function handleToggleClick() {
    if (isDesktop) {
      setSidebarCollapsed((collapsed) => !collapsed);
      return;
    }
    if (!drawerOpen) {
      // About to open the drawer -- remember what had focus so closing can restore it,
      // mirroring the alert-dialog primitive's own focus-return behavior.
      focusBeforeDrawerRef.current = document.activeElement as HTMLElement | null;
    }
    setDrawerOpen((open) => !open);
  }

  function closeDrawer() {
    setDrawerOpen(false);
    focusBeforeDrawerRef.current?.focus();
  }

  // A viewport crossing into desktop width closes any open drawer rather than leaving it open
  // (and its document-level listeners attached) underneath the now-visible desktop sidebar.
  useEffect(() => {
    if (isDesktop) {
      setDrawerOpen(false);
    }
  }, [isDesktop]);

  // Moves focus into the drawer's first link once it opens; a plain fixed-position panel (not
  // a native <dialog>) doesn't do this on its own.
  useEffect(() => {
    if (!drawerOpen) {
      return;
    }
    drawerRef.current?.querySelector<HTMLElement>("a")?.focus();
  }, [drawerOpen]);

  // Locks background scroll while the drawer covers the viewport, restoring whatever value was
  // there before (rather than assuming "") so nested overflow rules aren't clobbered.
  useEffect(() => {
    if (!drawerOpen) {
      return undefined;
    }
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [drawerOpen]);

  // Escape closes the drawer, like the alert-dialog primitive it stands in for; Tab/Shift-Tab
  // are trapped inside it so a plain fixed-position panel (not a native <dialog>) still reads as
  // modal to a keyboard user. Attached only while the drawer is actually open on mobile -- never
  // while `isDesktop`, so it can't collide with an unrelated dialog (e.g. /power's AlertDialog).
  useEffect(() => {
    if (!drawerOpen || isDesktop) {
      return undefined;
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        closeDrawer();
        return;
      }
      if (event.key !== "Tab") {
        return;
      }
      const focusable = drawerRef.current?.querySelectorAll<HTMLElement>('a[href], button:not([disabled])');
      if (!focusable || focusable.length === 0) {
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drawerOpen, isDesktop]);

  const headerActions = (
    <>
      <PlatformUpdatePill />
      <span aria-hidden className="hidden h-5 w-px bg-border sm:block" />
      <RunningAppControl runningApp={systemStatus?.running_app} defaultApp={defaultApp} />
      <DeviceMenu
        hostname={systemStatus?.hostname ?? ""}
        network={[wifiStatus?.ssid, wifiStatus?.ip_address].filter(Boolean).join(" · ")}
        connected={systemStatus?.state === "connected"}
      />
    </>
  );

  return (
    <div className={cn("flex flex-col bg-background", fitViewport ? "h-dvh overflow-hidden [&>header]:shrink-0" : "min-h-screen")}>
      <AppHeader
        onToggleSidebar={handleToggleClick}
        actions={headerActions}
        showToggleBadge={updateAttention}
        updateAvailable={updateAvailable && platform?.ready !== false}
        showLanguageToggle={false}
        wordmarkLinksHome
      />
      <div className={cn("flex flex-1 md:flex-row", fitViewport && "min-h-0")}>
        <aside
          data-rail={sidebarCollapsed || undefined}
          className={cn(
            "hidden shrink-0 flex-col gap-4 border-r border-border bg-sidebar md:flex",
            sidebarCollapsed ? "w-14 px-2 py-3.5" : "w-58 px-2.5 py-3.5",
          )}
        >
          <SidebarNav rail={sidebarCollapsed} updateAvailable={updateAttention} idPrefix="sidebar" />
          <SidebarFooter rail={sidebarCollapsed} />
        </aside>
        {/* min-w-0: as a flex item, <main> otherwise refuses to be narrower than its widest
            unbreakable line (`truncate` does not shrink that), widening every page past a phone's viewport. */}
        <main className={cn("min-w-0 flex-1 px-5 py-6 md:px-12 md:py-10", fitViewport && "flex min-h-0 flex-col overflow-y-auto")}>
          <div className={cn("mx-auto flex w-full max-w-[1040px] flex-col gap-7", fitViewport && "min-h-0 flex-1")}>
            {title || breadcrumbs ? (
              <div className={cn("flex flex-col gap-2", fitViewport && "shrink-0")}>
                {breadcrumbs ? <Breadcrumbs items={breadcrumbs} label={t("common.breadcrumb")} /> : null}
                {title ? <PageHeader title={title} description={description} actions={actions} /> : null}
              </div>
            ) : null}
            {children}
          </div>
        </main>
      </div>

      {drawerOpen ? (
        <div className="fixed inset-0 z-50 md:hidden">
          <div data-testid="drawer-backdrop" className="fixed inset-0 bg-black/50" aria-hidden onClick={closeDrawer} />
          <div
            ref={drawerRef}
            role="dialog"
            aria-modal="true"
            aria-label={t("nav.primaryNavigation")}
            className="fixed inset-y-0 left-0 flex w-[280px] max-w-[80vw] flex-col gap-4 overscroll-contain border-r border-border bg-sidebar p-4 shadow-lg"
          >
            <div className="flex items-center justify-end px-2">
              <Button type="button" variant="ghost" size="icon" aria-label={t("nav.closeMenu")} onClick={closeDrawer}>
                <X className="size-4" />
              </Button>
            </div>
            <SidebarNav updateAvailable={updateAttention} onNavigate={() => setDrawerOpen(false)} idPrefix="drawer" />
            <SidebarFooter />
          </div>
        </div>
      ) : null}
    </div>
  );
}
