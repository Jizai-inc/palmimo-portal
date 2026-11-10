import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { Download, ExternalLink, Upload } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { PortalApiError } from "@/api/client";
import { getListAppsApiV1AppsGetQueryKey, useInstallPublicApiV1AppsInstallPublicPost, useListAppsApiV1AppsGet } from "@/api/generated/apps/apps";
import { getGetPublicCatalogApiV1CatalogPublicGetQueryKey, useGetPublicCatalogApiV1CatalogPublicGet } from "@/api/generated/catalog/catalog";
import { useListSecretsApiV1SecretsGet } from "@/api/generated/secrets/secrets";
import type { AppJobInfo, AppSummary, PublicCatalogAppInfo, ManifestPreviewResponse } from "@/api/generated/models";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { AppJobDialog } from "@/components/AppJobDialog";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { GitInstallSource, InstallSource } from "@/lib/appInstall";
import { installApp, previewApp } from "@/lib/appInstall";
import { isValidAppNamePart } from "@/lib/appNamePart";
import { appText } from "@/lib/appText";
import { formatLocalTimestamp } from "@/lib/formatTimestamp";
import { refreshAppQueries } from "@/lib/refreshAppQueries";
import { deviceLabel } from "@/lib/deviceLabel";
import { isHttpUrl } from "@/lib/isHttpUrl";
import { TEMPLATE_REPO_URL } from "@/lib/navigation";
import { tabClassName } from "@/lib/tabClassName";

export type AddAppTab = "catalog" | "github" | "zip";

const ALL_TABS: AddAppTab[] = ["catalog", "github", "zip"];

interface FlowProps {
  onInstalled: (jobId: string, displayName: string) => void;
  appRunning: boolean;
  inlineConfirm: boolean;
  onConfirmingChange: (confirming: boolean) => void;
}

/**
 * The add-app form and confirmation, hosted by `AddAppDialog` and `DiscoverPanel`. Free of
 * router hooks -- `onInstalled` is the only reach-out to routing, mirroring
 * `UpdatePanel`'s `onRestarted` -- so it's unit-testable directly. `onInstalled` receives the
 * finished job's `app_id` (the namespaced app id, known only once install completes) so the
 * caller can route straight to the new app's detail page.
 */
export function AddAppPanel({
  onInstalled = () => undefined,
  tabs = ALL_TABS,
  initialTab,
  inlineConfirm = false,
  onInstallStarted,
}: {
  onInstalled?: (appId: string | null) => void;
  /** Install sources offered, in order; the tab bar is hidden when there is only one. */
  tabs?: AddAppTab[];
  initialTab?: AddAppTab;
  /** Shows the install confirmation in place of the form instead of in its own dialog -- for hosts that are already a dialog. */
  inlineConfirm?: boolean;
  /** Takes over the install job (and its progress dialog) from this panel, for hosts that close before the job finishes. */
  onInstallStarted?: (jobId: string, displayName: string) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<AddAppTab>(initialTab && tabs.includes(initialTab) ? initialTab : tabs[0]);
  const [confirming, setConfirming] = useState(false);
  const [installJob, setInstallJob] = useState<{ jobId: string; name: string } | null>(null);
  const { data: appsData, error: appsError } = useListAppsApiV1AppsGet();
  const appRunning = (appsError ? [] : appsData?.apps ?? []).some((app) => ["running", "starting", "stopping"].includes(app.status));

  function handleJobDone(job: AppJobInfo) {
    void refreshAppQueries(queryClient);
    setInstallJob(null);
    onInstalled(job.app_id);
  }

  const startInstallJob = onInstallStarted ?? ((jobId: string, displayName: string) => setInstallJob({ jobId, name: displayName }));
  const flow: FlowProps = { onInstalled: startInstallJob, appRunning, inlineConfirm, onConfirmingChange: setConfirming };

  return (
    <div className="flex flex-col gap-4">
      <ApiErrorAlert error={appsError} />
      {tabs.length > 1 && !confirming ? (
        <div className="flex gap-6 border-b border-border">
          {tabs.map((option) => (
            <TabButton key={option} active={tab === option} onClick={() => setTab(option)}>
              {tabLabel(t, option)}
            </TabButton>
          ))}
        </div>
      ) : null}

      {tab === "catalog" ? <CatalogTab {...flow} installedApps={appsError ? [] : appsData?.apps ?? []} onOpenInstalled={onInstalled} /> : tab === "github" ? <GithubTab {...flow} /> : <ZipTab {...flow} />}

      <AppJobDialog
        jobId={installJob?.jobId ?? null}
        title={t("appAdd.installingTitle", { name: installJob?.name ?? "" })}
        onClose={() => setInstallJob(null)}
        onDone={handleJobDone}
      />
    </div>
  );
}

function tabLabel(t: TFunction, tab: AddAppTab): string {
  switch (tab) {
    case "catalog":
      return t("appAdd.tabCatalog");
    case "github":
      return t("appAdd.tabGithub");
    case "zip":
      return t("appAdd.tabZip");
  }
}

function TabButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button type="button" onClick={onClick} aria-current={active ? "page" : undefined} className={tabClassName(active)}>
      {children}
    </button>
  );
}

