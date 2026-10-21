import { useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronUp, ListOrdered, Plus, Trash2 } from "lucide-react";
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
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";

type Pending = { kind: "delete" | "switch"; ssid: string } | null;

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
  const { data: saved, error: listError } = useListSavedApiV1WifiSavedGet();
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

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4">
      <div className="flex items-center gap-2">
        <ListOrdered className="size-4 text-muted-foreground" aria-hidden />
        <p className="font-semibold">{t("wifiSettings.savedTitle")}</p>
        <Button variant="outline" size="sm" className="ml-auto" onClick={onAdd}>
          <Plus className="size-4" aria-hidden />
          {t("wifiSettings.savedAdd")}
        </Button>
      </div>
      <p className="text-sm text-muted-foreground">{t("wifiSettings.savedBody", { hostname })}</p>
      <ApiErrorAlert error={listError ?? reorder.error ?? remove.error ?? activate.error} />
      {saved && items.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("wifiSettings.savedEmpty")}</p>
      ) : (
        <ol className="flex flex-col rounded-md border border-input">
          {items.map((item, index) => (
            <li
              key={item.ssid}
              className={`flex items-center gap-2 px-3 py-2 ${index === 0 ? "" : "border-t border-input"}`}
            >
              <span className="w-5 shrink-0 text-xs text-muted-foreground">{index + 1}</span>
              <span className="min-w-0 flex-1 truncate text-sm font-medium">{item.ssid}</span>
              {item.active ? (
                <span className="shrink-0 rounded-md bg-green-500/15 px-2 py-0.5 text-xs text-green-700 dark:text-green-400">
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
  );
}
