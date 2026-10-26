import type * as React from "react";

import { cn } from "@/lib/utils";

/** The Software update page's card: a titled head, a facts list, live status, and an action footer. */
export function UpdateCardFrame({
  warning = false,
  eyebrow,
  title,
  description,
  badge,
  facts,
  status,
  footer,
}: {
  warning?: boolean;
  eyebrow: string;
  title: React.ReactNode;
  description?: React.ReactNode;
  badge?: React.ReactNode;
  facts: React.ReactNode;
  status?: React.ReactNode;
  footer?: React.ReactNode;
}) {
  return (
    <section className={cn("flex flex-col overflow-hidden rounded-xl border bg-card", warning ? "border-warning-border" : "border-border")}>
      <div className={cn("flex flex-col gap-1.5 p-5", warning && "bg-warning-soft")}>
        <div className="flex items-center justify-between gap-2">
          <p className={cn("text-xs font-semibold uppercase", warning ? "text-warning-foreground" : "text-muted-foreground")}>{eyebrow}</p>
          {badge}
        </div>
        <h2 className="text-lg font-semibold">{title}</h2>
        {description ? <p className={cn("text-[13px]", warning ? "text-warning-foreground" : "text-muted-foreground")}>{description}</p> : null}
      </div>
      <div className="flex flex-1 flex-col gap-3 border-t border-border px-5 py-4">
        <dl className="flex flex-col gap-2.5 text-[13px]">{facts}</dl>
        {status}
      </div>
      {footer ? <div className="flex flex-wrap items-center gap-2 border-t border-border px-5 py-4">{footer}</div> : null}
    </section>
  );
}

export function UpdateFact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <dt className="shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-right">{children}</dd>
    </div>
  );
}
