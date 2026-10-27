import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { ChevronDown, LogOut, Power, RotateCw, User } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useLogoutApiV1AuthLogoutPost } from "@/api/generated/auth/auth";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

/** `compact` is the icon-only trigger for the collapsed sidebar rail; otherwise a full-width account row. */
export function AccountMenu({ compact = false }: { compact?: boolean } = {}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const logout = useLogoutApiV1AuthLogoutPost();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        {compact ? (
          <Button type="button" variant="ghost" size="icon" aria-label={t("header.accountMenu")}>
            <AccountAvatar />
          </Button>
        ) : (
          <button
            type="button"
            aria-label={t("header.accountMenu")}
            className="flex min-w-0 flex-1 items-center gap-2.5 rounded-lg p-1 text-left outline-none hover:bg-accent/60 focus-visible:ring-2 focus-visible:ring-ring"
          >
            <AccountAvatar />
            <span className="flex min-w-0 flex-col">
              <span className="truncate text-[13px] font-semibold">{t("account.name")}</span>
              <span className="truncate text-xs text-muted-foreground">{t("account.kind")}</span>
            </span>
          </button>
        )}
      </DropdownMenuTrigger>
      <DropdownMenuContent align={compact ? "start" : "end"} side={compact ? "right" : "top"}>
        <DropdownMenuItem
          disabled={logout.isPending}
          onSelect={() =>
            logout.mutate(undefined, {
              onSuccess: () => {
                // No screen after this point may keep rendering pre-logout auth state from cache.
                queryClient.clear();
                void navigate({ to: "/login" });
              },
            })
          }
        >
          <LogOut />
          {t("common.logout")}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function AccountAvatar() {
  return (
    <span className="flex size-[30px] shrink-0 items-center justify-center rounded-full bg-accent" aria-hidden>
      <User className="size-[15px] text-foreground/80" />
    </span>
  );
}

/** Device name and connection in the header; Restart and Shut down hand off to Settings → Device, which owns the confirm and reboot-wait flow. */
export function DeviceMenu({ hostname, network, connected }: { hostname: string; network: string; connected: boolean }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const goToDevice = (action: "restart" | "shutdown") => void navigate({ to: "/settings/device", search: { action } });

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="gap-1.5 border-border px-2.5 font-medium"
          aria-label={`${t("header.deviceMenu")} ${hostname}`.trim()}
        >
          <span className={cn("size-[7px] rounded-full", connected ? "bg-success" : "bg-muted-foreground")} aria-hidden />
          <span className="hidden max-w-40 truncate sm:inline">{hostname}</span>
          <ChevronDown className="size-3.5 text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent>
        <DropdownMenuLabel>
          <span className="font-medium">{hostname}</span>
          {network ? <span className="text-xs font-normal text-muted-foreground">{network}</span> : null}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => goToDevice("restart")}>
          <RotateCw />
          {t("header.restart")}
        </DropdownMenuItem>
        <DropdownMenuItem variant="destructive" onSelect={() => goToDevice("shutdown")}>
          <Power />
          {t("header.shutdown")}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
