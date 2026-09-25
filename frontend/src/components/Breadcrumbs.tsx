import type * as React from "react";

export function Breadcrumbs({ items, label }: { items: React.ReactNode[]; label: string }) {
  return (
    <nav aria-label={label}>
      <ol className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
        {items.map((item, index) => (
          <li key={index} className="flex items-center gap-2">
            {index > 0 ? <span aria-hidden>/</span> : null}
            {item}
          </li>
        ))}
      </ol>
    </nav>
  );
}
