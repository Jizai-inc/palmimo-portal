import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { Upload } from "lucide-react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { getListAppsApiV1AppsGetQueryKey } from "@/api/generated/apps/apps";
import { useGetCatalogApiV1CatalogGet } from "@/api/generated/catalog/catalog";
import { useListSecretsApiV1SecretsGet } from "@/api/generated/secrets/secrets";
import type { AppJobInfo, CatalogAppInfo, ManifestPreviewResponse } from "@/api/generated/models";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { AppJobDialog } from "@/components/AppJobDialog";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { GitInstallSource, InstallSource } from "@/lib/appInstall";
import { installApp, previewApp } from "@/lib/appInstall";
import { parseCatalogSource } from "@/lib/catalogSource";
import { deviceLabel } from "@/lib/deviceLabel";
import { isHttpUrl } from "@/lib/isHttpUrl";

type Tab = "catalog" | "github" | "zip";

/**
 * The add-app screen's logic (see routes/apps.add.tsx, which wraps this in `AppShell`). Free of
 * router hooks -- `onInstalled` is the only reach-out to routing, mirroring
 * `UpdatePanel`'s `onRestarted` -- so it's unit-testable directly. `onInstalled` receives the
 * finished job's `app_name` (the manifest's declared name, known only once install completes) so
 * the caller can route straight to the new app's detail page.
 */
export function AddAppPanel({ onInstalled = () => undefined }: { onInstalled?: (appName: string | null) => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<Tab>("catalog");
  const [installJob, setInstallJob] = useState<{ jobId: string; name: string } | null>(null);

  const install = useMutation({
    mutationFn: (source: InstallSource) => installApp(source),
    onSuccess: (data, source) => {
      const name = source.type === "zip" ? source.file.name.replace(/\.zip$/i, "") : source.url;
      setInstallJob({ jobId: data.job.id, name });
    },
  });

  function handleJobDone(job: AppJobInfo) {
    void queryClient.invalidateQueries({ queryKey: getListAppsApiV1AppsGetQueryKey() });
    setInstallJob(null);
    onInstalled(job.app_name);
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex gap-2 border-b border-border">
        <TabButton active={tab === "catalog"} onClick={() => setTab("catalog")}>
          {t("appAdd.tabCatalog")}
        </TabButton>
        <TabButton active={tab === "github"} onClick={() => setTab("github")}>
          {t("appAdd.tabGithub")}
        </TabButton>
        <TabButton active={tab === "zip"} onClick={() => setTab("zip")}>
          {t("appAdd.tabZip")}
        </TabButton>
      </div>

      <ApiErrorAlert error={install.error} />

      {tab === "catalog" ? (
        <CatalogTab onInstall={(source) => install.mutate(source)} installPending={install.isPending} />
      ) : tab === "github" ? (
        <GithubTab onInstall={(source) => install.mutate(source)} installPending={install.isPending} />
      ) : (
        <ZipTab onInstall={(source) => install.mutate(source)} installPending={install.isPending} />
      )}

      <AppJobDialog
        jobId={installJob?.jobId ?? null}
        title={t("appAdd.installingTitle", { name: installJob?.name ?? "" })}
        onClose={() => setInstallJob(null)}
        onDone={handleJobDone}
      />
    </div>
  );
}

function TabButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      className={`border-b-2 px-3 py-2 text-sm font-medium ${active ? "border-primary text-foreground" : "border-transparent text-muted-foreground hover:text-foreground"}`}
    >
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

function CatalogTab({
  onInstall,
  installPending,
}: {
  onInstall: (source: GitInstallSource) => void;
  installPending: boolean;
}) {
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
            <CatalogCard key={catalogApp.name} app={catalogApp} onInstall={onInstall} installPending={installPending} />
          ))}
        </div>
      )}
    </div>
  );
}

