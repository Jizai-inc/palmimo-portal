import { Link } from "@tanstack/react-router";
import { CircleArrowDown, PanelLeft } from "lucide-react";
import type * as React from "react";
import { useTranslation } from "react-i18next";

import logoDark from "@/assets/palmimo-devkit-logo-white.png";
import logoLight from "@/assets/palmimo-devkit-logo.png";
import { LanguageToggle } from "@/components/LanguageToggle";
import { UpdateDot } from "@/components/UpdateDot";
import { Button } from "@/components/ui/button";

/**
 * Swaps by {@link index.css}'s `prefers-color-scheme` breakpoint, the only
 * dark-mode mechanism this app has (no `.dark` class, no theme toggle). Both
 * images carry `aria-hidden`, since CSS-only visibility (`dark:hidden`)
 * isn't something the accessible-name computation can rely on; the wrapping
 * `role="img"` element carries the one accessible name instead.
 */
function Wordmark() {
  const { t } = useTranslation();
  const alt = t("app.wordmark");
  return (
    <span role="img" aria-label={alt}>
      <img src={logoLight} alt={alt} aria-hidden="true" className="block h-6 w-auto dark:hidden" />
      <img src={logoDark} alt={alt} aria-hidden="true" className="hidden h-6 w-auto dark:block" />
    </span>
  );
}

/**
 * Chrome shared by every screen: a 56px header with the wordmark on the
 * left and the {@link LanguageToggle} on the right. Used directly by
 * {@link import("./AuthShell").AuthShell} and by
 * {@link import("./AppShell").AppShell}, which adds the nav toggle, the
 * update badge, and the device and account menus.
 */
export function AppHeader({
  onToggleSidebar,
  actions,
  showToggleBadge,
  updateAvailable = false,
  wordmarkLinksHome = false,
  showLanguageToggle = true,
}: {
  /** Renders the `panel-left` nav toggle when set -- collapses the sidebar on desktop, opens the drawer on mobile. */
  onToggleSidebar?: () => void;
  /** Rendered after the language toggle on every viewport. */
  actions?: React.ReactNode;
  /** The dashboard moves the toggle into the sidebar footer; auth screens keep it here. */
  showLanguageToggle?: boolean;
  /** Renders an {@link UpdateDot} pinned to the toggle button plus its accessible announcement. Ignored when `onToggleSidebar` is unset. */
  showToggleBadge?: boolean;
  /** Renders a link to Settings → Software update. */
  updateAvailable?: boolean;
  /**
   * Renders the wordmark as a `Link to="/"` when true. Defaults to false so
   * {@link import("./AuthShell").AuthShell} keeps the wordmark inert: a tap
   * there would navigate to `/`, the auth gate would bounce back, and the
   * remount would wipe mid-entry form state (e.g. a typed password).
   */
  wordmarkLinksHome?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <header className="flex h-14 items-center gap-2 border-b border-border bg-background px-3 sm:gap-3 sm:px-5">
      {onToggleSidebar ? (
        <div className="relative">
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={t("app.toggleSidebar")}
            onClick={onToggleSidebar}
          >
            <PanelLeft className="size-4" />
          </Button>
          {showToggleBadge ? (
            <>
              <UpdateDot className="-top-0.5 -right-0.5" />
              <span className="sr-only">{t("nav.updateAvailable")}</span>
            </>
          ) : null}
        </div>
      ) : null}
      {wordmarkLinksHome ? (
        <Link to="/" className="hover:opacity-80">
          <Wordmark />
        </Link>
      ) : (
        <Wordmark />
      )}
      <div className="ml-auto flex min-w-0 items-center gap-1 text-[13px] sm:gap-3">
        {updateAvailable ? (
          <Link
            to="/settings/update"
            className="flex items-center gap-1.5 rounded-full bg-secondary px-2.5 py-1 font-semibold hover:bg-accent"
          >
            <CircleArrowDown className="size-3.5" aria-hidden />
            <span className="sr-only sm:not-sr-only">{t("nav.updateAvailable")}</span>
          </Link>
        ) : null}
        {showLanguageToggle ? <LanguageToggle /> : null}
        {actions}
      </div>
    </header>
  );
}
