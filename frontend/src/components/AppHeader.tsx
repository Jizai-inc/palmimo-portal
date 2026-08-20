import { PanelLeft } from "lucide-react";
import type * as React from "react";
import { useTranslation } from "react-i18next";

import { LanguageToggle } from "@/components/LanguageToggle";
import { Button } from "@/components/ui/button";

/**
 * Chrome shared by every screen: a 56px header with the wordmark on the
 * left and the {@link LanguageToggle} on the right. Used directly by
 * {@link import("./AuthShell").AuthShell} and by
 * {@link import("./AppShell").AppShell}, which layers on the nav toggle
 * (desktop sidebar collapse / mobile drawer) and the logout button below.
 */
export function AppHeader({
  onToggleSidebar,
  logoutSlot,
}: {
  /** Renders the `panel-left` nav toggle when set -- collapses the sidebar on desktop, opens the drawer on mobile. Only `AppShell` passes this. */
  onToggleSidebar?: () => void;
  /** Renders in the header's right side, desktop only (e.g. the logout button). Only `AppShell` passes this. */
  logoutSlot?: React.ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <header className="flex h-14 items-center gap-2 border-b border-border bg-background px-4">
      {onToggleSidebar ? (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={t("app.toggleSidebar")}
          onClick={onToggleSidebar}
        >
          <PanelLeft className="size-4" />
        </Button>
      ) : null}
      <span className="font-semibold">{t("app.wordmark")}</span>
      <div className="ml-auto flex items-center gap-2">
        <LanguageToggle />
        {logoutSlot ? <div className="hidden md:block">{logoutSlot}</div> : null}
      </div>
    </header>
  );
}
