import { useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useNavigate } from "@tanstack/react-router";
import type * as React from "react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useGetStatusApiV1WifiStatusGet } from "@/api/generated/wifi/wifi";
import { useGetStatusApiV1SystemStatusGet } from "@/api/generated/system/system";
import { useLogoutApiV1AuthLogoutPost } from "@/api/generated/auth/auth";
import { AppHeader } from "@/components/AppHeader";
import { Button } from "@/components/ui/button";
import { isActive, NAV_ITEMS } from "@/lib/navigation";
import { navLabel } from "@/lib/navLabels";
import { cn } from "@/lib/utils";

/**
 * Chrome for the dashboard family (`/dashboard`, `/ssh-keys`, `/power`, Wi-Fi settings, update).
 * `src/lib/navigation.ts`'s `NAV_ITEMS` is the single source of truth for the nav both this
 * shell and the dashboard's own quick-actions list render from. Owns logout itself rather than
 * each page calling it directly.
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
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const { data: wifiStatus } = useGetStatusApiV1WifiStatusGet();
  const { data: systemStatus } = useGetStatusApiV1SystemStatusGet();
  const logout = useLogoutApiV1AuthLogoutPost();

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
      <AppHeader onToggleSidebar={() => setSidebarCollapsed((collapsed) => !collapsed)} logoutSlot={logoutButton} />
      <div className="flex flex-1 md:flex-row">
        {sidebarCollapsed ? null : (
          <aside className="hidden w-60 shrink-0 flex-col gap-4 border-r border-border bg-muted/40 p-4 md:flex">
            <p className="px-2 text-xs font-medium text-muted-foreground">{t("nav.deviceSection")}</p>
            <nav className="flex flex-col gap-1" aria-label={t("nav.primaryNavigation")}>
              {NAV_ITEMS.map((item) => {
                const Icon = item.icon;
                const active = isActive(location.pathname, item.to);
                return (
                  <Link
                    key={item.to}
                    to={item.to}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm",
                      active ? "bg-accent font-semibold text-accent-foreground" : "text-muted-foreground hover:bg-accent/50",
                    )}
                  >
                    <Icon className="size-4" />
                    {navLabel(t, item.labelKey)}
                  </Link>
                );
              })}
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
        <main className="flex-1 p-5 pb-20 md:p-8 md:pb-10 lg:p-10">
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

      <nav
        className="fixed inset-x-0 bottom-0 flex border-t border-border bg-background pb-[env(safe-area-inset-bottom)] md:hidden"
        aria-label={t("nav.primaryNavigation")}
      >
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;
          const active = isActive(location.pathname, item.to);
          return (
            <Link
              key={item.to}
              to={item.to}
              aria-current={active ? "page" : undefined}
              className="flex flex-1 flex-col items-center gap-0.5 py-2"
            >
              <span
                className={cn(
                  "flex h-[26px] w-11 items-center justify-center rounded-full",
                  active ? "bg-accent" : undefined,
                )}
              >
                <Icon className={cn("size-5", active ? "font-semibold text-foreground" : "text-muted-foreground")} />
              </span>
              <span className={cn("text-[11px]", active ? "font-semibold text-foreground" : "text-muted-foreground")}>
                {navLabel(t, item.labelKey)}
              </span>
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
