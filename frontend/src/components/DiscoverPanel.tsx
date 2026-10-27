import { ExternalLink, LayoutTemplate } from "lucide-react";
import { useTranslation } from "react-i18next";

import { AddAppPanel } from "@/components/AddAppPanel";
import { Button } from "@/components/ui/button";
import { TEMPLATE_REPO_URL } from "@/lib/navigation";

/** The Apps page's Discover tab: official catalog apps plus a link to start from the app template. */
export function DiscoverPanel({ onInstalled }: { onInstalled?: (appId: string | null) => void }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-3 rounded-xl border border-dashed border-border p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-3">
          <LayoutTemplate className="mt-0.5 size-5 shrink-0 text-muted-foreground" aria-hidden />
          <div className="flex flex-col gap-1">
            <p className="font-semibold">{t("apps.templateTitle")}</p>
            <p className="text-sm text-muted-foreground">{t("apps.templateBody")}</p>
          </div>
        </div>
        <Button asChild variant="outline" className="w-fit shrink-0">
          <a href={TEMPLATE_REPO_URL} target="_blank" rel="noreferrer">
            {t("apps.templateButton")}
            <ExternalLink className="size-4" aria-hidden />
          </a>
        </Button>
      </div>
      <AddAppPanel tabs={["catalog"]} onInstalled={onInstalled} />
    </div>
  );
}
