import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { Upload } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { PortalApiError } from "@/api/client";
import { getListAppsApiV1AppsGetQueryKey, useListAppsApiV1AppsGet } from "@/api/generated/apps/apps";
import { useGetCatalogApiV1CatalogGet } from "@/api/generated/catalog/catalog";
import { useListSecretsApiV1SecretsGet } from "@/api/generated/secrets/secrets";
import type { AppJobInfo, CatalogAppInfo, ManifestPreviewResponse } from "@/api/generated/models";
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
import type { GitInstallSource, InstallSource } from "@/lib/appInstall";
import { installApp, previewApp } from "@/lib/appInstall";
import { isValidAppNamePart } from "@/lib/appNamePart";
import { appText } from "@/lib/appText";
import { parseCatalogSource } from "@/lib/catalogSource";
import { deviceLabel } from "@/lib/deviceLabel";
import { isHttpUrl } from "@/lib/isHttpUrl";
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
 * The add-app screen's logic (see routes/apps.add.tsx, which wraps this in `AppShell`). Free of
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
  const { data: appsData } = useListAppsApiV1AppsGet();
  const appRunning = (appsData?.apps ?? []).some((app) => ["running", "starting", "stopping"].includes(app.status));

  function handleJobDone(job: AppJobInfo) {
    void queryClient.invalidateQueries({ queryKey: getListAppsApiV1AppsGetQueryKey() });
    setInstallJob(null);
    onInstalled(job.app_id);
  }

  const startInstallJob = onInstallStarted ?? ((jobId: string, displayName: string) => setInstallJob({ jobId, name: displayName }));
  const flow: FlowProps = { onInstalled: startInstallJob, appRunning, inlineConfirm, onConfirmingChange: setConfirming };

  return (
    <div className="flex flex-col gap-4">
      {tabs.length > 1 && !confirming ? (
        <div className="flex gap-2 border-b border-border">
          {tabs.map((option) => (
            <TabButton key={option} active={tab === option} onClick={() => setTab(option)}>
              {tabLabel(t, option)}
            </TabButton>
          ))}
        </div>
      ) : null}

      {tab === "catalog" ? <CatalogTab {...flow} /> : tab === "github" ? <GithubTab {...flow} /> : <ZipTab {...flow} />}

      <AppJobDialog
        jobId={installJob?.jobId ?? null}
        title={t("appAdd.installingTitle", { name: installJob?.name ?? "" })}
        onClose={() => setInstallJob(null)}
        onDone={handleJobDone}
      />
    </div>
  );
}

/** Via explicit `t(...)` calls so the i18n-parity scan sees every key (see `appStatusLabel`). */
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
 * Fetches an install preview and confirms the install, shared by all three add-app tabs. The
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

function CatalogTab({ onInstalled, appRunning }: FlowProps) {
  const { t } = useTranslation();
  const { data, isLoading } = useGetCatalogApiV1CatalogGet();

  return (
    <div className="flex flex-col gap-3">
      {data?.stale ? (
        <Alert>
          <AlertDescription>{catalogStaleReasonText(t, data.reason)}</AlertDescription>
        </Alert>
      ) : null}
      {isLoading ? (
        <p className="text-sm text-muted-foreground">{t("common.loading")}</p>
      ) : (data?.apps ?? []).length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("appAdd.catalogEmptyState")}</p>
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {(data?.apps ?? []).map((catalogApp) => (
            <CatalogCard key={catalogApp.name} app={catalogApp} onInstalled={onInstalled} appRunning={appRunning} />
          ))}
        </div>
      )}
    </div>
  );
}

function CatalogCard({
  app,
  onInstalled,
  appRunning,
}: {
  app: CatalogAppInfo;
  onInstalled: (jobId: string, displayName: string) => void;
  appRunning: boolean;
}) {
  const { t, i18n } = useTranslation();
  const env = app.env.map((entry) => ({
    name: entry.name,
    required: entry.required,
    description: appText(app.translations, i18n.language, `env.${entry.name}`, entry.description),
    helpUrl: entry.help_url,
  }));
  const source = parseCatalogSource(app.source);

  return (
    <div className="flex flex-col gap-2 rounded-xl border border-border bg-card p-4">
      <div className="flex flex-wrap items-center gap-2">
        <p className="font-semibold">{app.name}</p>
        <Badge>{t("appAdd.catalogOfficialBadge")}</Badge>
        {source ? <Badge variant="outline">{source.ref}</Badge> : null}
      </div>
      <p className="text-sm text-muted-foreground">
        {appText(app.translations, i18n.language, "description", app.description)}
      </p>
      <EnvRequirementList entries={env} />
      {app.devices.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {app.devices.map((device) => (
            <Badge key={device} variant="outline">
              {deviceLabel(t, device)}
            </Badge>
          ))}
        </div>
      ) : null}
      {source ? (
        <InstallFlow
          getSource={() => source}
          sourceKey={`${source.url}\u0000${source.ref_kind}\u0000${source.ref}\u0000${source.subdir ?? ""}\u0000${source.manifest ?? ""}`}
          canPreview
          appRunning={appRunning}
          onInstalled={onInstalled}
        />
      ) : null}
    </div>
  );
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
      <div className="flex flex-col gap-1">
        <Label htmlFor="github-url">{t("appAdd.githubUrlLabel")}</Label>
        <Input id="github-url" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://github.com/org/repo" />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor="github-ref-kind">{t("appAdd.githubRefKindLabel")}</Label>
        <select
          id="github-ref-kind"
          className="h-10 rounded-md border border-input bg-transparent px-3 text-sm"
          value={refKind}
          onChange={(event) => setRefKind(event.target.value as "branch" | "tag")}
        >
          <option value="branch">{t("appAdd.githubRefKindBranch")}</option>
          <option value="tag">{t("appAdd.githubRefKindTag")}</option>
        </select>
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
