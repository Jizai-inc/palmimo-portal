import { useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronUp, Lock, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { SavedNetworkResponse } from "@/api/generated/models";
import { getGetStatusApiV1SystemStatusGetQueryKey } from "@/api/generated/system/system";
import {
  getGetStatusApiV1WifiStatusGetQueryKey,
  getListSavedApiV1WifiSavedGetQueryKey,
  useActivateSavedApiV1WifiSavedActivatePost,
  useDeleteSavedApiV1WifiSavedDeletePost,
  useListSavedApiV1WifiSavedGet,
  useReorderSavedApiV1WifiSavedOrderPut,
} from "@/api/generated/wifi/wifi";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { SectionHeader } from "@/components/PageHeader";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { usePlatformUpdate } from "@/lib/usePlatformUpdate";

type Pending = { kind: "delete" | "switch"; ssid: string } | null;

/** The first platform bundle whose polkit rules let the Portal edit NetworkManager's saved networks. */
const SAVED_NETWORKS_PLATFORM_VERSION = 3;

/**
 * The saved networks, in the order the device tries them. Reordering is optimistic: the list
 * moves at once and snaps back if the server refuses. `onSwitched` fires once a switch is
 * accepted -- the device may be about to leave this browser's network.
 */
export function SavedNetworksCard({
  hostname,
  onAdd,
  onSwitched,
}: {
  hostname: string;
  onAdd: () => void;
  onSwitched: (ssid: string) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const listKey = getListSavedApiV1WifiSavedGetQueryKey();
  const { platform, platformError, startUpdate, starting, startError, job } = usePlatformUpdate();
  // An unreadable platform status falls through to the list, which reports its own error.
  const platformKnown = platform !== undefined || platformError !== null;
  const needsPlatform = platform !== undefined && (platform.installed_version ?? 0) < SAVED_NETWORKS_PLATFORM_VERSION;
  const { data: saved, error: listError } = useListSavedApiV1WifiSavedGet({
    query: { enabled: platformKnown && !needsPlatform },
  });
  const [pending, setPending] = useState<Pending>(null);

  const setList = (list: SavedNetworkResponse[]) => queryClient.setQueryData(listKey, list);
  const reorder = useReorderSavedApiV1WifiSavedOrderPut({
    mutation: {
      onSuccess: setList,
      onError: () => void queryClient.invalidateQueries({ queryKey: listKey }),
    },
  });
  const remove = useDeleteSavedApiV1WifiSavedDeletePost({
    mutation: {
      onSuccess: (list) => {
        setPending(null);
        setList(list);
      },
      onError: () => setPending(null),
    },
  });
  const activate = useActivateSavedApiV1WifiSavedActivatePost({
    mutation: {
      onSuccess: (_, { data: { ssid } }) => {
        setPending(null);
        void queryClient.invalidateQueries({ queryKey: getGetStatusApiV1SystemStatusGetQueryKey() });
        void queryClient.invalidateQueries({ queryKey: getGetStatusApiV1WifiStatusGetQueryKey() });
        onSwitched(ssid);
      },
      onError: () => setPending(null),
    },
  });

  const items = saved ?? [];
  const current = items.find((item) => item.active)?.ssid ?? "";

  function move(index: number, offset: -1 | 1) {
    const target = index + offset;
    if (target < 0 || target >= items.length) return;
    const next = [...items];
    [next[index], next[target]] = [next[target], next[index]];
    setList(next);
    reorder.mutate({ data: { ssids: next.map((item) => item.ssid) } });
  }

  const busy = remove.isPending || activate.isPending;

  if (needsPlatform) {
    return (
      <section className="flex flex-col gap-2.5">
        <SectionHeader title={t("wifiSettings.savedTitle")} />
        {job?.state === "running" && job.step === "display_firmware" ? (
          <Alert><AlertDescription>{t("update.displayFirmwareWriting")}</AlertDescription></Alert>
        ) : null}

        <div className="flex flex-col gap-3 rounded-xl border border-border bg-card px-5 py-4 sm:flex-row sm:items-center sm:gap-3.5">
          <Lock className="hidden size-4 shrink-0 text-muted-foreground sm:block" aria-hidden />
          <div className="flex min-w-0 flex-1 flex-col gap-0.5 text-[13px]">
            <p className="font-medium">{t("wifiSettings.savedNeedsPlatformTitle")}</p>
            <p className="text-muted-foreground">{t("wifiSettings.savedNeedsPlatformBody")}</p>
          </div>
          <Button size="sm" variant="outline" className="w-fit" onClick={startUpdate} disabled={starting || job?.state === "running"}>
            {t("apps.platformUpdateButton")}
          </Button>
        </div>
        <ApiErrorAlert error={startError} />
      </section>
    );
  }

  return (
    <section className="flex flex-col gap-2.5">
      <SectionHeader
        title={t("wifiSettings.savedTitle")}
        actions={
          <Button variant="outline" size="sm" onClick={onAdd}>
            <Plus className="size-4" aria-hidden />
            {t("wifiSettings.savedAdd")}
          </Button>
        }
      />
    <div className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4">
      {job?.state === "running" && job.step === "display_firmware" ? (
          <Alert><AlertDescription>{t("update.displayFirmwareWriting")}</AlertDescription></Alert>
        ) : null}
      <p className="text-sm text-muted-foreground">{t("wifiSettings.savedBody", { hostname })}</p>
      <ApiErrorAlert error={listError ?? reorder.error ?? remove.error ?? activate.error} />
      {saved && items.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("wifiSettings.savedEmpty")}</p>
      ) : (
        <ol className="flex flex-col rounded-lg border border-border">
          {items.map((item, index) => (
            <li
              key={item.ssid}
              className={`flex items-center gap-2 px-3 py-2 ${index === 0 ? "" : "border-t border-border"}`}
            >
              <span className="w-5 shrink-0 text-xs text-muted-foreground">{index + 1}</span>
              <span className="min-w-0 flex-1 truncate text-sm font-medium">{item.ssid}</span>
              {item.active ? (
                <span className="shrink-0 rounded-md bg-success-soft px-2 py-0.5 text-xs font-medium text-success">
                  {t("wifiSettings.savedActive")}
                </span>
              ) : (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={busy}
                  onClick={() => setPending({ kind: "switch", ssid: item.ssid })}
                >
                  {t("wifiSettings.savedSwitch")}
                </Button>
              )}
              <Button
                variant="ghost"
                size="icon"
                aria-label={t("wifiSettings.savedMoveUp", { ssid: item.ssid })}
                disabled={index === 0 || reorder.isPending}
                onClick={() => move(index, -1)}
              >
                <ChevronUp className="size-4" aria-hidden />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                aria-label={t("wifiSettings.savedMoveDown", { ssid: item.ssid })}
                disabled={index === items.length - 1 || reorder.isPending}
                onClick={() => move(index, 1)}
              >
                <ChevronDown className="size-4" aria-hidden />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                aria-label={t("wifiSettings.savedDelete", { ssid: item.ssid })}
                className={item.active ? "invisible" : undefined}
                disabled={item.active || busy}
                onClick={() => setPending({ kind: "delete", ssid: item.ssid })}
              >
                <Trash2 className="size-4" aria-hidden />
              </Button>
            </li>
          ))}
        </ol>
      )}

      <AlertDialog open={pending !== null} onOpenChange={(open) => !open && setPending(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {pending?.kind === "switch"
                ? t("wifiSettings.savedSwitchDialogTitle", { ssid: pending.ssid })
                : t("wifiSettings.savedDeleteDialogTitle", { ssid: pending?.ssid ?? "" })}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {pending?.kind === "switch"
                ? t("wifiSettings.savedSwitchDialogBody", { ssid: pending.ssid, current, hostname })
                : t("wifiSettings.savedDeleteDialogBody", { ssid: pending?.ssid ?? "" })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>{t("common.cancel")}</AlertDialogCancel>
            <Button
              variant={pending?.kind === "delete" ? "destructive" : "default"}
              disabled={busy}
              onClick={() => {
                if (!pending) return;
                if (pending.kind === "switch") {
                  activate.mutate({ data: { ssid: pending.ssid } });
                } else {
                  remove.mutate({ data: { ssid: pending.ssid } });
                }
              }}
            >
              {pending?.kind === "switch" ? t("wifiSettings.savedSwitchConfirm") : t("wifiSettings.savedDeleteConfirm")}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
    </section>
  );
}
