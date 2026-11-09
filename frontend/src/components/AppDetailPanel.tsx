import { AppOfficialBadge } from "@/components/AppOfficialBadge";
import { refreshAppQueries } from "@/lib/refreshAppQueries";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { PortalApiError } from "@/api/client";
import {
  getGetAppApiV1AppsNameGetQueryKey,
  getListAppsApiV1AppsGetQueryKey,
  useDeleteAppApiV1AppsNameDelete,
  useGetAppApiV1AppsNameGet,
  useListAppsApiV1AppsGet,
  usePutDefaultAppApiV1AppsDefaultPut,
  usePutBindingsApiV1AppsNameBindingsPut,
  usePutParamsApiV1AppsNameParamsPut,
  usePutSourceApiV1AppsNameSourcePut,
  useStartAppEndpointApiV1AppsNameStartPost,
  useStopAppEndpointApiV1AppsNameStopPost,
  useUpdateAppApiV1AppsNameUpdatePost,
  useUpdateCheckApiV1AppsNameUpdateCheckPost,
} from "@/api/generated/apps/apps";
import { useListSecretsApiV1SecretsGet } from "@/api/generated/secrets/secrets";
import { getGetStatusApiV1SystemStatusGetQueryKey } from "@/api/generated/system/system";
import type { PublicUpdateSourceInfo, AppDetailResponse, ParamSpecInfo, SourceUpdateRequestRefKind } from "@/api/generated/models";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { appIdNamePart } from "@/components/AppIdLabel";
import { AppJobDialog } from "@/components/AppJobDialog";
import { AppTitleEditor } from "@/components/AppTitle";
import { LogViewer } from "@/components/LogViewer";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { appStatusLabel, appStatusTone, isAppStatusBusy, shouldPollAppStatus } from "@/lib/appStatus";
import { appText } from "@/lib/appText";
import { deviceLabel } from "@/lib/deviceLabel";
import { formatUtcTimestamp } from "@/lib/formatTimestamp";
import { useAppLogs } from "@/lib/useAppLogs";

