import { Link } from "@tanstack/react-router";
import { PanelLeft } from "lucide-react";
import type * as React from "react";
import { useTranslation } from "react-i18next";

import { LanguageToggle } from "@/components/LanguageToggle";
import { Button } from "@/components/ui/button";

/**
 * Chrome shared by every screen: a 56px header with the wordmark on the
 * left and the {@link LanguageToggle} on the right. Used directly by
 * {@link import("./AuthShell").AuthShell} and by
 * {@link import("./AppShell").AppShell}, which layers on the sidebar-collapse
 * toggle and the logout button below.
 */
export function AppHeader({
  onToggleSidebar,
  logoutSlot,
  wordmarkLinksHome = false,
}: {
  /** Renders the desktop-only `panel-left` sidebar-collapse toggle when set. Only `AppShell` passes this. */
  onToggleSidebar?: () => void;
  /** Renders in the header's right side, desktop only (e.g. the logout button). Only `AppShell` passes this. */
  logoutSlot?: React.ReactNode;
  /**
   * Renders the wordmark as a `Link to="/"` when true. Defaults to false so
   * {@link import("./AuthShell").AuthShell} (setup / login / change-password
   * / wifi / wifi.waiting / status-error) keeps the wordmark inert: a tap
   * there would navigate to `/`, the auth gate would bounce back, and the
   * remount would wipe mid-entry form state (e.g. a typed password). Only
   * `AppShell` (the authed shell) passes true.
   */
  wordmarkLinksHome?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <header className="flex h-14 items-center gap-2 border-b border-border bg-background px-4">
      {onToggleSidebar ? (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="hidden md:inline-flex"
          aria-label={t("app.toggleSidebar")}
          onClick={onToggleSidebar}
        >
          <PanelLeft className="size-4" />
        </Button>
      ) : null}
      {wordmarkLinksHome ? (
        <Link to="/" className="font-semibold hover:opacity-80">
          {t("app.wordmark")}
        </Link>
      ) : (
        <span className="font-semibold">{t("app.wordmark")}</span>
      )}
      <div className="ml-auto flex items-center gap-2">
        <LanguageToggle />
        {logoutSlot ? <div className="hidden md:block">{logoutSlot}</div> : null}
      </div>
    </header>
  );
}
