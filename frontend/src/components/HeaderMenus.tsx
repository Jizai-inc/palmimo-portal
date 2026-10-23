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

export function AccountMenu() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const logout = useLogoutApiV1AuthLogoutPost();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="ghost" size="icon" aria-label={t("header.accountMenu")}>
          <User className="size-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent>
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

/** Device name and connection in the header; Restart and Shut down hand off to Settings → Device, which owns the confirm and reboot-wait flow. */
export function DeviceMenu({ hostname, network, connected }: { hostname: string; network: string; connected: boolean }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const goToDevice = (action: "restart" | "shutdown") => void navigate({ to: "/settings/device", search: { action } });

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="ghost" className="gap-2 px-2" aria-label={`${t("header.deviceMenu")} ${hostname}`.trim()}>
          <span className={cn("size-2.5 rounded-full", connected ? "bg-green-500" : "bg-muted-foreground")} aria-hidden />
          <span className="hidden max-w-40 truncate text-sm font-medium sm:inline">{hostname}</span>
          <ChevronDown className="size-4 text-muted-foreground" />
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