function CatalogCard({
  app,
  onInstall,
  installPending,
}: {
  app: CatalogAppInfo;
  onInstall: (source: GitInstallSource) => void;
  installPending: boolean;
}) {
  const { t } = useTranslation();
  const env = app.env.map((entry) => ({
    name: entry.name,
    required: entry.required,
    description: entry.description,
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
      <p className="text-sm text-muted-foreground">{app.description}</p>
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
      <Button className="w-fit" disabled={!source || installPending} onClick={() => source && onInstall(source)}>
        {t("appAdd.installButton")}
      </Button>
    </div>
  );
}

function GithubTab({
  onInstall,
  installPending,
}: {
  onInstall: (source: GitInstallSource) => void;
  installPending: boolean;
}) {
  const { t } = useTranslation();
  const [url, setUrl] = useState("");
  const [refKind, setRefKind] = useState<"branch" | "tag">("branch");
  const [ref, setRef] = useState("");
  const [subdir, setSubdir] = useState("");
  const [manifest, setManifest] = useState("");
  const [preview, setPreview] = useState<ManifestPreviewResponse | null>(null);

  const source: GitInstallSource = {
    type: "git",
    url,
    ref,
    ref_kind: refKind,
    ...(subdir ? { subdir } : {}),
    ...(manifest ? { manifest } : {}),
  };
  const canSubmit = url.trim() !== "" && ref.trim() !== "";

  const previewMutation = useMutation({
    mutationFn: () => previewApp(source),
    onSuccess: setPreview,
  });

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <Label htmlFor="github-url">{t("appAdd.githubUrlLabel")}</Label>
        <Input id="github-url" value={url} onChange={(event) => { setUrl(event.target.value); setPreview(null); }} placeholder="https://github.com/org/repo" />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor="github-ref-kind">{t("appAdd.githubRefKindLabel")}</Label>
        <select
          id="github-ref-kind"
          className="h-10 rounded-md border border-input bg-transparent px-3 text-sm"
          value={refKind}
          onChange={(event) => { setRefKind(event.target.value as "branch" | "tag"); setPreview(null); }}
        >
          <option value="branch">{t("appAdd.githubRefKindBranch")}</option>
          <option value="tag">{t("appAdd.githubRefKindTag")}</option>
        </select>
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor="github-ref">{t("appAdd.githubRefLabel")}</Label>
        <Input id="github-ref" value={ref} onChange={(event) => { setRef(event.target.value); setPreview(null); }} />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor="github-subdir">{t("appAdd.githubSubdirLabel")}</Label>
        <Input id="github-subdir" value={subdir} onChange={(event) => { setSubdir(event.target.value); setPreview(null); }} />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor="github-manifest">{t("appAdd.manifestLabel")}</Label>
        <Input
          id="github-manifest"
          value={manifest}
          onChange={(event) => { setManifest(event.target.value); setPreview(null); }}
          placeholder="palmimo.toml"
        />
      </div>
      <ApiErrorAlert error={previewMutation.error} />
      <div className="flex gap-2">
        <Button variant="outline" disabled={!canSubmit || previewMutation.isPending} onClick={() => previewMutation.mutate()}>
          {t("appAdd.previewButton")}
        </Button>
        {preview ? (
          <Button disabled={installPending} onClick={() => onInstall(source)}>
            {t("appAdd.installButton")}
          </Button>
        ) : null}
      </div>
      {preview ? <PreviewCard preview={preview} /> : null}
    </div>
  );
}

function ZipTab({
  onInstall,
  installPending,
}: {
  onInstall: (source: InstallSource) => void;
  installPending: boolean;
}) {
  const { t } = useTranslation();
  const [file, setFile] = useState<File | null>(null);
  const [manifest, setManifest] = useState("");
  const [preview, setPreview] = useState<ManifestPreviewResponse | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const sourceFor = (chosenFile: File): InstallSource => ({
    type: "zip",
    file: chosenFile,
    ...(manifest ? { manifest } : {}),
  });

  const previewMutation = useMutation({
    mutationFn: (chosenFile: File) => previewApp(sourceFor(chosenFile)),
    onSuccess: setPreview,
  });

  function handleFile(chosenFile: File | undefined) {
    if (!chosenFile) return;
    setFile(chosenFile);
    setPreview(null);
  }

  return (
    <div className="flex flex-col gap-3">
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
          onChange={(event) => { setManifest(event.target.value); setPreview(null); }}
          placeholder="palmimo.toml"
        />
      </div>
      <ApiErrorAlert error={previewMutation.error} />
      <div className="flex gap-2">
        <Button variant="outline" disabled={!file || previewMutation.isPending} onClick={() => file && previewMutation.mutate(file)}>
          {t("appAdd.previewButton")}
        </Button>
        {preview && file ? (
          <Button disabled={installPending} onClick={() => onInstall(sourceFor(file))}>
            {t("appAdd.installButton")}
          </Button>
        ) : null}
      </div>
      {preview ? <PreviewCard preview={preview} /> : null}
    </div>
  );
}

function PreviewCard({ preview }: { preview: ManifestPreviewResponse }) {
  const { t } = useTranslation();
  const { data: secretsData } = useListSecretsApiV1SecretsGet();
  const registeredNames = new Set((secretsData?.secrets ?? []).map((secret) => secret.name));
  return (
    <div className="flex flex-col gap-2 rounded-xl border border-border bg-card p-4">
      <p className="font-semibold">{preview.name}</p>
      <p className="text-sm text-muted-foreground">{preview.description}</p>
      <EnvRequirementList
        entries={preview.env.map((entry) => ({
          name: entry.name,
          required: entry.required,
          description: entry.description,
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
