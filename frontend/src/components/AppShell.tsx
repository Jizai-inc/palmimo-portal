import { useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useNavigate } from "@tanstack/react-router";
import { X } from "lucide-react";
import type * as React from "react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { useGetStatusApiV1WifiStatusGet } from "@/api/generated/wifi/wifi";
import { useGetStatusApiV1SystemStatusGet } from "@/api/generated/system/system";
import { useGetStatusApiV1UpdateStatusGet } from "@/api/generated/update/update";
import { useLogoutApiV1AuthLogoutPost } from "@/api/generated/auth/auth";
import { AppHeader } from "@/components/AppHeader";
import { UpdateDot } from "@/components/UpdateDot";
import { Button } from "@/components/ui/button";
import { isActive, NAV_ITEMS, type NavItem } from "@/lib/navigation";
import { navLabel } from "@/lib/navLabels";
import { useMediaQuery } from "@/lib/useMediaQuery";
import { cn } from "@/lib/utils";

/** How often to re-poll `update/status` for the nav badge -- cheap, auth-gated, no need for tighter than window-focus + a slow floor. */
const UPDATE_BADGE_REFETCH_INTERVAL_MS = 5 * 60 * 1000;

/** Tailwind's `md` breakpoint -- the same width at which the desktop sidebar replaces the mobile drawer in the markup below. */
const DESKTOP_QUERY = "(min-width: 768px)";

/**
 * Chrome for the dashboard family (`/dashboard`, `/ssh-keys`, `/power`, Wi-Fi settings, update).
 * `src/lib/navigation.ts`'s `NAV_ITEMS` is the single source of truth for the nav, rendered
 * identically as a desktop sidebar and (via the same {@link SidebarLink} entries) a mobile
 * slide-in drawer opened from the header toggle -- one nav model on both form factors, so it
 * scales with the list's length rather than needing a tab-count budget. Owns logout itself
 * rather than each page calling it directly.
 */
