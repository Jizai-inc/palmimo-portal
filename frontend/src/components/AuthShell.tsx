import type * as React from "react";

import { AppHeader } from "@/components/AppHeader";

/**
 * Shared chrome for the setup / login / change-password / wifi /
 * wifi.waiting / status-error screens: {@link AppHeader} on top, then a
 * body that centers a card. Mobile has no card border -- the page itself is
 * the "card" (full-width, `p-5`); desktop centers a bordered `max-w-[440px]`
 * card.
 */
export function AuthShell({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-h-screen flex-col bg-background">
      <AppHeader />
      <main className="flex flex-1 items-start justify-center p-5 md:items-center md:p-8">
        <div className="flex w-full flex-col gap-4 md:max-w-[440px] md:rounded-xl md:border md:border-border md:bg-card md:p-8">
          <div className="flex flex-col gap-1.5">
            <h1 className="text-2xl font-semibold md:text-[22px]">{title}</h1>
            {description ? <p className="text-sm text-muted-foreground">{description}</p> : null}
          </div>
          {children}
        </div>
      </main>
    </div>
  );
}
