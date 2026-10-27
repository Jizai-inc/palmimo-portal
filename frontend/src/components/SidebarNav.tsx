import { Link, useLocation } from "@tanstack/react-router";
import { BookOpen, ChevronRight, ExternalLink } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { AccountMenu } from "@/components/HeaderMenus";
import { UpdateDot } from "@/components/UpdateDot";
import { DOCS_URL, isActive, NAV_ITEMS, type NavItem } from "@/lib/navigation";
import { navLabel } from "@/lib/navLabels";
import { cn } from "@/lib/utils";

/** The entry that carries the update-available dot; a collapsed group containing it shows the dot instead. */
const UPDATE_PATH = "/settings/update";

// Row, pill and sub-list styles follow shadcn/ui's Sidebar (SidebarMenuButton / SidebarMenuSub).
const ROW = "flex h-9 w-full items-center gap-2.5 rounded-lg px-2.5 text-left text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";
const PILL = "bg-accent font-semibold text-accent-foreground";
const IDLE = "font-medium text-foreground hover:bg-accent/60";

function storageKey(groupTo: string) {
  return `portal.navExpanded:${groupTo}`;
}

function readExpanded(groupTo: string): boolean {
  try {
    return localStorage.getItem(storageKey(groupTo)) !== "false";
  } catch {
    return true;
  }
}

function writeExpanded(groupTo: string, expanded: boolean) {
  try {
    localStorage.setItem(storageKey(groupTo), String(expanded));
  } catch {
    // Storage blocked: the choice just doesn't outlive this page.
  }
}

/**
 * The primary nav, shared by the desktop sidebar and the mobile drawer. Exactly one entry wears
 * the pill: the current page's own entry, or in `rail` mode (children hidden) its group's icon.
 */
export function SidebarNav({
  rail = false,
  updateAvailable,
  onNavigate,
  idPrefix,
}: {
  rail?: boolean;
  updateAvailable: boolean;
  onNavigate?: () => void;
  /** Keeps `aria-controls` ids unique while the sidebar and the drawer are both in the DOM. */
  idPrefix: string;
}) {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  return (
    <nav className="flex flex-col gap-0.5" aria-label={t("nav.primaryNavigation")}>
      {NAV_ITEMS.map((item) =>
        item.children ? (
          <NavGroup
            key={item.to}
            item={item}
            items={item.children}
            pathname={pathname}
            rail={rail}
            updateAvailable={updateAvailable}
            onNavigate={onNavigate}
            idPrefix={idPrefix}
          />
        ) : (
          <NavRow
            key={item.to}
            item={item}
            to={item.to}
            active={isActive(pathname, item.to)}
            rail={rail}
            showBadge={updateAvailable && item.to === UPDATE_PATH}
            onNavigate={onNavigate}
          />
        ),
      )}
      <DocsLink rail={rail} />
    </nav>
  );
}

function NavRow({
  item,
  to,
  active,
  rail,
  showBadge,
  onNavigate,
}: {
  item: NavItem;
  to: string;
  active: boolean;
  rail: boolean;
  showBadge: boolean;
  onNavigate?: () => void;
}) {
  const { t } = useTranslation();
  const Icon = item.icon;
  const label = navLabel(t, item.labelKey);
  return (
    <Link
      to={to}
      aria-current={active ? "page" : undefined}
      onClick={onNavigate}
      title={rail ? label : undefined}
      className={cn(ROW, rail && "justify-center px-0", active ? PILL : IDLE)}
    >
      <span className="relative flex items-center">
        <Icon className="size-4" />
        {showBadge ? <UpdateDot className="-right-1 -top-1 size-1.5" /> : null}
      </span>
      <span className={cn(rail && "sr-only")}>{label}</span>
      {showBadge ? <span className="sr-only"> {t("nav.updateAvailable")}</span> : null}
    </Link>
  );
}