function EnvRequirementList({
  entries,
}: {
  entries: { name: string; required: boolean; description: string; helpUrl?: string | null; registered?: boolean }[];
}) {
  const { t } = useTranslation();
  if (entries.length === 0) return null;
  return (
    <div className="flex flex-col gap-1">
      <p className="text-xs font-medium text-muted-foreground">{t("appAdd.requiredEnvTitle")}</p>
      <ul className="flex flex-col gap-1">
        {entries.map((entry) => (
          <li key={entry.name} className="flex flex-wrap items-center gap-2 text-sm">
            <code className="font-mono text-xs">{entry.name}</code>
            <Badge variant={entry.required ? "destructive" : "outline"}>
              {entry.required ? t("appAdd.envRequiredBadge") : t("appAdd.envOptionalBadge")}
            </Badge>
            {entry.registered === false ? <Badge variant="outline">{t("appAdd.envNotRegisteredBadge")}</Badge> : null}
            <span className="text-muted-foreground">{entry.description}</span>
            {isHttpUrl(entry.helpUrl) ? (
              <a href={entry.helpUrl} target="_blank" rel="noreferrer" className="text-xs underline">
                {t("appAdd.envHelpLink")}
              </a>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Reason-specific text for a stale catalog, via explicit `t(...)` calls (see `appStatusLabel` for why). */
function catalogStaleReasonText(t: TFunction, reason: string | null): string {
  switch (reason) {
    case "persistence_failed":
      return t("appAdd.catalogPersistenceFailed");
    case "offline":
      return t("appAdd.catalogStaleReasonOffline");
    case "clock_unsynced":
      return t("appAdd.catalogStaleReasonClockUnsynced");
    case "rate_limited":
      return t("appAdd.catalogStaleReasonRateLimited");
    default:
      return t("appAdd.catalogStaleBanner");
  }
}

/**
 * Fetches an install preview and confirms the install, shared by the GitHub and zip tabs. The
 * confirmation opens in its own dialog, or with `inlineConfirm` replaces `children` (the
 * source form) in place, so a host dialog never stacks a second one.
 *
 * A 409 `app_exists` on install (the suggested/typed name part was taken between preview and
 * install) re-runs preview automatically instead of leaving the operator stuck on a name that
 * will never install.
 */
function InstallFlow({
  getSource,
  sourceKey,
  canPreview,
  appRunning,
  onInstalled,
  inlineConfirm = false,
  onConfirmingChange,
  children,
}: {
  getSource: () => InstallSource;
  sourceKey: unknown;
  canPreview: boolean;
  appRunning: boolean;
  onInstalled: (jobId: string, displayName: string) => void;
  inlineConfirm?: boolean;
  onConfirmingChange?: (confirming: boolean) => void;
  children?: React.ReactNode;
}) {
  const { t } = useTranslation();
  const [preview, setPreview] = useState<ManifestPreviewResponse | null>(null);
  const [namePart, setNamePart] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const sourceKeyRef = useRef(sourceKey);
  sourceKeyRef.current = sourceKey;

  const previewMutation = useMutation({
    mutationFn: async () => {
      const requestSourceKey = sourceKeyRef.current;
      return { preview: await previewApp(getSource()), sourceKey: requestSourceKey };
    },
    onSuccess: ({ preview: data, sourceKey: requestSourceKey }) => {
      if (requestSourceKey !== sourceKeyRef.current) {
        return;
      }
      setPreview(data);
      setNamePart(data.suggested_name);
    },
  });

  const install = useMutation({
    mutationFn: () => installApp(getSource(), namePart),
    onSuccess: (data) => onInstalled(data.job.id, preview?.name ?? namePart),
    onError: (error) => {
      if (error instanceof PortalApiError && error.code === "app_exists") {
        install.reset();
        previewMutation.mutate();
      }
    },
  });

  useEffect(() => {
    setPreview(null);
    setNamePart("");
    setDialogOpen(false);
  }, [sourceKey]);

  useEffect(() => {
    onConfirmingChange?.(inlineConfirm && dialogOpen);
  }, [inlineConfirm, dialogOpen, onConfirmingChange]);

  const namePartValid = isValidAppNamePart(namePart);

  const confirmBody = (
    <>
      {previewMutation.isPending ? <p className="text-sm text-muted-foreground">{t("common.loading")}</p> : null}
      <ApiErrorAlert error={previewMutation.error} />
      {preview ? (
        <PreviewCard preview={preview} namePart={namePart} namePartValid={namePartValid} onNamePartChange={setNamePart} />
      ) : null}
      <ApiErrorAlert error={install.error} />
    </>
  );
  const installButton = (
    <Button disabled={!preview || install.isPending || !namePartValid} onClick={() => install.mutate()}>
      <Download className="size-4" aria-hidden />
      {t("appAdd.installButton")}
    </Button>
  );

  if (inlineConfirm && dialogOpen) {
    return (
      <div className="flex flex-col gap-3">
        <p className="font-semibold">{t("appAdd.confirmInstallTitle")}</p>
        {confirmBody}
        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Button variant="outline" onClick={() => setDialogOpen(false)}>
            {t("common.back")}
          </Button>
          {installButton}
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {children}
      <Button
        disabled={!canPreview || appRunning}
        title={appRunning ? t("appAdd.installDisabledAppRunning") : undefined}
        onClick={() => {
          setDialogOpen(true);
          setPreview(null);
          setNamePart("");
          previewMutation.mutate();
        }}
      >
        {inlineConfirm ? null : <Download className="size-4" aria-hidden />}
        {inlineConfirm ? t("appAdd.continueButton") : t("appAdd.installButton")}
      </Button>
      {appRunning ? <p className="text-sm text-muted-foreground">{t("appAdd.installDisabledAppRunning")}</p> : null}
      {inlineConfirm ? null : (
        <AlertDialog open={dialogOpen} onOpenChange={setDialogOpen}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{t("appAdd.confirmInstallTitle")}</AlertDialogTitle>
            </AlertDialogHeader>
            {confirmBody}
            <AlertDialogFooter>
              <Button variant="outline" onClick={() => setDialogOpen(false)}>
                {t("common.cancel")}
              </Button>
              {installButton}
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      )}
    </div>
  );
}

function catalogAppTitle(app: PublicCatalogAppInfo): string {
  return app.public.official ? app.name : `${app.public.namespace}/${app.name}`;
}

function CatalogTab({ onInstalled, appRunning, installedApps, onOpenInstalled }: FlowProps & { installedApps: AppSummary[]; onOpenInstalled: (appId: string) => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data, error, isLoading } = useGetPublicCatalogApiV1CatalogPublicGet();
  const [kind, setKind] = useState("official");
  const [selected, setSelected] = useState<PublicCatalogAppInfo | null>(null);
  const [installError, setInstallError] = useState<unknown>(null);
  const install = useInstallPublicApiV1AppsInstallPublicPost({ mutation: {
    onSuccess: (response) => {
      const title = selected ? catalogAppTitle(selected) : "";
      setSelected(null);
      onInstalled(response.job.id, title);
    },
    onError: (failure) => {
      setSelected(null);
      setInstallError(failure);
      if (failure instanceof PortalApiError && failure.code === "public_app_changed") {
        void queryClient.invalidateQueries({ queryKey: getGetPublicCatalogApiV1CatalogPublicGetQueryKey() });
      }
    },
  } });
  useEffect(() => { if (error) setSelected(null); }, [error]);
  useEffect(() => {
    if (data) void queryClient.invalidateQueries({ queryKey: getListAppsApiV1AppsGetQueryKey() });
  }, [data, queryClient]);
  const catalog = error ? undefined : data;
  const apps = (catalog?.apps ?? []).filter((app) => app.public.official === (kind === "official"));

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <Label htmlFor="catalog-kind">{t("appAdd.catalogFilterLabel")}</Label>
        <select id="catalog-kind" value={kind} onChange={(event) => setKind(event.target.value)} className="h-9 w-fit rounded-md border border-input bg-background px-3 text-sm">
          <option value="official">{t("appAdd.catalogOfficialBadge")}</option>
          <option value="public">{t("appAdd.catalogPublicOption")}</option>
        </select>
      </div>
      <ApiErrorAlert error={error} />
      <ApiErrorAlert error={installError} />
      {catalog?.stale ? (
        <Alert>
          <AlertDescription className="flex flex-col gap-1">
            <span>{catalogStaleReasonText(t, catalog.reason)}</span>
            {catalog.fetched_at !== null ? (
              <span>{t("appAdd.catalogLastFetched")} <time dateTime={new Date(catalog.fetched_at * 1000).toISOString()}>{formatLocalTimestamp(catalog.fetched_at)}</time></span>
            ) : <span>{t("appAdd.catalogNeverFetched")}</span>}
          </AlertDescription>
        </Alert>
      ) : null}
      {error ? null : isLoading ? (
        <p className="text-sm text-muted-foreground">{t("common.loading")}</p>
      ) : apps.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("appAdd.catalogEmptyState")}</p>
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {apps.map((app) => {
            const installed = installedApps.find((record) => record.source.public_app_id === app.public.app_id);
            return (
              <div key={app.public.app_id} className="flex flex-col gap-2 rounded-xl border border-border bg-card p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="font-semibold">{catalogAppTitle(app)}</h3>
                  {installed ? <Badge variant="outline">{t("appAdd.installedBadge")}</Badge> : null}
                  {app.public.official ? <Badge>{t("appAdd.catalogOfficialBadge")}</Badge> : null}
                  {app.source.ref ? <Badge variant="outline">{app.source.ref}</Badge> : null}
                </div>
                <CatalogAppDetails app={app} />
                {installed ? <Button className="mt-auto w-fit" onClick={() => onOpenInstalled(installed.id)}>{t("appAdd.openInstalledButton")}</Button> : <Button className="mt-auto w-fit" disabled={appRunning || !app.source.commit} onClick={() => { setInstallError(null); setSelected(app); }}>
                  <Download className="size-4" aria-hidden />{t("appAdd.installButton")}
                </Button>}
                {appRunning && !installed ? <p className="text-sm text-muted-foreground">{t("appAdd.installDisabledAppRunning")}</p> : null}
              </div>
            );
          })}
        </div>
      )}
      <AlertDialog open={selected !== null && !error} onOpenChange={(open) => { if (!open && !install.isPending) setSelected(null); }}>
        <AlertDialogContent>
          <AlertDialogHeader><AlertDialogTitle>{selected ? catalogAppTitle(selected) : t("appAdd.confirmInstallTitle")}</AlertDialogTitle></AlertDialogHeader>
          {selected ? <>
            <CatalogAppDetails app={selected} />
            {!selected.public.official ? <Alert><AlertDescription className="flex flex-col gap-2">
              <p>{t("appAdd.publicWarning", { namespace: selected.public.namespace })}</p>
              {isHttpUrl(selected.source.url) ? <a href={selected.source.url} target="_blank" rel="noreferrer" className="underline [overflow-wrap:anywhere]">{t("appAdd.publicSourceLink")}</a> : null}
            </AlertDescription></Alert> : null}
          </> : null}
          <AlertDialogFooter>
            <Button variant="outline" disabled={install.isPending} onClick={() => setSelected(null)}>{t("common.cancel")}</Button>
            <Button disabled={install.isPending || appRunning || !selected?.source.commit} onClick={() => {
              if (selected?.source.commit) install.mutate({ data: { public_app_id: selected.public.app_id, commit: selected.source.commit } });
            }}>{t("appAdd.installButton")}</Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function CatalogAppDetails({ app }: { app: PublicCatalogAppInfo }) {
  const { t, i18n } = useTranslation();
  return <>
    <p className="text-sm text-muted-foreground">{appText(app.translations, i18n.language, "description", app.description)}</p>
    <EnvRequirementList entries={app.env.map((entry) => ({
      name: entry.name, required: entry.required,
      description: appText(app.translations, i18n.language, `env.${entry.name}`, entry.description), helpUrl: entry.help_url,
    }))} />
    {app.devices.length ? <div className="flex flex-wrap gap-1">{app.devices.map((device) => <Badge key={device} variant="outline">{deviceLabel(t, device)}</Badge>)}</div> : null}
    <a href={app.public_page_url ?? undefined} target="_blank" rel="noreferrer" className="text-sm underline">{t("appAdd.publicPageLink")}</a>
  </>;
}

function GithubTab({ onInstalled, appRunning, inlineConfirm, onConfirmingChange }: FlowProps) {
  const { t } = useTranslation();
  const [url, setUrl] = useState("");
  const [refKind, setRefKind] = useState<"branch" | "tag">("branch");
  const [ref, setRef] = useState("");
  const [subdir, setSubdir] = useState("");
  const [manifest, setManifest] = useState("");

  const source: GitInstallSource = {
    type: "git",
    url,
    ref,
    ref_kind: refKind,
    ...(subdir ? { subdir } : {}),
    ...(manifest ? { manifest } : {}),
  };
  const canPreview = url.trim() !== "" && ref.trim() !== "";
  const sourceKey = `${url}\u0000${refKind}\u0000${ref}\u0000${subdir}\u0000${manifest}`;

  return (
    <InstallFlow
      getSource={() => source}
      sourceKey={sourceKey}
      canPreview={canPreview}
      appRunning={appRunning}
      onInstalled={onInstalled}
      inlineConfirm={inlineConfirm}
      onConfirmingChange={onConfirmingChange}
    >
      <p className="text-sm text-muted-foreground">
        {t("apps.templateHint")}{" "}
        <a
          href={TEMPLATE_REPO_URL}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 font-medium text-foreground underline underline-offset-4"
        >
          {t("apps.templateButton")}
          <ExternalLink className="size-3.5" aria-hidden />
        </a>
      </p>
      <div className="flex flex-col gap-1">
        <Label htmlFor="github-url">{t("appAdd.githubUrlLabel")}</Label>
        <Input id="github-url" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://github.com/org/repo" />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor="github-ref-kind">{t("appAdd.githubRefKindLabel")}</Label>
        <Select value={refKind} onValueChange={(next) => setRefKind(next as "branch" | "tag")}>
          <SelectTrigger id="github-ref-kind">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="branch">{t("appAdd.githubRefKindBranch")}</SelectItem>
            <SelectItem value="tag">{t("appAdd.githubRefKindTag")}</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor="github-ref">{t("appAdd.githubRefLabel")}</Label>
        <Input id="github-ref" value={ref} onChange={(event) => setRef(event.target.value)} />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor="github-subdir">{t("appAdd.githubSubdirLabel")}</Label>
        <Input id="github-subdir" value={subdir} onChange={(event) => setSubdir(event.target.value)} />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor="github-manifest">{t("appAdd.manifestLabel")}</Label>
        <Input
          id="github-manifest"
          value={manifest}
          onChange={(event) => setManifest(event.target.value)}
          placeholder="palmimo.toml"
        />
      </div>
    </InstallFlow>
  );
}

function ZipTab({ onInstalled, appRunning, inlineConfirm, onConfirmingChange }: FlowProps) {
  const { t } = useTranslation();
  const [file, setFile] = useState<File | null>(null);
  const [manifest, setManifest] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const sourceKey = useMemo(() => ({ file, manifest }), [file, manifest]);

  function handleFile(chosenFile: File | undefined) {
    if (!chosenFile) return;
    setFile(chosenFile);
  }

  return (
    <InstallFlow
      getSource={() => ({ type: "zip", file: file as File, ...(manifest ? { manifest } : {}) })}
      sourceKey={sourceKey}
      canPreview={file !== null}
      appRunning={appRunning}
      onInstalled={onInstalled}
      inlineConfirm={inlineConfirm}
      onConfirmingChange={onConfirmingChange}
    >
      <div
        className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-input p-6 text-center"
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          handleFile(event.dataTransfer.files[0]);
        }}
      >
        <Upload className="size-5 text-muted-foreground" aria-hidden />
        <p className="text-sm text-muted-foreground">{t("appAdd.zipDropLabel")}</p>
        <Button type="button" variant="outline" onClick={() => fileInputRef.current?.click()}>
          {t("appAdd.zipChooseFileButton")}
        </Button>
        <input
          ref={fileInputRef}
          type="file"
          accept=".zip"
          className="hidden"
          aria-label={t("appAdd.zipChooseFileButton")}
          onChange={(event) => handleFile(event.target.files?.[0])}
        />
        {file ? <p className="text-sm font-medium">{file.name}</p> : null}
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor="zip-manifest">{t("appAdd.manifestLabel")}</Label>
        <Input
          id="zip-manifest"
          value={manifest}
          onChange={(event) => setManifest(event.target.value)}
          placeholder="palmimo.toml"
        />
      </div>
    </InstallFlow>
  );
}

function PreviewCard({
  preview,
  namePart,
  namePartValid,
  onNamePartChange,
}: {
  preview: ManifestPreviewResponse;
  namePart: string;
  namePartValid: boolean;
  onNamePartChange: (value: string) => void;
}) {
  const { t, i18n } = useTranslation();
  const { data: secretsData } = useListSecretsApiV1SecretsGet();
  const registeredNames = new Set((secretsData?.secrets ?? []).map((secret) => secret.name));
  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm">
        <span className="text-muted-foreground">{t("appAdd.manifestNameLabel")}: </span>
        <span className="font-semibold">{preview.name}</span>
      </p>
      <div className="flex flex-col gap-1">
        <Label htmlFor="app-name-part">{t("appAdd.deviceNameLabel")}</Label>
        <p className="text-xs text-muted-foreground">{t("appAdd.deviceNameHelp")}</p>
        <div className="flex items-center gap-1">
          <span className="shrink-0 text-sm text-muted-foreground">{preview.namespace}.</span>
          <Input
            id="app-name-part"
            value={namePart}
            aria-invalid={!namePartValid}
            onChange={(event) => onNamePartChange(event.target.value)}
          />
        </div>
        {!namePartValid ? <p className="text-xs text-destructive">{t("appAdd.deviceNameInvalid")}</p> : null}
      </div>
      <p className="text-sm text-muted-foreground">
        {appText(preview.translations, i18n.language, "description", preview.description)}
      </p>
      <EnvRequirementList
        entries={preview.env.map((entry) => ({
          name: entry.name,
          required: entry.required,
          description: appText(preview.translations, i18n.language, `env.${entry.name}`, entry.description),
          helpUrl: entry.help_url,
          registered: registeredNames.has(entry.name),
        }))}
      />
      {preview.devices.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          <p className="text-xs font-medium text-muted-foreground">{t("appAdd.devicesTitle")}</p>
          {preview.devices.map((device) => (
            <Badge key={device} variant="outline">
              {deviceLabel(t, device)}
            </Badge>
          ))}
        </div>
      ) : null}
    </div>
  );
}
