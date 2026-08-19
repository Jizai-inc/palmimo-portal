import type { LucideIcon } from "lucide-react";
import { Download, KeyRound, LayoutDashboard, Power, Wifi } from "lucide-react";

/** One entry in the dashboard family's navigation (mobile tab bar, desktop sidebar, dashboard quick-action list). */
export interface NavItem {
  to: string;
  icon: LucideIcon;
  labelKey: string;
  /** Not set for the dashboard entry itself; that list excludes it. */
  descriptionKey?: string;
}

/**
 * Single source of truth for the dashboard family's navigation -- rendered by
 * the mobile tab bar and desktop sidebar (components/AppShell.tsx), and by
 * the dashboard's own quick-actions list (routes/dashboard.tsx, minus itself).
 * `authGate.ts`'s `DASHBOARD_FAMILY_PATHS` derives from this list so the two can't drift.
 */
export const NAV_ITEMS: readonly NavItem[] = [
  { to: "/dashboard", icon: LayoutDashboard, labelKey: "nav.dashboard" },
  { to: "/wifi-settings", icon: Wifi, labelKey: "nav.wifi", descriptionKey: "nav.wifiDescription" },
  { to: "/ssh-keys", icon: KeyRound, labelKey: "nav.sshKeys", descriptionKey: "nav.sshKeysDescription" },
  { to: "/power", icon: Power, labelKey: "nav.power", descriptionKey: "nav.powerDescription" },
  { to: "/update", icon: Download, labelKey: "nav.update", descriptionKey: "nav.updateDescription" },
] as const;

/** Whether `to` is the current route -- exact match, since none of these routes nest further. */
export function isActive(pathname: string, to: string): boolean {
  return pathname === to;
}
