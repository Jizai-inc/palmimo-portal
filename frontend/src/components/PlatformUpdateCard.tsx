import { useTranslation } from "react-i18next";

import { PortalApiError } from "@/api/client";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ProgressBar } from "@/components/ProgressBar";
import { isVersionAtLeast } from "@/lib/compareVersions";
import { formatUtcTimestamp } from "@/lib/formatTimestamp";
import { usePlatformUpdate } from "@/lib/usePlatformUpdate";

/** A 429 rate limit is not actionable, so it stays silent, matching UpdatePanel's own check button. */
function isSilentRateLimit(error: unknown): boolean {
  return error instanceof PortalApiError && error.status === 429;
}

/**
 * `platform.latest_error`'s two named codes get their own message; anything else (``no_release``,
 * a missing release asset, or any other fetch/parse failure -- see `core/platform.py`'s
 * `PlatformLatestCache._refresh`) is free text, not a small enum, so it collapses into one
 * generic "could not check" message rather than being shown verbatim.
 */
function PlatformLatestErrorMessage({ error }: { error: string | null }) {
  const { t } = useTranslation();
  if (error === "clock_unsynced") {
    return <p className="text-sm text-muted-foreground">{t("update.platformLatestErrorClockUnsynced")}</p>;
  }
  if (error === "rate_limited") {
    return <p className="text-sm text-muted-foreground">{t("update.platformLatestErrorRateLimited")}</p>;
  }
  return <p className="text-sm text-muted-foreground">{t("update.platformLatestErrorUnknown")}</p>;
}

/**
 * The update screen's "device platform" row (design doc 2.8/3.7): installed vs. latest bundle
 * version, a verify-diff warning, and the same update job the apps-tab banner triggers (shared
 * via `usePlatformUpdate`).
 */
export function PlatformUpdateCard({
  installedPortalVersion,
  portalUpdateRunning = false,
}: {
  installedPortalVersion?: string;
  /** Disables "Check now" while a Portal self-update job is running, mirroring the platform job's own gate. */
  portalUpdateRunning?: boolean;
}) {
  const { t } = useTranslation();
  const { platform, platformError, job, startUpdate, starting, startError, checkNow, checking, checkError } =
    usePlatformUpdate();

  if (!platform) {
    return <ApiErrorAlert error={platformError} />;
  }

  const latestKnown = platform.latest !== null;
  const upToDate = latestKnown && platform.installed_version !== null && platform.latest!.version <= platform.installed_version;
  const updateAvailable = latestKnown && !upToDate;
  const portalTooOld =
    updateAvailable &&
    installedPortalVersion !== undefined &&
    !isVersionAtLeast(installedPortalVersion, platform.latest!.requires_portal);
  const legacyLedgerError =
    (startError instanceof PortalApiError && startError.code === "ledger_legacy") ||
    (checkError instanceof PortalApiError && checkError.code === "ledger_legacy");

  return (
    <div className="flex flex-col gap-4 rounded-xl border border-border bg-card p-4">
      <div className="flex items-center gap-2">
        <p className="font-semibold">{t("update.platformTitle")}</p>
        {updateAvailable ? <Badge variant="outline" className="ml-auto">{platform.latest?.tag}</Badge> : null}
      </div>
      {updateAvailable ? <p className="text-sm text-muted-foreground">{t("update.platformNewVersionAvailable")}</p> : null}
      <div className="border-t border-border" />
      <dl className="flex flex-col gap-2 text-sm">
        <div className="flex items-center justify-between gap-2">
          <dt className="text-muted-foreground">{t("update.platformInstalledVersionLabel")}</dt>
          <dd className="font-medium">{platform.installed_version ?? "—"}</dd>
        </div>
        {platform.latest ? (
          <div className="flex items-center justify-between gap-2">
            <dt className="text-muted-foreground">{t("update.platformLatestVersionLabel")}</dt>
            <dd className="flex items-center gap-2 font-medium">
              <span>
                {platform.latest.version} ({platform.latest.tag})
              </span>
              <Button
                variant="ghost"
                size="sm"
                onClick={checkNow}
                disabled={checking || starting || job?.state === "running" || portalUpdateRunning}
              >
                {t("update.platformCheckNowButton")}
              </Button>
            </dd>
          </div>
        ) : null}
        {platform.latest?.summary ? (
          <div className="flex flex-col gap-1">
            <dt className="text-muted-foreground">{t("update.platformSummaryLabel")}</dt>
            <dd>{platform.latest.summary}</dd>
          </div>
        ) : null}
        {platform.installed_at ? (
          <div className="flex items-center justify-between gap-2">
            <dt className="text-muted-foreground">{t("appDetail.sourceInstalledAtLabel")}</dt>
            <dd className="font-medium">{formatUtcTimestamp(Date.parse(platform.installed_at) / 1000)}</dd>
          </div>
        ) : null}
      </dl>

      {platform.verify_diffs.length > 0 ? (
        <Alert variant="destructive">
          <AlertDescription>{t("update.platformVerifyDiffBanner")}</AlertDescription>
        </Alert>
      ) : null}

      {job?.state === "running" ? (
        <div className="flex flex-col gap-2 rounded-lg bg-muted p-3">
          <ProgressBar label={t("common.inProgress")} />
        </div>
      ) : job?.state === "failed" ? (
        <Alert variant="destructive">
          <AlertDescription>{job.error ?? t("errors.install_failed")}</AlertDescription>
        </Alert>
      ) : null}

      {legacyLedgerError ? (
        <Alert>
          <AlertDescription>{t("update.platformLegacyLedger")}</AlertDescription>
        </Alert>
      ) : null}
      <ApiErrorAlert error={startError instanceof PortalApiError && startError.code === "ledger_legacy" ? undefined : startError} />
      <ApiErrorAlert
        error={
          isSilentRateLimit(checkError) || (checkError instanceof PortalApiError && checkError.code === "ledger_legacy")
            ? undefined
            : checkError
        }
      />

      {platform.latest?.reflash_required ? (
        <Alert>
          <AlertDescription>{t("update.platformReflashRequired")}</AlertDescription>
        </Alert>
      ) : portalTooOld ? (
        <Alert>
          <AlertDescription>{t("update.platformPortalTooOld")}</AlertDescription>
        </Alert>
      ) : !latestKnown ? (
        <PlatformLatestErrorMessage error={platform.latest_error} />
      ) : upToDate ? (
        <p className="text-sm text-muted-foreground">{t("update.platformUpToDate")}</p>
      ) : (
        <Button
          className="w-fit"
          onClick={startUpdate}
          disabled={starting || job?.state === "running"}
        >
          {t("update.platformUpdateButton")}
        </Button>
      )}
    </div>
  );
}
