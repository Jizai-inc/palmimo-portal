import { Link } from "@tanstack/react-router";
import type { TFunction } from "i18next";
import { AlertTriangle, ChevronRight, Download } from "lucide-react";
import type * as React from "react";
import { useTranslation } from "react-i18next";

import type { AttentionItem } from "@/lib/homeAttention";

function AttentionLink({ item, children }: { item: AttentionItem; children: React.ReactNode }) {
  const className = "flex items-center gap-3 rounded-lg p-2 hover:bg-secondary";
  switch (item.kind) {
    case "appBroken":
    case "appUpdate":
    case "missingKeys":
      return (
        <Link to="/apps/$id" params={{ id: item.appId }} className={className}>
          {children}
        </Link>
      );
    case "credentialRejected":
      return (
        <Link to="/settings/developer" className={className}>
          {children}
        </Link>
      );
    case "chooseDefault":
      return (
        <Link to="/apps" search={{}} className={className}>
          {children}
        </Link>
      );
    case "portalUpdate":
      return (
        <Link to="/settings/update" className={className}>
          {children}
        </Link>
      );
  }
}

function attentionText(t: TFunction, item: AttentionItem): string {
  switch (item.kind) {
    case "appBroken":
      return t("home.attentionAppBroken", { name: item.appName });
    case "credentialRejected":
      return t("home.attentionCredentialRejected", { name: item.appName });
    case "appUpdate":
      return t("home.attentionAppUpdate", { name: item.appName });
    case "missingKeys":
      return t("home.attentionMissingKeys", { name: item.appName });
    case "chooseDefault":
      return t("home.attentionChooseDefault");
    case "portalUpdate":
      return t("home.attentionPortalUpdate");
  }
}

export function NeedsAttentionCard({ items }: { items: readonly AttentionItem[] }) {
  const { t } = useTranslation();
  return (
    <section aria-labelledby="home-attention" className="flex flex-col gap-1 rounded-xl border border-border bg-card p-3">
      <h2 id="home-attention" className="px-2 py-1 text-[15px] font-semibold">
        {t("home.attentionTitle")}
      </h2>
      <ul className="flex flex-col">
        {items.map((item) => {
          const update = item.kind === "appUpdate" || item.kind === "portalUpdate";
          const Icon = update ? Download : AlertTriangle;
          return (
            <li key={"appId" in item ? `${item.kind}:${item.appId}` : item.kind}>
              <AttentionLink item={item}>
                <Icon className={update ? "size-4 shrink-0 text-muted-foreground" : "size-4 shrink-0 text-destructive"} aria-hidden />
                <span className="min-w-0 flex-1 text-sm">{attentionText(t, item)}</span>
                <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden />
              </AttentionLink>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
