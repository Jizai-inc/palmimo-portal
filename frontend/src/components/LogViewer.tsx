import { Check, SlidersHorizontal } from "lucide-react";
import { memo, useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { JournalEntryInfo, JournalInvocationInfo } from "@/api/generated/models";
import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/copyText";
import { formatLogMessage } from "@/lib/formatLogMessage";
import { formatLocalTimestamp } from "@/lib/formatTimestamp";
import { useLogDisplay } from "@/lib/useLogDisplay";
import { cn } from "@/lib/utils";

const messageCaches = { compact: new WeakMap<JournalEntryInfo, string>(), pretty: new WeakMap<JournalEntryInfo, string>() };

function displayedMessage(entry: JournalEntryInfo, pretty: boolean): string {
  const cache = pretty ? messageCaches.pretty : messageCaches.compact;
  let message = cache.get(entry);
  if (message === undefined) {
    message = formatLogMessage(entry.message, pretty);
    cache.set(entry, message);
  }
  return message;
}

export function LogViewer({
  entries, text, droppedCount = 0, expanded = false, unavailable = false, invocations = [], invocation = null, setInvocation, toolbar,
}: {
  entries: JournalEntryInfo[];
  text: string;
  droppedCount?: number;
  expanded?: boolean;
  unavailable?: boolean;
  invocations?: JournalInvocationInfo[];
  invocation?: string | null;
  setInvocation?: (id: string) => void;
  toolbar?: ReactNode;
}) {
  const { t, i18n } = useTranslation();
  const { wrap, pretty, updateDisplay } = useLogDisplay();
  const [follow, setFollow] = useState(true);
  const [menuOpen, setMenuOpen] = useState(false);
  const [copyStatus, setCopyStatus] = useState<"idle" | "copied" | "failed">("idle");
  const copyTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const menuWrapperRef = useRef<HTMLDivElement>(null);
  const anchor = useRef<{ key: string; delta: number } | null>(null);
  const lastInvocation = useRef(invocation);
  const previousTop = useRef(0);
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuId = useId();

  const rememberAnchor = useCallback(() => {
    const node = scrollRef.current;
    if (!node) return;
    const rows = Array.from(node.querySelectorAll<HTMLDivElement>("[data-log-key]"));
    const row = rows.find((row) => row.offsetTop + row.offsetHeight > node.scrollTop);
    anchor.current = row ? { key: row.dataset.logKey!, delta: row.offsetTop - node.scrollTop } : null;
  }, []);

  const reconcileScroll = useCallback(() => {
    const node = scrollRef.current;
    if (!node) return;
    const switched = lastInvocation.current !== invocation;
    if (switched) {
      lastInvocation.current = invocation;
      anchor.current = null;
      setFollow(true);
    }
    if (follow || switched) {
      node.scrollTop = node.scrollHeight;
    } else if (anchor.current) {
      const row = node.querySelector<HTMLDivElement>(`[data-log-key="${anchor.current.key}"]`);
      if (row) node.scrollTop = row.offsetTop - anchor.current.delta;
    }
    // Programmatic movement and browser clamping must not disable following.
    previousTop.current = node.scrollTop;
    rememberAnchor();
  }, [follow, invocation, rememberAnchor]);

  useLayoutEffect(reconcileScroll, [reconcileScroll, entries, droppedCount, wrap, pretty, unavailable]);
  useLayoutEffect(() => {
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(reconcileScroll);
    if (scrollRef.current) observer.observe(scrollRef.current);
    if (contentRef.current) observer.observe(contentRef.current);
    return () => observer.disconnect();
  }, [reconcileScroll, unavailable]);

  useEffect(() => {
    if (!menuOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!menuWrapperRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [menuOpen]);

  useEffect(() => () => clearTimeout(copyTimer.current), []);
  useEffect(() => {
    if (menuOpen) menuRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
  }, [menuOpen]);

  async function handleCopy() {
    const ok = await copyText(text);
    setCopyStatus(ok ? "copied" : "failed");
    clearTimeout(copyTimer.current);
    copyTimer.current = setTimeout(() => setCopyStatus("idle"), 2000);
  }

  const options = [
    { label: t("appDetail.logsWrap"), checked: wrap, toggle: () => updateDisplay({ wrap: !wrap, pretty }) },
    { label: t("appDetail.logsFollow"), checked: follow, toggle: () => setFollow(!follow) },
    { label: t("appDetail.logsPretty"), checked: pretty, toggle: () => updateDisplay({ wrap, pretty: !pretty }) },
  ];

  return (
    <div className={cn("flex min-h-0 min-w-0 flex-col gap-2", expanded && "flex-1")}>
      <div className="flex shrink-0 flex-wrap items-center gap-2">
        {invocations.length > 1 && invocation !== null ? (
          <select aria-label={t("appDetail.logsInvocationLabel")} className="h-9 max-w-full rounded-md border border-input bg-transparent px-2 text-sm" value={invocation} onChange={(event) => setInvocation?.(event.target.value)}>
            {invocations.map((start) => (
              <option key={start.id} value={start.id}>
                {t("appDetail.logsInvocationAt", { time: formatLocalTimestamp(start.started_at, { locale: i18n.language }) })}
              </option>
            ))}
          </select>
        ) : null}
        <Button type="button" variant="outline" size="sm" onClick={() => void handleCopy()}>
          {copyStatus === "copied" ? t("appDetail.logsCopied") : copyStatus === "failed" ? t("appDetail.logsCopyFailed") : t("appDetail.logsCopyButton")}
        </Button>
        {toolbar}
        <div ref={menuWrapperRef} className="relative" onBlur={(event) => {
          if (event.relatedTarget !== null && !event.currentTarget.contains(event.relatedTarget)) setMenuOpen(false);
        }} onKeyDown={(event) => {
          if (event.key === "Escape") {
            setMenuOpen(false);
            triggerRef.current?.focus();
          }
        }}>
          <Button ref={triggerRef} type="button" variant="outline" size="sm" aria-haspopup="menu" aria-expanded={menuOpen} aria-controls={menuOpen ? menuId : undefined} onClick={() => setMenuOpen((open) => !open)} onKeyDown={(event) => {
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
              event.preventDefault();
              setMenuOpen(true);
            }
          }}>
            <SlidersHorizontal className="size-4" aria-hidden />{t("appDetail.logsDisplay")}
          </Button>
          {menuOpen ? (
            <div ref={menuRef} id={menuId} role="menu" aria-label={t("appDetail.logsDisplay")} className="absolute right-0 top-full z-20 mt-1 min-w-48 rounded-md border bg-background p-1 shadow-md" onKeyDown={(event) => {
              const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>("button"));
              const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
              let next: number;
              if (event.key === "ArrowDown") next = (index + 1) % buttons.length;
              else if (event.key === "ArrowUp") next = (index - 1 + buttons.length) % buttons.length;
              else if (event.key === "Home") next = 0;
              else if (event.key === "End") next = buttons.length - 1;
              else return;
              event.preventDefault();
              buttons[next]?.focus();
            }}>
              {options.map((option) => (
                <button key={option.label} type="button" role="menuitemcheckbox" aria-checked={option.checked} className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm outline-none hover:bg-accent focus:bg-accent" onClick={option.toggle}>
                  <span className="size-4">{option.checked ? <Check className="size-4" aria-hidden /> : null}</span>{option.label}
                </button>
              ))}
            </div>
          ) : null}
        </div>
      </div>
      {unavailable ? <p className="text-sm text-muted-foreground">{t("appDetail.logsUnavailable")}</p> : <div ref={scrollRef} role="region" aria-label={t("appDetail.logsTitle")} tabIndex={0} className={cn("relative min-w-0 overflow-auto [overflow-anchor:none] overscroll-contain rounded-md bg-muted p-2 font-mono text-xs", expanded ? "min-h-64 flex-1" : "min-h-0 max-h-64")} onScroll={(event) => {
        const node = event.currentTarget;
        if (node.scrollTop === previousTop.current) return;
        previousTop.current = node.scrollTop;
        rememberAnchor();
        setFollow(node.scrollHeight - node.clientHeight - node.scrollTop <= 4);
      }}>
        <div ref={contentRef}>
          {entries.length === 0 ? <p className="text-muted-foreground">{t("appDetail.logsEmptyState")}</p> : entries.map((entry, index) => (
            <LogRow key={droppedCount + index} rowKey={droppedCount + index} entry={entry} wrap={wrap} pretty={pretty} locale={i18n.language} />
          ))}
        </div>
      </div>}
    </div>
  );
}

const LogRow = memo(function LogRow({ entry, rowKey, wrap, pretty, locale }: {
  entry: JournalEntryInfo;
  rowKey: number;
  wrap: boolean;
  pretty: boolean;
  locale: string;
}) {
  return (
    <div data-log-key={rowKey} className="flex gap-3">
      <span className="shrink-0 text-muted-foreground">{entry.timestamp !== null ? formatLocalTimestamp(entry.timestamp, { withYear: false, locale }) : "--"}</span>
      <span className={wrap ? "min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere]" : "shrink-0 whitespace-pre"}>{displayedMessage(entry, pretty)}</span>
    </div>
  );
});