export function AppShell({
  title,
  subtitle,
  mobileHeaderAction,
  children,
}: {
  title: string;
  subtitle?: string;
  /** Rendered beside the title, mobile only -- the dashboard's own "log out" button (see routes/dashboard.tsx). */
  mobileHeaderAction?: React.ReactNode;
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const isDesktop = useMediaQuery(DESKTOP_QUERY);
  // Desktop-only: collapses the static sidebar. Never touched by the mobile drawer path.
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  // Mobile-only: opens the slide-in drawer. Never touched by the desktop sidebar path, and
  // forced closed the moment the viewport crosses into desktop width (see the effect below) --
  // e.g. rotating a phone to landscape closes the drawer instead of leaking its state into the
  // desktop sidebar.
  const [drawerOpen, setDrawerOpen] = useState(false);
  const drawerRef = useRef<HTMLDivElement>(null);
  const focusBeforeDrawerRef = useRef<HTMLElement | null>(null);
  const { data: wifiStatus } = useGetStatusApiV1WifiStatusGet();
  const { data: systemStatus } = useGetStatusApiV1SystemStatusGet();
  const { data: updateStatus } = useGetStatusApiV1UpdateStatusGet({
    query: {
      staleTime: UPDATE_BADGE_REFETCH_INTERVAL_MS,
      refetchInterval: UPDATE_BADGE_REFETCH_INTERVAL_MS,
      refetchOnWindowFocus: true,
    },
  });
  const logout = useLogoutApiV1AuthLogoutPost();

  // `update/status` is a plain read; the badge never fires `update/check` itself, so it never
  // touches that endpoint's own rate limit (see UpdatePanel's "Check now" button).
  const updateAvailable = updateStatus?.update_available ?? false;

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

  const logoutButton = (
    <Button
      variant="ghost"
      onClick={() =>
        logout.mutate(undefined, {
          onSuccess: () => {
            // No screen after this point may keep rendering pre-logout auth state from cache.
            queryClient.clear();
            void navigate({ to: "/login" });
          },
        })
      }
      disabled={logout.isPending}
    >
      {t("common.logout")}
    </Button>
  );

  return (
    <div className="flex min-h-screen flex-col bg-background">
      <AppHeader onToggleSidebar={handleToggleClick} logoutSlot={logoutButton} showToggleBadge={updateAvailable} />
      <div className="flex flex-1 md:flex-row">
        {sidebarCollapsed ? null : (
          <aside className="hidden w-60 shrink-0 flex-col gap-4 border-r border-border bg-muted/40 p-4 md:flex">
            <p className="px-2 text-xs font-medium text-muted-foreground">{t("nav.deviceSection")}</p>
            <nav className="flex flex-col gap-1" aria-label={t("nav.primaryNavigation")}>
              {NAV_ITEMS.map((item) => (
                <SidebarLink
                  key={item.to}
                  item={item}
                  active={isActive(location.pathname, item.to)}
                  showBadge={item.to === "/update" && updateAvailable}
                />
              ))}
            </nav>
            <div className="mt-auto flex flex-col gap-1 rounded-lg bg-accent/60 p-3 text-xs">
              <div className="flex items-center gap-2">
                <span className="size-2.5 rounded-full bg-green-500" aria-hidden />
                <span className="font-medium">{systemStatus?.hostname ?? ""}</span>
              </div>
              <p className="text-muted-foreground">
                {[wifiStatus?.ssid, wifiStatus?.ip_address].filter(Boolean).join(" · ")}
              </p>
            </div>
          </aside>
        )}
        <main className="flex-1 p-5 md:p-8 lg:p-10">
          <div className="mx-auto flex max-w-[960px] flex-col gap-6">
            <div className="flex items-center justify-between gap-2">
              <div className="flex flex-col gap-1">
                <h1 className="text-2xl font-semibold">{title}</h1>
                {subtitle ? <p className="text-sm text-muted-foreground">{subtitle}</p> : null}
              </div>
              {mobileHeaderAction ? <div className="md:hidden">{mobileHeaderAction}</div> : null}
            </div>
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
            className="fixed inset-y-0 left-0 flex w-[280px] max-w-[80vw] flex-col gap-4 overscroll-contain border-r border-border bg-background p-4 shadow-lg"
          >
            <div className="flex items-center justify-between px-2">
              <p className="text-xs font-medium text-muted-foreground">{t("nav.deviceSection")}</p>
              <Button type="button" variant="ghost" size="icon" aria-label={t("nav.closeMenu")} onClick={closeDrawer}>
                <X className="size-4" />
              </Button>
            </div>
            <nav className="flex flex-col gap-1" aria-label={t("nav.primaryNavigation")}>
              {NAV_ITEMS.map((item) => (
                <SidebarLink
                  key={item.to}
                  item={item}
                  active={isActive(location.pathname, item.to)}
                  showBadge={item.to === "/update" && updateAvailable}
                  onNavigate={() => setDrawerOpen(false)}
                />
              ))}
            </nav>
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** One nav entry -- icon, label, and an optional update-available dot. Shared by the desktop sidebar and the mobile drawer, both rendered from `NAV_ITEMS`. */
function SidebarLink({
  item,
  active,
  showBadge,
  onNavigate,
}: {
  item: NavItem;
  active: boolean;
  showBadge: boolean;
  /** Closes the mobile drawer after navigating; unset for the desktop sidebar, which has no drawer to close. */
  onNavigate?: () => void;
}) {
  const { t } = useTranslation();
  const Icon = item.icon;
  return (
    <Link
      to={item.to}
      aria-current={active ? "page" : undefined}
      onClick={onNavigate}
      className={cn(
        "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm",
        active ? "bg-accent font-semibold text-accent-foreground" : "text-muted-foreground hover:bg-accent/50",
      )}
    >
      <span className="relative flex items-center">
        <Icon className="size-4" />
        {showBadge ? <UpdateDot className="-right-1 -top-1 size-1.5" /> : null}
      </span>
      {navLabel(t, item.labelKey)}
      {showBadge ? <span className="sr-only"> {t("nav.updateAvailable")}</span> : null}
    </Link>
  );
}