export function AppDetailPanel({ id, onDeleted = () => undefined }: { id: string; onDeleted?: () => void }) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const invalidate = () => void queryClient.invalidateQueries({ queryKey: getGetAppApiV1AppsNameGetQueryKey(id) });

  const [job, setJob] = useState<{ id: string; kind: "update" | "delete" } | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  // Set once a delete is confirmed and never cleared for this mount -- unlike `job` (cleared when
  // the dialog closes), this is what keeps the app-detail query disabled for the rest of a delete
  // even after "Close" (closing does not cancel the job).
  const [deleting, setDeleting] = useState(false);
  const [deleteOutcome, setDeleteOutcome] = useState<"done" | "failed" | null>(null);
  const [redirectedAfterDelete, setRedirectedAfterDelete] = useState(false);
  const [updateCompleted, setUpdateCompleted] = useState(0);

  const { data: app, error } = useGetAppApiV1AppsNameGet(id, {
    query: {
      // While the delete job's dialog is open, its own state (not this query) is the source of
      // truth for "done" -- disabling here avoids a race where this 404s (the app already
      // dropped from state) before the job's own poll has caught up to "done", which would
      // otherwise render a raw error while the dialog still claims to be in progress and strand
      // its "Close" button. Once the dialog is closed the job is no longer polled by anything, so
      // this re-enables: its eventual 404 (see `deletedAfterClose` below) is the only signal left
      // that the deletion, which "Close" does not cancel, has actually finished.
      enabled: !(deleting && job !== null),
      refetchInterval: (query) => {
        // Re-enabling alone does not refetch a query whose cached data is not otherwise due for
        // a refresh -- poll until the eventual 404 resolves the flow, since nothing else is
        // watching the job once its dialog is gone.
        if (deleting && job === null) return 3_000;
        return query.state.data && shouldPollAppStatus(query.state.data.status) ? 3_000 : false;
      },
    },
  });

  const deletedAfterClose = deleting && job === null && error instanceof PortalApiError && error.code === "app_not_found";

  useEffect(() => {
    if (deletedAfterClose && !redirectedAfterDelete) {
      setRedirectedAfterDelete(true);
      onDeleted();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deletedAfterClose, redirectedAfterDelete]);

  const invalidateWithStatus = () => {
    invalidate();
    void queryClient.invalidateQueries({ queryKey: getGetStatusApiV1SystemStatusGetQueryKey() });
  };
  const startApp = useStartAppEndpointApiV1AppsNameStartPost({ mutation: { onSuccess: invalidateWithStatus } });
  const stopApp = useStopAppEndpointApiV1AppsNameStopPost({ mutation: { onSuccess: invalidateWithStatus } });
  const updateApp = useUpdateAppApiV1AppsNameUpdatePost({
    mutation: { onSuccess: (data) => setJob({ id: data.job.id, kind: "update" }),
      onError: (failure) => { if (failure instanceof PortalApiError && failure.code === "public_app_changed") void refreshAppQueries(queryClient, id); } },
  });
  const { data: appsResponse, error: appsError } = useListAppsApiV1AppsGet();
  const appsData = appsError ? undefined : appsResponse;
  const deleteApp = useDeleteAppApiV1AppsNameDelete({
    mutation: {
      onSuccess: (data) => {
        setDeleteOpen(false);
        setDeleting(true);
        setDeleteOutcome(null);
        setJob({ id: data.job.id, kind: "delete" });
      },
    },
  });

  if (deletedAfterClose) {
    return null;
  }
  if (error) {
    return <ApiErrorAlert error={error} />;
  }
  if (!app) {
    return <p className="text-sm text-muted-foreground">{t("common.loading")}</p>;
  }

  const tone = appStatusTone(app.status);
  const busy = isAppStatusBusy(app.status);
  // This app's own status comes from the detail query, not the list: nothing on this page refetches
  // the list, so its entry for this app is stale right after a start here or an exit on its own.
  const appRunning = [app, ...(appsData?.apps ?? []).filter((candidate) => candidate.id !== app.id)].some((candidate) =>
    ["running", "starting", "stopping"].includes(candidate.status),
  );

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <AppTitleEditor app={app} />
          <Badge variant={tone === "green" ? "default" : tone === "red" ? "destructive" : tone === "muted" ? "secondary" : "outline"} className="flex items-center gap-1">
            {busy ? <Loader2 className="size-3 animate-spin" /> : null}
            {appStatusLabel(t, app.status, app.exit_code)}
          </Badge>
          <AppOfficialBadge official={app.source.official} publicAppId={app.source.public_app_id} />
          {appsData?.default_app === app.id ? <Badge variant="outline">{t("apps.defaultBadge")}</Badge> : null}
          <div className="ml-auto flex gap-2">
            {app.status === "running" && app.url ? (
              <Button variant="outline" asChild>
                <a href={app.url} target="_blank" rel="noreferrer">{t("apps.openButton")}</a>
              </Button>
            ) : null}
            {app.status === "running" ? (
              <Button variant="outline" onClick={() => stopApp.mutate({ name: id })} disabled={stopApp.isPending}>{t("apps.stopButton")}</Button>
            ) : app.status === "stopped" || app.status === "failed" ? (
              <Button onClick={() => startApp.mutate({ name: id })} disabled={startApp.isPending}>{t("apps.startButton")}</Button>
            ) : null}
          </div>
        </div>
        {app.description ? (
          <p className="text-sm text-muted-foreground">
            {appText(app.translations, i18n.language, "description", app.description)}
          </p>
        ) : null}
        {app.name !== appIdNamePart(app.id) ? <p className="text-sm text-muted-foreground [overflow-wrap:anywhere]">{app.name}</p> : null}
      </div>
      <ApiErrorAlert error={startApp.error} />
      <ApiErrorAlert error={stopApp.error} />

      {app.broken_reason ? (
        <Alert variant="destructive">
          <AlertTitle>{t("appDetail.brokenReasonTitle")}</AlertTitle>
          <AlertDescription>{app.broken_reason}</AlertDescription>
        </Alert>
      ) : null}

      <ApiErrorAlert error={updateApp.error} />

      <BindingsSection app={app} onSaved={invalidate} />
      <ParamsSection app={app} onSaved={invalidate} />
      <LogsSection id={id} status={app.status} />

      {app.devices.length > 0 ? (
        <Section title={t("appDetail.devicesTitle")}>
          <div className="flex flex-wrap gap-1">
            {app.devices.map((device) => (
              <Badge key={device} variant="outline">{deviceLabel(t, device)}</Badge>
            ))}
          </div>
        </Section>
      ) : null}

      <DefaultAppSection app={app} onSaved={invalidate} />
      <SourceSection app={app} onUpdate={(commit) => updateApp.mutate({ name: id, data: commit ? { commit } : undefined })} updatePending={updateApp.isPending} updateCompleted={updateCompleted} appRunning={appRunning} />

      <Section title={t("appDetail.dangerZoneTitle")}>
        <Button variant="destructive" className="w-fit" onClick={() => setDeleteOpen(true)}>
          {t("appDetail.deleteAppButton")}
        </Button>
      </Section>

      <AlertDialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("appDetail.deleteDialogTitle", { name: app.name })}</AlertDialogTitle>
            <AlertDialogDescription>{t("appDetail.deleteDialogBody")}</AlertDialogDescription>
          </AlertDialogHeader>
          <ApiErrorAlert error={deleteApp.error} />
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteApp.isPending}>{t("common.cancel")}</AlertDialogCancel>
            <Button variant="destructive" disabled={deleteApp.isPending} onClick={() => deleteApp.mutate({ name: id })}>
              {t("appDetail.deleteDialogConfirm")}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AppJobDialog
        jobId={job?.id ?? null}
        title={job?.kind === "delete" && deleteOutcome === null ? t("appDetail.deleteProgressTitle", { name: app.name }) : t("apps.jobDialogTitle", { name: app.name })}
        completedMessage={job?.kind === "delete" ? t("appDetail.deleteCompleted", { name: app.name }) : undefined}
        onClose={() => {
          setJob(null);
          if (deleteOutcome === "done") onDeleted();
        }}
        onDone={() => {
          const wasDelete = job?.kind === "delete";
          if (wasDelete) {
            setDeleteOutcome("done");
          } else {
            setJob(null);
            void refreshAppQueries(queryClient, id);
            setUpdateCompleted((value) => value + 1);
          }
        }}
        onFailed={() => {
          if (job?.kind === "delete") setDeleteOutcome("failed");
        }}
      />
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4">
      <p className="font-semibold">{title}</p>
      {children}
    </div>
  );
}

