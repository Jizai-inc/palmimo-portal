import type { LucideIcon } from "lucide-react";
import { Cpu, Download, House, KeyRound, Package, Settings, SquareTerminal, Wifi } from "lucide-react";

/** One entry in a navigation list (sidebar, drawer, dashboard quick actions). */
export interface NavItem {
  to: string;
  icon: LucideIcon;
  labelKey: string;
  descriptionKey?: string;
  /** Rendered as an expandable group; the parent links to the first child. */
  children?: readonly NavItem[];
}

export const SETTINGS_ITEMS: readonly NavItem[] = [
  { to: "/settings/network", icon: Wifi, labelKey: "nav.network", descriptionKey: "nav.networkDescription" },
  { to: "/settings/secrets", icon: KeyRound, labelKey: "nav.secrets", descriptionKey: "nav.secretsDescription" },
  { to: "/settings/update", icon: Download, labelKey: "nav.softwareUpdate", descriptionKey: "nav.softwareUpdateDescription" },
  { to: "/settings/device", icon: Cpu, labelKey: "nav.device", descriptionKey: "nav.deviceDescription" },
  { to: "/settings/developer", icon: SquareTerminal, labelKey: "nav.developer", descriptionKey: "nav.developerDescription" },
] as const;

/**
 * The dashboard family's top-level navigation, rendered by `AppShell`'s sidebar and drawer.
 * `authGate.ts`'s `DASHBOARD_FAMILY_PATHS` derives from this list so the two can't drift.
 */
export const NAV_ITEMS: readonly NavItem[] = [
  { to: "/dashboard", icon: House, labelKey: "nav.dashboard" },
  { to: "/apps", icon: Package, labelKey: "nav.apps", descriptionKey: "nav.appsDescription" },
  { to: "/settings", icon: Settings, labelKey: "nav.settings", children: SETTINGS_ITEMS },
] as const;

/** Stand-in until the docs site exists; rendered as an external link, never routed. */
export const DOCS_URL = "https://github.com/Jizai-inc";

/** Whether `to` is the current route or an ancestor of it (`/settings` stays active on `/settings/device`). */
export function isActive(pathname: string, to: string): boolean {
  return pathname === to || pathname.startsWith(`${to}/`);
}
