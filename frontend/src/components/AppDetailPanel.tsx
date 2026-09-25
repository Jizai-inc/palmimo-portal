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
  usePutAutostartApiV1AppsNameAutostartPut,
  usePutBindingsApiV1AppsNameBindingsPut,
  usePutParamsApiV1AppsNameParamsPut,
  usePutSourceApiV1AppsNameSourcePut,
  useStartAppEndpointApiV1AppsNameStartPost,
  useStopAppEndpointApiV1AppsNameStopPost,
  useUpdateAppApiV1AppsNameUpdatePost,
  useUpdateCheckApiV1AppsNameUpdateCheckPost,
} from "@/api/generated/apps/apps";
import { useListSecretsApiV1SecretsGet } from "@/api/generated/secrets/secrets";
import type { AppDetailResponse, ParamSpecInfo, SourceUpdateRequestRefKind } from "@/api/generated/models";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { AppIdLabel, appIdNamePart } from "@/components/AppIdLabel";
import { AppJobDialog } from "@/components/AppJobDialog";
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
import { appStatusLabel, appStatusTone, isAppStatusBusy } from "@/lib/appStatus";
import { copyText } from "@/lib/copyText";
import { deviceLabel } from "@/lib/deviceLabel";
import { formatLocalTimestamp, formatUtcTimestamp } from "@/lib/formatTimestamp";
import { useAppLogs } from "@/lib/useAppLogs";

