import { useNavigate } from "@tanstack/react-router";
import { CircleArrowDown, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { buttonVariants } from "@/components/ui/button";
import { usePlatformUpdate } from "@/lib/usePlatformUpdate";
import { cn } from "@/lib/utils";

/** The one place the shell warns about an outdated device platform; renders nothing while the platform is ready or unknown. */
export function PlatformUpdatePill() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { platform, job, startUpdate, starting, startError } = usePlatformUpdate();

  if (!platform || platform.ready) return null;

  const updating = starting || job?.state === "running";

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        className="flex min-w-0 items-center gap-1.5 rounded-full bg-warning-pill px-2.5 py-1 font-semibold text-warning-foreground outline-none hover:brightness-95 focus-visible:ring-2 focus-visible:ring-ring"
        aria-label={t("platformPill.label")}
      >
        {updating ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : <CircleArrowDown className="size-3.5" aria-hidden />}
        <span className="hidden truncate md:inline">{updating ? t("platformPill.updating") : t("platformPill.label")}</span>
      </DropdownMenuTrigger>
      <DropdownMenuContent className="flex w-[340px] max-w-[calc(100vw-2rem)] flex-col gap-2.5 rounded-xl p-4 shadow-lg">
        <p className="text-sm font-semibold">{t("platformPill.title")}</p>
        <p className="text-[13px] leading-normal text-muted-foreground">
          {t("platformPill.body", { version: platform.installed_version ?? "—" })}
        </p>
        <p className="text-xs text-muted-foreground">{t("platformPill.hint")}</p>
        <ApiErrorAlert error={startError} />
        <div className="mt-1 flex gap-2">
          <DropdownMenuItem
            disabled={updating}
            onSelect={(event) => {
              event.preventDefault();
              startUpdate();
            }}
            className={cn(buttonVariants({ size: "sm" }), "focus:bg-primary focus:opacity-90")}
          >
            {updating ? t("platformPill.updating") : t("apps.platformUpdateButton")}
          </DropdownMenuItem>
          <DropdownMenuItem
            onSelect={() => void navigate({ to: "/settings/update" })}
            className={cn(buttonVariants({ variant: "outline", size: "sm" }))}
          >
            {t("platformPill.details")}
          </DropdownMenuItem>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
