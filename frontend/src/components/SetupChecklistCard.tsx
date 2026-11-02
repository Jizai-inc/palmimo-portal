import { Link } from "@tanstack/react-router";
import type { TFunction } from "i18next";
import { CheckCircle2, ChevronDown, Circle, ExternalLink } from "lucide-react";
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import type { ChecklistItem, ChecklistStep } from "@/lib/setupChecklist";
import { GETTING_STARTED_URL, TEMPLATE_REPO_URL } from "@/lib/navigation";
import { cn } from "@/lib/utils";

const EXPANDED_KEY = "portal.checklistExpanded";

function readExpanded(): boolean {
  try {
    return localStorage.getItem(EXPANDED_KEY) !== "false";
  } catch {
    return true;
  }
}

function writeExpanded(expanded: boolean) {
  try {
    localStorage.setItem(EXPANDED_KEY, String(expanded));
  } catch {
    // Storage blocked: the choice just doesn't outlive this page.
  }
}

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

export function SetupChecklistCard({ items }: { items: readonly ChecklistItem[] }) {
  const { t } = useTranslation();
  const doneCount = items.filter((item) => item.done).length;
  const [expanded, setExpanded] = useState(readExpanded);
  const bodyId = useId();
  function toggle() {
    setExpanded(!expanded);
    writeExpanded(!expanded);
  }
  return (
    <section aria-labelledby="home-checklist" className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4">
      <div className="flex flex-col gap-0.5">
        <h2 id="home-checklist" className="font-semibold">
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls={bodyId}
            onClick={toggle}
            className="-mx-1 flex w-[calc(100%+0.5rem)] items-center justify-between gap-2 rounded-md px-1 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {t("home.checklistTitle")}
            <ChevronDown aria-hidden className={cn("size-4 shrink-0 text-muted-foreground transition-transform duration-200", expanded && "rotate-180")} />
          </button>
        </h2>
        <p className="text-xs text-muted-foreground">{t("home.checklistProgress", { done: doneCount, total: items.length })}</p>
      </div>
      <div id={bodyId} hidden={!expanded} className="flex flex-col gap-3">
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
      </div>
    </section>
  );
}
