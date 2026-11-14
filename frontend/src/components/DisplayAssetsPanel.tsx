import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { PortalApiError } from "@/api/client";
import {
  getListStandardAssetsApiV1DisplayAssetsStandardGetQueryKey,
  useDeleteStandardAssetApiV1DisplayAssetsStandardNameDelete,
  useListStandardAssetsApiV1DisplayAssetsStandardGet,
  usePreviewStandardAssetApiV1DisplayAssetsStandardNamePreviewPost,
  usePutStandardAssetApiV1DisplayAssetsStandardNamePut,
} from "@/api/generated/display-assets/display-assets";
import type { DisplayAssetPreviewResponse, DisplaySyncResult, StandardAssetInfo } from "@/api/generated/models";
import { getGetStatusApiV1SystemStatusGetQueryKey } from "@/api/generated/system/system";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { PageHeader, SectionHeader } from "@/components/PageHeader";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { appText } from "@/lib/appText";
import { displayAssetName } from "@/lib/displayAssetName";

type Confirmation =
  | { mode: "replace"; asset: StandardAssetInfo; preview: DisplayAssetPreviewResponse }
  | { mode: "reset"; name: string; orphan: boolean }
  | null;

export function DisplayAssetsPanel() {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const list = useListStandardAssetsApiV1DisplayAssetsStandardGet();
  const [confirmation, setConfirmation] = useState<Confirmation>(null);
  const [outcome, setOutcome] = useState<DisplaySyncResult | null>(null);
  const [fileError, setFileError] = useState(false);
  const preview = usePreviewStandardAssetApiV1DisplayAssetsStandardNamePreviewPost();
  const saved = (response: { sync: DisplaySyncResult }) => {
    setConfirmation(null);
    setOutcome(response.sync);
    void queryClient.invalidateQueries({ queryKey: getListStandardAssetsApiV1DisplayAssetsStandardGetQueryKey() });
    void queryClient.invalidateQueries({ queryKey: getGetStatusApiV1SystemStatusGetQueryKey() });
  };
  const save = usePutStandardAssetApiV1DisplayAssetsStandardNamePut({ mutation: {
    onSuccess: saved,
    onError: (error) => {
      if (error instanceof PortalApiError && error.code === "display_asset_preview_expired") setConfirmation(null);
    },
  } });
  const reset = useDeleteStandardAssetApiV1DisplayAssetsStandardNameDelete({ mutation: { onSuccess: saved } });
  const busy = preview.isPending || save.isPending || reset.isPending;
  const clearFeedback = () => {
    preview.reset();
    save.reset();
    reset.reset();
    setFileError(false);
    setOutcome(null);
  };
  const choose = (asset: StandardAssetInfo, file: File) => {
    clearFeedback();
    if (file.size > 16 * 1024 * 1024) {
      setFileError(true);
      return;
    }
    preview.mutate({ name: asset.name, data: { file } }, {
      onSuccess: (result) => setConfirmation({ mode: "replace", asset, preview: result }),
    });
  };
  const openReset = (name: string, orphan = false) => {
    clearFeedback();
    setConfirmation({ mode: "reset", name, orphan });
  };
  const size = (bytes: number) => t("settings.display.size", {
    kb: new Intl.NumberFormat(i18n.language, { maximumFractionDigits: 2 }).format(bytes / 1024),
  });
  const kind = (value: string) => value === "vector" ? t("settings.display.vector")
    : value === "gif" ? t("settings.display.gif") : t("settings.display.image");
  const name = confirmation?.mode === "replace" ? confirmation.asset.name : confirmation?.name ?? "";
  const error = preview.error ?? save.error ?? reset.error;
  const diagnostics = preview.error instanceof PortalApiError && Array.isArray(preview.error.params.errors)
    ? preview.error.params.errors.filter((entry): entry is { message: string; path?: string } =>
      typeof entry === "object" && entry !== null && typeof entry.message === "string") : [];
  const syncMessage = outcome?.result === "busy" ? t("settings.display.syncBusy")
    : outcome?.result === "skipped" ? t("settings.display.syncSkipped")
    : outcome?.result === "failed" ? t("settings.display.syncFailed") : t("settings.display.syncDone");

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title={t("settings.display.title")} description={t("settings.display.description")} />
      {outcome && <Alert role={outcome.result === "failed" ? "alert" : "status"} variant={outcome.result === "failed" ? "destructive" : "default"}>
        <AlertDescription>{syncMessage}</AlertDescription>
        {outcome.reason && <p className="break-words text-sm">{outcome.reason}</p>}
      </Alert>}
      {!confirmation && <ApiErrorAlert error={error} />}
      {diagnostics.length > 0 && <ul className="list-inside list-disc break-words text-sm">
        {diagnostics.map((entry, index) => <li key={index}>{entry.path ? `${entry.path}: ` : ""}{entry.message}</li>)}
      </ul>}
      {fileError && <Alert variant="destructive"><AlertDescription>{t("settings.display.fileTooLarge")}</AlertDescription></Alert>}
      <ApiErrorAlert error={list.error} />
      {list.error ? null : list.isLoading ? <p className="text-sm text-muted-foreground">{t("common.loading")}</p> : <>
        <section className="flex flex-col gap-3">
          <SectionHeader title={t("settings.display.standard")} description={t("settings.display.fileHint")} />
          {list.data?.standard.length === 0 ? <p className="text-sm text-muted-foreground">{t("settings.display.empty")}</p> :
            <ul className="overflow-hidden rounded-xl border border-border text-[13px] divide-y divide-border">
              {list.data?.standard.map((asset) => <li key={asset.name} className="flex flex-col gap-3 px-5 py-4 md:flex-row md:items-center md:justify-between">
                <div className="flex min-w-0 flex-col gap-1 md:max-w-[55%]">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="font-semibold">{displayAssetName(t, asset.name)}</h3>
                    {asset.overridden && <Badge variant="outline">{t("settings.display.overridden")}</Badge>}
                  </div>
                  <p className="break-words text-muted-foreground">{appText(asset.translations, i18n.language, "description", asset.description ?? "")}</p>
                  <p className="text-muted-foreground">{kind(asset.kind)} · {size(asset.bytes)}</p>
                </div>
                <div className="flex flex-wrap items-end gap-2 md:justify-end">
                  <div className="flex flex-col gap-1">
                    <Label htmlFor={`display-file-${asset.name}`} className="text-xs">{t("settings.display.chooseFile", { name: displayAssetName(t, asset.name) })}</Label>
                    <Input id={`display-file-${asset.name}`} type="file" className="w-full max-w-64 text-xs" disabled={busy} onChange={(event) => {
                      const file = event.currentTarget.files?.[0];
                      event.currentTarget.value = "";
                      if (file) choose(asset, file);
                    }} />
                  </div>
                  {asset.overridden && <Button variant="outline" size="sm" disabled={busy} onClick={() => openReset(asset.name)}>{t("settings.display.reset")}</Button>}
                </div>
              </li>)}
            </ul>}
          {preview.isPending && <p role="status" className="text-sm text-muted-foreground">{t("settings.display.previewing")}</p>}
        </section>
        {(list.data?.orphan_overrides.length ?? 0) > 0 && <section className="flex flex-col gap-3">
          <SectionHeader title={t("settings.display.orphans")} description={t("settings.display.orphanDescription")} />
          <ul className="divide-y divide-border rounded-xl border border-border text-[13px]">
            {list.data?.orphan_overrides.map((orphan) => <li key={orphan} className="flex items-center justify-between gap-3 px-5 py-4">
              <span className="min-w-0 break-all font-mono">{orphan}</span>
              <Button variant="dangerGhost" size="sm" disabled={busy} onClick={() => openReset(orphan, true)}>{t("settings.display.deleteOrphan")}</Button>
            </li>)}
          </ul>
        </section>}
      </>}
      <AlertDialog open={confirmation !== null} onOpenChange={(open) => { if (!open && !busy) setConfirmation(null); }}>
        <AlertDialogContent className="max-h-[85dvh] w-[calc(100%_-_2rem)] max-w-lg overflow-y-auto">
          <AlertDialogHeader>
            <AlertDialogTitle>{confirmation?.mode === "replace" ? t("settings.display.confirmTitle", { name: displayAssetName(t, name) }) : confirmation?.orphan ? t("settings.display.deleteTitle", { name }) : t("settings.display.resetTitle", { name: displayAssetName(t, name) })}</AlertDialogTitle>
            <AlertDialogDescription>{confirmation?.mode === "replace" ? t("settings.display.confirmDescription") : confirmation?.orphan ? t("settings.display.deleteDescription") : t("settings.display.resetDescription")}</AlertDialogDescription>
          </AlertDialogHeader>
          {confirmation?.mode === "replace" && <div className="flex flex-col gap-4 text-sm">
            <dl className="grid grid-cols-2 gap-2">
              <dt className="text-muted-foreground">{t("settings.display.converted")}</dt>
              <dd>{kind(confirmation.preview.kind)} · {size(confirmation.preview.bytes)}</dd>
              <dt className="text-muted-foreground">{t("settings.display.sizeChange")}</dt>
              <dd aria-label={t("settings.display.sizeChange")}>{confirmation.preview.bytes >= confirmation.asset.bytes ? "+" : "−"}{size(Math.abs(confirmation.preview.bytes - confirmation.asset.bytes))}</dd>
            </dl>
            {(confirmation.preview.kind !== "vector" || confirmation.preview.params_lost.length > 0) && <div className="rounded-lg border border-warning-border bg-warning-soft p-3 text-warning-foreground">
              {confirmation.preview.kind !== "vector" && <p>{t("settings.display.rasterWarning")}</p>}
              {confirmation.preview.params_lost.length > 0 && <>
                <p>{t("settings.display.paramsLost")}</p>
                <ul className="mt-1 list-inside list-disc">{confirmation.preview.params_lost.map((sid) => <li key={sid}>{appText(confirmation.asset.translations, i18n.language, `params.${sid}`, sid)}</li>)}</ul>
              </>}
            </div>}
            {confirmation.preview.warnings.length > 0 && <div>
              <h3 className="font-medium">{t("settings.display.warnings")}</h3>
              <ul className="mt-1 list-inside list-disc break-words">{confirmation.preview.warnings.map((warning, index) => <li key={index}>{warning.message}</li>)}</ul>
            </div>}
            {!confirmation.preview.capacity.ok && <Alert variant="destructive">
              <p>{t("settings.display.capacityExceeded", { size: size(confirmation.preview.capacity.short_by) })}</p>
              {confirmation.preview.capacity.apps_no_longer_fit.length > 0 && <>
                <p>{t("settings.display.appsNoLongerFit")}</p>
                <ul className="list-inside list-disc break-all">{confirmation.preview.capacity.apps_no_longer_fit.map((id) => <li key={id}>{id}</li>)}</ul>
              </>}
            </Alert>}
          </div>}
          <ApiErrorAlert error={save.error ?? reset.error} />
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>{t("common.cancel")}</AlertDialogCancel>
            <Button disabled={busy || (confirmation?.mode === "replace" && !confirmation.preview.capacity.ok)} onClick={() => {
              if (confirmation?.mode === "replace") save.mutate({ name, data: { token: confirmation.preview.token } });
              else if (confirmation) reset.mutate({ name });
            }}>{save.isPending || reset.isPending ? t("settings.display.saving") : confirmation?.mode === "replace" ? t("settings.display.save") : t("settings.display.confirmReset")}</Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