export function AppDetailPanel({ id, onDeleted = () => undefined }: { id: string; onDeleted?: () => void }) {
  const { t } = useTranslation();
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
        return query.state.data && isAppStatusBusy(query.state.data.status) ? 3_000 : false;
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

  const startApp = useStartAppEndpointApiV1AppsNameStartPost({ mutation: { onSuccess: invalidate } });
  const stopApp = useStopAppEndpointApiV1AppsNameStopPost({ mutation: { onSuccess: invalidate } });
  const updateApp = useUpdateAppApiV1AppsNameUpdatePost({
    mutation: { onSuccess: (data) => setJob({ id: data.job.id, kind: "update" }) },
  });
  const { data: appsData } = useListAppsApiV1AppsGet();
  const appRunning = (appsData?.apps ?? []).some((candidate) => ["running", "starting", "stopping"].includes(candidate.status));
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

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-xl"><AppIdLabel id={app.id} /></h2>
          <Badge variant={tone === "green" ? "default" : tone === "red" ? "destructive" : tone === "muted" ? "secondary" : "outline"} className="flex items-center gap-1">
            {busy ? <Loader2 className="size-3 animate-spin" /> : null}
            {appStatusLabel(t, app.status, app.exit_code)}
          </Badge>
          {app.source.official ? <Badge>{t("appAdd.catalogOfficialBadge")}</Badge> : null}
          {app.autostart ? <Badge variant="outline">{t("apps.autostartBadge")}</Badge> : null}
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
        {app.name !== appIdNamePart(app.id) ? <p className="text-sm text-muted-foreground">{app.name}</p> : null}
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

      <AutostartSection app={app} onSaved={invalidate} />
      <SourceSection app={app} onUpdate={() => updateApp.mutate({ name: id })} updatePending={updateApp.isPending} updateCompleted={updateCompleted} appRunning={appRunning} />

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
            invalidate();
            void queryClient.invalidateQueries({ queryKey: getListAppsApiV1AppsGetQueryKey() });
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

function BindingsSection({ app, onSaved }: { app: AppDetailResponse; onSaved: () => void }) {
  const { t } = useTranslation();
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
          const unbound = envVar.required && !bindings[envVar.name];
          return (
            <div key={envVar.name} className={`flex flex-wrap items-center gap-2 rounded-md p-2 ${unbound ? "bg-destructive/10" : ""}`}>
              <div className="flex min-w-0 flex-1 flex-col">
                <div className="flex items-center gap-2">
                  <code className="font-mono text-sm">{envVar.name}</code>
                  <Badge variant={envVar.required ? "destructive" : "outline"}>
                    {envVar.required ? t("appAdd.envRequiredBadge") : t("appAdd.envOptionalBadge")}
                  </Badge>
                </div>
                <p className="text-xs text-muted-foreground">{envVar.description}</p>
                {unbound ? <p className="text-xs text-destructive">{t("appDetail.bindingsUnboundRequiredWarning")}</p> : null}
              </div>
              <select
                aria-label={envVar.name}
                className="h-10 rounded-md border border-input bg-transparent px-3 text-sm"
                value={bindings[envVar.name] ?? ""}
                onChange={(event) => setBindings((current) => ({ ...current, [envVar.name]: event.target.value }))}
              >
                <option value="">{t("appDetail.bindingsNotBound")}</option>
                {secretNames.map((secretName) => (
                  <option key={secretName} value={secretName}>{secretName}</option>
                ))}
              </select>
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
  const { t } = useTranslation();
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
            const value = values[spec.name];
            const id = `param-${spec.name}`;
            return (
              <div key={spec.name} className="flex flex-col gap-1">
                <div className="flex items-center gap-2">
                  <Label htmlFor={id} className="w-40 shrink-0 font-mono text-xs">{spec.name}</Label>
                  {spec.type === "enum" ? (
                    <select
                      id={id}
                      className="h-10 rounded-md border border-input bg-transparent px-3 text-sm"
                      value={typeof value === "string" ? value : ""}
                      onChange={(event) => setValues((current) => ({ ...current, [spec.name]: event.target.value }))}
                    >
                      {(spec.choices ?? []).map((choice) => (
                        <option key={choice} value={choice}>
                          {choice}
                        </option>
                      ))}
                    </select>
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
                {spec.description ? <p className="pl-40 text-xs text-muted-foreground">{spec.description}</p> : null}
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
  const { t, i18n } = useTranslation();
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const { unavailable, invocations, invocation, setInvocation, accumulated, text, refetch } = useAppLogs(id, status);

  if (unavailable) {
    return (
      <Section title={t("appDetail.logsTitle")}>
        <p className="text-sm text-muted-foreground">{t("appDetail.logsUnavailable")}</p>
      </Section>
    );
  }

  async function handleCopy() {
    const ok = await copyText(text);
    setCopied(ok);
    setCopyFailed(!ok);
    setTimeout(() => { setCopied(false); setCopyFailed(false); }, 2000);
  }

  return (
    <Section title={t("appDetail.logsTitle")}>
      <p className="text-xs text-muted-foreground">{t("appDetail.logsReusedIdNote")}</p>
      <div className="flex flex-wrap items-center gap-2">
        {invocations.length > 1 && invocation !== null ? (
          <select
            aria-label={t("appDetail.logsInvocationLabel")}
            className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
            value={invocation ?? ""}
            onChange={(event) => setInvocation(event.target.value)}
          >
            {invocations.map((start) => (
              <option key={start.id} value={start.id}>
                {t("appDetail.logsInvocationAt", { time: formatLocalTimestamp(start.started_at, { locale: i18n.language }) })}
              </option>
            ))}
          </select>
        ) : null}
        <Button type="button" variant="outline" size="sm" onClick={() => void handleCopy()}>
          {copied ? t("appDetail.logsCopied") : copyFailed ? t("appDetail.logsCopyFailed") : t("appDetail.logsCopyButton")}
        </Button>
        <Button type="button" variant="ghost" size="sm" asChild>
          <Link to="/apps/$id/logs" params={{ id }}>{t("appDetail.logsExpandButton")}</Link>
        </Button>
        {status !== "running" ? (
          <Button type="button" variant="ghost" size="sm" onClick={refetch}>
            {t("appDetail.logsLoadMoreButton")}
          </Button>
        ) : null}
      </div>
      {accumulated.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("appDetail.logsEmptyState")}</p>
      ) : (
        <pre className="max-h-64 overflow-auto rounded-md bg-muted p-2 font-mono text-xs">{text}</pre>
      )}
    </Section>
  );
}

function AutostartSection({ app, onSaved }: { app: AppDetailResponse; onSaved: () => void }) {
  const { t } = useTranslation();
  const putAutostart = usePutAutostartApiV1AppsNameAutostartPut({ mutation: { onSuccess: onSaved } });
  return (
    <Section title={t("appDetail.autostartTitle")}>
      <p className="text-sm text-muted-foreground">{t("appDetail.autostartBody")}</p>
      <ApiErrorAlert error={putAutostart.error} />
      <label className="flex w-fit items-center gap-2 text-sm">
        <input
          type="checkbox"
          role="switch"
          aria-checked={app.autostart}
          checked={app.autostart}
          disabled={putAutostart.isPending}
          onChange={(event) => putAutostart.mutate({ name: app.id, data: { enabled: event.target.checked } })}
        />
        {t("appDetail.autostartTitle")}
      </label>
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
  onUpdate: () => void;
  updatePending: boolean;
  updateCompleted: number;
  appRunning: boolean;
}) {
  const { t } = useTranslation();
  const [editingRef, setEditingRef] = useState(false);
  const [refKind, setRefKind] = useState<SourceUpdateRequestRefKind>("branch");
  const [ref, setRef] = useState("");
  const putSource = usePutSourceApiV1AppsNameSourcePut();
  const updateCheck = useUpdateCheckApiV1AppsNameUpdateCheckPost();
  const { reset: resetUpdateCheck } = updateCheck;

  useEffect(() => {
    resetUpdateCheck();
  }, [resetUpdateCheck, updateCompleted]);

  const isGit = app.source.type === "git";

  return (
    <Section title={t("appDetail.sourceTitle")}>
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
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={() => setEditingRef((value) => !value)}>{t("appDetail.sourceEditRefButton")}</Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => updateCheck.mutate({ name: app.id })}
            disabled={updateCheck.isPending}
          >
            {t("appDetail.checkForUpdatesButton")}
          </Button>
        </div>
      ) : null}
      {updateCheck.data ? (
        updateCheck.data.update_available ? (
          <div className="flex items-center gap-2">
            <p className="text-sm">{t("apps.updateAvailableChip")}</p>
            <Button size="sm" onClick={onUpdate} disabled={updatePending || appRunning} title={appRunning ? t("appDetail.updateDisabledAppRunning") : undefined}>{t("appDetail.updateButton")}</Button>
            {appRunning ? <p className="text-sm text-muted-foreground">{t("appDetail.updateDisabledAppRunning")}</p> : null}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">{t("appDetail.checkForUpdatesUpToDate")}</p>
        )
      ) : null}
      <ApiErrorAlert error={updateCheck.error} />
      {editingRef ? (
        <div className="flex flex-col gap-2">
          <p className="text-sm font-medium">{t("appDetail.sourceEditRefTitle")}</p>
          <div className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="source-ref-kind">{t("appAdd.githubRefKindLabel")}</Label>
            <select
              id="source-ref-kind"
              className="h-10 rounded-md border border-input bg-transparent px-3 text-sm"
              value={refKind}
              onChange={(event) => setRefKind(event.target.value as SourceUpdateRequestRefKind)}
            >
              <option value="branch">{t("appAdd.githubRefKindBranch")}</option>
              <option value="tag">{t("appAdd.githubRefKindTag")}</option>
            </select>
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="source-ref">{t("appDetail.sourceRefLabel")}</Label>
            <Input id="source-ref" value={ref} onChange={(event) => setRef(event.target.value)} />
          </div>
          <Button
            onClick={() => putSource.mutate({ name: app.id, data: { ref, ref_kind: refKind } }, { onSuccess: () => setEditingRef(false) })}
            disabled={putSource.isPending || !ref}
          >
            {t("appDetail.sourceSaveButton")}
          </Button>
          </div>
        </div>
      ) : null}
      <ApiErrorAlert error={putSource.error} />
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