// Secret names are uppercase-only, so this cannot collide; Radix Select forbids "" as an item value.
const UNBOUND = "unbound";

function BindingsSection({ app, onSaved }: { app: AppDetailResponse; onSaved: () => void }) {
  const { t, i18n } = useTranslation();
  const { data: secretsData } = useListSecretsApiV1SecretsGet();
  const [bindings, setBindings] = useState<Record<string, string>>(app.bindings);
  const [justSaved, setJustSaved] = useState(false);
  useEffect(() => setBindings(app.bindings), [app.bindings]);

  const putBindings = usePutBindingsApiV1AppsNameBindingsPut({
    mutation: {
      onSuccess: () => {
        onSaved();
        setJustSaved(true);
        setTimeout(() => setJustSaved(false), 2000);
      },
    },
  });
  const secretNames = (secretsData?.secrets ?? []).map((secret) => secret.name);

  if (app.env.length === 0) return null;

  function handleSave() {
    // Only names, never values: the binding maps a required var to a *registered secret name*.
    const payload: Record<string, string> = {};
    for (const [key, value] of Object.entries(bindings)) {
      if (value) payload[key] = value;
    }
    putBindings.mutate({ name: app.id, data: { bindings: payload } });
  }

  return (
    <Section title={t("appDetail.bindingsTitle")}>
      <div className="flex flex-col gap-2">
        {app.env.map((envVar) => {
          const savedMissing = bindings[envVar.name] === app.bindings[envVar.name] && (app.missing_env ?? []).includes(envVar.name);
          const unbound = envVar.required && (!bindings[envVar.name] || savedMissing);
          return (
            <div key={envVar.name} className={`flex flex-wrap items-center gap-2 rounded-md p-2 ${unbound ? "bg-destructive/10" : ""}`}>
              <div className="flex min-w-0 flex-1 flex-col">
                <div className="flex items-center gap-2">
                  <code className="font-mono text-sm">{envVar.name}</code>
                  <Badge variant={envVar.required ? "destructive" : "outline"}>
                    {envVar.required ? t("appAdd.envRequiredBadge") : t("appAdd.envOptionalBadge")}
                  </Badge>
                </div>
                <p className="text-xs text-muted-foreground">
                  {appText(app.translations, i18n.language, `env.${envVar.name}`, envVar.description)}
                </p>
                {unbound ? <p className="text-xs text-destructive">{t("appDetail.bindingsUnboundRequiredWarning")}</p> : null}
              </div>
              <Select
                value={bindings[envVar.name] || UNBOUND}
                onValueChange={(next) => setBindings((current) => ({ ...current, [envVar.name]: next === UNBOUND ? "" : next }))}
              >
                <SelectTrigger aria-label={envVar.name} className="sm:w-72">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={UNBOUND}>{t("appDetail.bindingsNotBound")}</SelectItem>
                  {secretNames.map((secretName) => (
                    <SelectItem key={secretName} value={secretName}>{secretName}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          );
        })}
      </div>
      <ApiErrorAlert error={putBindings.error} />
      <div className="flex items-center gap-2">
        <Button className="w-fit" onClick={handleSave} disabled={putBindings.isPending}>{t("appDetail.bindingsSaveButton")}</Button>
        {justSaved ? <p className="text-sm text-muted-foreground">{t("appDetail.bindingsSaved")}</p> : null}
      </div>
    </Section>
  );
}

/**
 * Renders one input per declared `[params.*]` spec (design doc 1.2), typed on `spec.type`:
 * an enum renders a `<select>` of `spec.choices`, a bool a checkbox switch, int/float a bounded
 * number input, string a text input. Falls back to the current-value keys when a manifest could
 * not be read (`app.manifest` is `null`), so a broken app's stored params are still visible.
 */
function ParamsSection({ app, onSaved }: { app: AppDetailResponse; onSaved: () => void }) {
  const { t, i18n } = useTranslation();
  const [values, setValues] = useState<Record<string, unknown>>(app.params);
  const [justSaved, setJustSaved] = useState(false);
  useEffect(() => setValues(app.params), [app.params]);
  const putParams = usePutParamsApiV1AppsNameParamsPut({
    mutation: {
      onSuccess: () => {
        onSaved();
        setJustSaved(true);
        setTimeout(() => setJustSaved(false), 2000);
      },
    },
  });

  const specs: ParamSpecInfo[] = app.manifest?.params ?? Object.keys(app.params).map((name) => ({ name, type: "string" }));

  return (
    <Section title={t("appDetail.paramsTitle")}>
      {specs.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("appDetail.paramsEmptyState")}</p>
      ) : (
        <div className="flex flex-col gap-2">
          {specs.map((spec) => {
            const value = values[spec.name] ?? spec.default;
            const id = `param-${spec.name}`;
            return (
              <div key={spec.name} className="flex flex-col gap-1">
                <div className="flex items-center gap-2">
                  <Label htmlFor={id} className="w-40 shrink-0 font-mono text-xs">{spec.name}</Label>
                  {spec.type === "enum" ? (
                    <Select
                      value={typeof value === "string" ? value : ""}
                      onValueChange={(next) => setValues((current) => ({ ...current, [spec.name]: next }))}
                    >
                      <SelectTrigger id={id}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {(spec.choices ?? []).map((choice) => (
                          <SelectItem key={choice} value={choice}>
                            {choice}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  ) : spec.type === "bool" ? (
                    <input
                      id={id}
                      type="checkbox"
                      role="switch"
                      checked={Boolean(value)}
                      onChange={(event) => setValues((current) => ({ ...current, [spec.name]: event.target.checked }))}
                    />
                  ) : spec.type === "int" || spec.type === "float" ? (
                    <Input
                      id={id}
                      type="number"
                      min={spec.min ?? undefined}
                      max={spec.max ?? undefined}
                      value={typeof value === "number" ? value : ""}
                      onChange={(event) => setValues((current) => ({ ...current, [spec.name]: Number(event.target.value) }))}
                    />
                  ) : (
                    <Input
                      id={id}
                      value={String(value ?? "")}
                      onChange={(event) => setValues((current) => ({ ...current, [spec.name]: event.target.value }))}
                    />
                  )}
                </div>
                {spec.description ? (
                  <p className="pl-40 text-xs text-muted-foreground">
                    {appText(app.translations, i18n.language, `params.${spec.name}`, spec.description)}
                  </p>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
      <ApiErrorAlert error={putParams.error} />
      {specs.length > 0 ? (
        <div className="flex items-center gap-2">
          <Button
            className="w-fit"
            onClick={() => putParams.mutate({ name: app.id, data: { params: values } })}
            disabled={putParams.isPending}
          >
            {t("appDetail.paramsSaveButton")}
          </Button>
          {justSaved ? <p className="text-sm text-muted-foreground">{t("appDetail.paramsSaved")}</p> : null}
        </div>
      ) : null}
    </Section>
  );
}

function LogsSection({ id, status }: { id: string; status: string }) {
  const { t } = useTranslation();
  const logs = useAppLogs(id, status);
  return (
    <Section title={t("appDetail.logsTitle")}>
      {logs.unavailable ? <p className="text-sm text-muted-foreground">{t("appDetail.logsUnavailable")}</p> : (
        <>
          <p className="text-xs text-muted-foreground">{t("appDetail.logsReusedIdNote")}</p>
          <LogViewer
            droppedCount={logs.droppedCount}
            entries={logs.accumulated}
            text={logs.text}
            invocations={logs.invocations}
            invocation={logs.invocation}
            setInvocation={logs.setInvocation}
            toolbar={<>
              <Button type="button" variant="ghost" size="sm" asChild>
                <Link to="/apps/$id/logs" params={{ id }}>{t("appDetail.logsExpandButton")}</Link>
              </Button>
              {status !== "running" ? <Button type="button" variant="ghost" size="sm" onClick={logs.refetch}>{t("appDetail.logsLoadMoreButton")}</Button> : null}
            </>}
          />
        </>
      )}
    </Section>
  );
}

function DefaultAppSection({ app, onSaved }: { app: AppDetailResponse; onSaved: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data: appsResponse, error: appsError } = useListAppsApiV1AppsGet();
  const appsData = appsError ? undefined : appsResponse;
  const isDefault = appsData?.default_app === app.id;
  const putDefault = usePutDefaultAppApiV1AppsDefaultPut({
    mutation: {
      onSuccess: () => {
        onSaved();
        void queryClient.invalidateQueries({ queryKey: getListAppsApiV1AppsGetQueryKey() });
      },
    },
  });
  return (
    <Section title={t("appDetail.defaultAppTitle")}>
      <p className="text-sm text-muted-foreground">{isDefault ? t("appDetail.defaultAppIsDefault") : t("appDetail.defaultAppBody")}</p>
      <ApiErrorAlert error={putDefault.error} />
      <Button
        variant="outline"
        className="w-fit"
        disabled={putDefault.isPending || appsData === undefined || (!isDefault && app.broken_reason != null)}
        onClick={() => putDefault.mutate({ data: { id: isDefault ? null : app.id } })}
      >
        {isDefault ? t("appDetail.defaultAppRemoveButton") : t("appDetail.defaultAppMakeButton")}
      </Button>
    </Section>
  );
}

function SourceSection({
  app,
  onUpdate,
  updatePending,
  updateCompleted,
  appRunning,
}: {
  app: AppDetailResponse;
  onUpdate: (commit?: string) => void;
  updatePending: boolean;
  updateCompleted: number;
  appRunning: boolean;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [target, setTarget] = useState<PublicUpdateSourceInfo | null>(null);
  const [repairing, setRepairing] = useState(false);
  const [editingRef, setEditingRef] = useState(false);
  const [refKind, setRefKind] = useState<SourceUpdateRequestRefKind>("branch");
  const [ref, setRef] = useState("");
  const putSource = usePutSourceApiV1AppsNameSourcePut();
  const refresh = () => { void queryClient.invalidateQueries({ queryKey: getGetAppApiV1AppsNameGetQueryKey(app.id) }); void queryClient.invalidateQueries({ queryKey: getListAppsApiV1AppsGetQueryKey() }); };
  const updateCheck = useUpdateCheckApiV1AppsNameUpdateCheckPost({ mutation: { onSuccess: refresh } });
  const { reset: resetUpdateCheck } = updateCheck;

  useEffect(() => {
    resetUpdateCheck();
  }, [resetUpdateCheck, updateCompleted]);

  const isGit = app.source.type === "git";
  const isPublic = Boolean(app.source.public_app_id);
  const unavailable = app.public_app_available === false;
  const isOfficialTag = isPublic ? app.source.official !== false : app.source.official && app.source.ref_kind === "tag";
  const needsPublicRepair = isPublic && !unavailable && (app.status === "broken" || app.status === "needs_repair");
  const repairWouldUpdate = Boolean(app.latest_source?.commit && app.latest_source.commit !== app.source.commit);
  const repairChangesVersion = repairing && target !== null && target.commit !== app.source.commit;
  const addedDevices = target?.devices.filter((device) => !app.devices.includes(device)) ?? [];
  const currentRequired = new Set(app.env.filter((entry) => entry.required).map((entry) => entry.name));
  const addedRequiredEnv = target?.env.filter((entry) => entry.required && !currentRequired.has(entry.name)) ?? [];
  const updateAvailable = !unavailable && (isPublic ? app.update_available === true : updateCheck.data?.update_available === true);

  return (
    <Section title={t("appDetail.sourceTitle")}>
      {app.source.public_page_url ? <a href={app.source.public_page_url} target="_blank" rel="noreferrer" className="text-sm underline">{t("appAdd.publicPageLink")}</a> : null}
      {unavailable ? <p role="status" className="text-sm text-muted-foreground">{t("apps.publicAppUnavailable")}</p> : null}
      <dl className="flex flex-col gap-1 text-sm">
        <KvRow label={t("appDetail.sourceTypeLabel")} value={app.source.type} />
        {app.source.url ? <KvRow label={t("appDetail.sourceRepoLabel")} value={app.source.url} /> : null}
        {app.source.subdir ? <KvRow label={t("appDetail.sourceSubdirLabel")} value={app.source.subdir} /> : null}
        {app.source.manifest ? <KvRow label={t("appDetail.sourceManifestLabel")} value={app.source.manifest} /> : null}
        {app.source.ref ? <KvRow label={t("appDetail.sourceRefLabel")} value={app.source.ref} /> : null}
        {app.source.commit ? <KvRow label={t("appDetail.sourceCommitLabel")} value={app.source.commit} /> : null}
        {app.installed_at ? <KvRow label={t("appDetail.sourceInstalledAtLabel")} value={formatUtcTimestamp(app.installed_at)} /> : null}
      </dl>
      {isGit ? (
        <div className="flex flex-wrap items-center gap-2">
          {!isOfficialTag ? <Button variant="outline" size="sm" onClick={() => setEditingRef((value) => !value)}>{t("appDetail.sourceEditRefButton")}</Button> : null}
          {needsPublicRepair ? <Button disabled={updatePending || appRunning || !app.latest_source?.commit} onClick={() => { if (app.latest_source) { setRepairing(true); setTarget(app.latest_source); } }}>{repairWouldUpdate ? t("appDetail.updateAndRepair") : t("apps.repairButton")}</Button> : null}
          {unavailable || (needsPublicRepair && app.latest_source?.commit) ? null : updateAvailable ? (
            <Button size="sm" onClick={() => {
              if (!isPublic) onUpdate();
              else if (app.latest_source) { setRepairing(false); setTarget(app.latest_source); }
            }} disabled={updatePending || appRunning || (isPublic && !app.latest_source?.commit)} title={appRunning ? t("appDetail.updateDisabledAppRunning") : undefined}>
              {t("appDetail.updateButton")}
            </Button>
          ) : (
            <Button
              variant="outline"
              size="sm"
              onClick={() => updateCheck.mutate({ name: app.id })}
              disabled={updateCheck.isPending}
            >
              {t("appDetail.checkForUpdatesButton")}
            </Button>
          )}
          {unavailable ? null : updateAvailable ? (
            <p className="text-sm">{appRunning ? t("appDetail.updateDisabledAppRunning") : t("apps.updateAvailableChip")}</p>
          ) : !unavailable && updateCheck.data ? (
            <p className="text-sm text-muted-foreground">{t("appDetail.checkForUpdatesUpToDate")}</p>
          ) : null}
        </div>
      ) : null}
      {isPublic && app.source.official === null ? <p role="note" className="text-sm text-muted-foreground">{t("appDetail.sourceCatalogUnknown")}</p> : isOfficialTag ? <p className="text-sm text-muted-foreground">{t("appDetail.sourceOfficialTagManaged")}</p> : null}
      <ApiErrorAlert error={updateCheck.error} />
      {editingRef && !isOfficialTag ? (
        <div className="flex flex-col gap-2">
          <p className="text-sm font-medium">{t("appDetail.sourceEditRefTitle")}</p>
          {isPublic ? <p role="note" className="text-sm">{t("appDetail.publicRefOptOut")}</p> : null}
          <div className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="source-ref-kind">{t("appAdd.githubRefKindLabel")}</Label>
            <Select value={refKind} onValueChange={(next) => setRefKind(next as SourceUpdateRequestRefKind)}>
              <SelectTrigger id="source-ref-kind" className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="branch">{t("appAdd.githubRefKindBranch")}</SelectItem>
                <SelectItem value="tag">{t("appAdd.githubRefKindTag")}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="source-ref">{t("appDetail.sourceRefLabel")}</Label>
            <Input id="source-ref" value={ref} onChange={(event) => setRef(event.target.value)} />
          </div>
          <Button
            onClick={() => putSource.mutate({ name: app.id, data: { ref, ref_kind: refKind } }, { onSuccess: () => { setEditingRef(false); refresh(); } })}
            disabled={putSource.isPending || !ref}
          >
            {t("appDetail.sourceSaveButton")}
          </Button>
          </div>
        </div>
      ) : null}
      <ApiErrorAlert error={putSource.error} />
      <AlertDialog open={target !== null} onOpenChange={(open) => { if (!open) setTarget(null); }}>
        <AlertDialogContent>
          <AlertDialogHeader><AlertDialogTitle>{repairChangesVersion ? t("appDetail.updateAndRepair") : repairing ? t("appDetail.confirmPublicRepair") : t("appDetail.confirmPublicUpdate")}</AlertDialogTitle></AlertDialogHeader>
          {repairChangesVersion ? <p role="note" className="text-sm [overflow-wrap:anywhere]">{t("appDetail.repairVersionChange", { current: app.source.commit, target: target.commit })}</p> : null}
          {target?.url && target.url !== app.source.url ? <p>{t("appDetail.publicUpdateSource")} <a className="underline [overflow-wrap:anywhere]" href={target.url} target="_blank" rel="noreferrer">{target.url}</a></p> : null}
          {target?.ref ? <p>{target.ref}</p> : null}
          {target ? <div className="flex flex-col gap-2 text-sm">
            <p className="font-medium">{t("appDetail.targetDevices")}</p>
            <ul aria-label={t("appDetail.targetDevices")}>{target.devices.map((device) => <li key={device}>{deviceLabel(t, device)}</li>)}</ul>
            <p className="font-medium">{t("appDetail.targetEnv")}</p>
            <ul aria-label={t("appDetail.targetEnv")}>{target.env.map((entry) => <li key={entry.name}><code>{entry.name}</code> <Badge variant={entry.required ? "destructive" : "outline"}>{entry.required ? t("appAdd.envRequiredBadge") : t("appAdd.envOptionalBadge")}</Badge></li>)}</ul>
            {addedDevices.length ? <><p className="font-medium">{t("appDetail.addedDevices")}</p><ul aria-label={t("appDetail.addedDevices")}>{addedDevices.map((device) => <li key={device}>{deviceLabel(t, device)}</li>)}</ul></> : null}
            {addedRequiredEnv.length ? <><p className="font-medium">{t("appDetail.addedRequiredEnv")}</p><ul aria-label={t("appDetail.addedRequiredEnv")}>{addedRequiredEnv.map((entry) => <li key={entry.name}><code>{entry.name}</code></li>)}</ul></> : null}
          </div> : null}
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <Button onClick={() => { const commit = target?.commit; setTarget(null); if (commit) onUpdate(commit); }}>{repairChangesVersion ? t("appDetail.updateAndRepair") : repairing ? t("apps.repairButton") : t("appDetail.updateButton")}</Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Section>
  );
}

function KvRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="max-w-[60%] truncate text-right font-medium">{value}</dd>
    </div>
  );
}
