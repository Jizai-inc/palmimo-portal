import { Link } from "@tanstack/react-router";
import type { TFunction } from "i18next";
import { CheckCircle2, Circle, ExternalLink } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import type { ChecklistItem, ChecklistStep } from "@/lib/setupChecklist";
import { GETTING_STARTED_URL, TEMPLATE_REPO_URL } from "@/lib/navigation";
import { cn } from "@/lib/utils";

/** Via explicit `t(...)` calls so the i18n-parity scan sees every key (see `appStatusLabel`). */
function stepText(t: TFunction, step: ChecklistStep): { title: string; body: string } {
  switch (step) {
    case "connectGithub":
      return { title: t("home.checklistConnectGithub"), body: t("home.checklistConnectGithubBody") };
    case "tryOfficialApp":
      return { title: t("home.checklistTryOfficialApp"), body: t("home.checklistTryOfficialAppBody") };
    case "buildOwnApp":
      return { title: t("home.checklistBuildOwnApp"), body: t("home.checklistBuildOwnAppBody") };
  }
}

function StepAction({ step }: { step: ChecklistStep }) {
  const { t } = useTranslation();
  switch (step) {
    case "connectGithub":
      return (
        <Button asChild size="sm" variant="outline" className="w-fit">
          <Link to="/settings/developer">{t("home.checklistConnectGithubAction")}</Link>
        </Button>
      );
    case "tryOfficialApp":
      return (
        <Button asChild size="sm" variant="outline" className="w-fit">
          <Link to="/apps" search={{ tab: "discover" }}>
            {t("home.checklistTryOfficialAppAction")}
          </Link>
        </Button>
      );
    case "buildOwnApp":
      return (
        <div className="flex flex-wrap gap-2 self-start">
          <Button asChild size="sm" variant="outline">
            <a href={TEMPLATE_REPO_URL} target="_blank" rel="noreferrer">
              {t("apps.templateButton")}
              <ExternalLink className="size-3.5" aria-hidden />
            </a>
          </Button>
          <Button asChild size="sm" variant="ghost">
            <Link to="/apps" search={{ add: "github" }}>
              {t("apps.addFromUrlButton")}
            </Link>
          </Button>
        </div>
      );
  }
}

export function SetupChecklistCard({
  items,
  onHide,
  hiding,
}: {
  items: readonly ChecklistItem[];
  onHide: () => void;
  hiding: boolean;
}) {
  const { t } = useTranslation();
  const doneCount = items.filter((item) => item.done).length;
  return (
    <section aria-labelledby="home-checklist" className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="flex flex-col gap-0.5">
          <h2 id="home-checklist" className="font-semibold">
            {t("home.checklistTitle")}
          </h2>
          <p className="text-xs text-muted-foreground">{t("home.checklistProgress", { done: doneCount, total: items.length })}</p>
        </div>
        <Button type="button" size="sm" variant="ghost" onClick={onHide} disabled={hiding}>
          {t("home.checklistHide")}
        </Button>
      </div>
      <ol className="flex flex-col gap-3">
        {items.map((item) => {
          const { title, body } = stepText(t, item.step);
          return (
            <li key={item.step} className="flex gap-3">
              {item.done ? (
                <CheckCircle2 className="mt-0.5 size-5 shrink-0 text-green-600" aria-hidden />
              ) : (
                <Circle className="mt-0.5 size-5 shrink-0 text-muted-foreground" aria-hidden />
              )}
              <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                <p className={cn("text-sm font-medium", item.done && "text-muted-foreground line-through")}>
                  {title}
                  <span className="sr-only">{item.done ? ` (${t("home.checklistDone")})` : ""}</span>
                </p>
                {item.done ? null : (
                  <>
                    <p className="text-sm text-muted-foreground">{body}</p>
                    <StepAction step={item.step} />
                  </>
                )}
              </div>
            </li>
          );
        })}
      </ol>
      <a
        href={GETTING_STARTED_URL}
        target="_blank"
        rel="noreferrer"
        className="flex w-fit items-center gap-1 text-sm font-medium underline-offset-4 hover:underline"
      >
        {t("home.checklistGuideLink")}
        <ExternalLink className="size-3.5" aria-hidden />
      </a>
    </section>
  );
}
