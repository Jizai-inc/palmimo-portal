import { ArrowUpRight, Store } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useGetPublicCatalogApiV1CatalogPublicGet } from "@/api/generated/catalog/catalog";
import { AddAppPanel } from "@/components/AddAppPanel";

/** The Apps page's Discover tab: the public app catalog. */
export function DiscoverPanel({ onInstalled }: { onInstalled?: (appId: string | null) => void }) {
  const { t } = useTranslation();
  const { data, error } = useGetPublicCatalogApiV1CatalogPublicGet();
  const siteUrl = !error ? data?.site_url : null;

  return (
    <div className="space-y-4">
      {siteUrl ? (
        <a
          href={siteUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-3 rounded-xl border border-border bg-card p-4 text-card-foreground transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Store aria-hidden="true" className="size-6 shrink-0 text-muted-foreground" />
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold">{t("appAdd.findMoreApps")}</div>
            <p className="mt-1 text-xs text-muted-foreground">{t("appAdd.findMoreAppsDescription")}</p>
          </div>
          <ArrowUpRight aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
        </a>
      ) : null}
      <AddAppPanel tabs={["catalog"]} onInstalled={onInstalled} />
    </div>
  );
}