function NavGroup({
  item,
  items,
  pathname,
  rail,
  updateAvailable,
  onNavigate,
  idPrefix,
}: {
  item: NavItem;
  items: readonly NavItem[];
  pathname: string;
  rail: boolean;
  updateAvailable: boolean;
  onNavigate?: () => void;
  idPrefix: string;
}) {
  const { t } = useTranslation();
  const containsActive = isActive(pathname, item.to);
  const [expanded, setExpanded] = useState(() => containsActive || readExpanded(item.to));
  const hasBadgedChild = updateAvailable && items.some((child) => child.to === UPDATE_PATH);

  useEffect(() => {
    if (containsActive) setExpanded(true);
  }, [containsActive]);

  if (rail) {
    return (
      <NavRow item={item} to={items[0].to} active={containsActive} rail showBadge={hasBadgedChild} onNavigate={onNavigate} />
    );
  }

  const listId = `${idPrefix}${item.to.replaceAll("/", "-")}-items`;
  const Icon = item.icon;
  const showParentBadge = hasBadgedChild && !expanded;

  function setAndRemember(next: boolean) {
    setExpanded(next);
    writeExpanded(item.to, next);
  }

  return (
    <div className="flex flex-col">
      <button
        type="button"
        aria-expanded={expanded}
        aria-controls={listId}
        onClick={() => setAndRemember(!expanded)}
        className={cn(ROW, IDLE, "font-semibold")}
      >
        <span className="relative flex items-center">
          <Icon className="size-4" />
          {showParentBadge ? <UpdateDot className="-right-1 -top-1 size-1.5" /> : null}
        </span>
        <span className="min-w-0 flex-1 truncate">{navLabel(t, item.labelKey)}</span>
        {showParentBadge ? <span className="sr-only">{t("nav.updateAvailable")}</span> : null}
        <ChevronRight aria-hidden className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform duration-200", expanded && "rotate-90")} />
      </button>
      <ul id={listId} hidden={!expanded} className="ml-[18px] mt-0.5 flex min-w-0 flex-col gap-0.5 border-l border-border pl-2 py-0.5">
        {items.map((child) => {
          const active = isActive(pathname, child.to);
          const showBadge = updateAvailable && child.to === UPDATE_PATH;
          return (
            <li key={child.to}>
              <Link
                to={child.to}
                aria-current={active ? "page" : undefined}
                onClick={onNavigate}
                className={cn(
                  "flex h-8 min-w-0 items-center justify-between gap-2 rounded-md px-2.5 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  active ? PILL : "text-foreground/75 hover:bg-accent/60 hover:text-foreground",
                )}
              >
                <span className="truncate">{navLabel(t, child.labelKey)}</span>
                {showBadge ? (
                  <>
                    <span aria-hidden className="size-1.5 shrink-0 rounded-full bg-warning" />
                    <span className="sr-only">{t("nav.updateAvailable")}</span>
                  </>
                ) : null}
              </Link>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function DocsLink({ rail }: { rail: boolean }) {
  const { t } = useTranslation();
  const label = t("nav.docs");
  return (
    <a href={DOCS_URL} target="_blank" rel="noreferrer" title={rail ? label : undefined} className={cn(ROW, IDLE, rail && "justify-center px-0")}>
      <BookOpen className="size-4" />
      <span className={cn(rail && "sr-only")}>{label}</span>
      {rail ? null : <ExternalLink className="size-3 text-muted-foreground" aria-hidden />}
      <span className="sr-only">{t("nav.opensInNewTab")}</span>
    </a>
  );
}

/** Pinned to the bottom of the sidebar and drawer: the account menu. */
export function SidebarFooter({ rail = false }: { rail?: boolean }) {
  return (
    <div className={cn("mt-auto flex items-center gap-2 border-t border-border pt-3", rail ? "flex-col px-0" : "px-1")}>
      <AccountMenu compact={rail} />
    </div>
  );
}
