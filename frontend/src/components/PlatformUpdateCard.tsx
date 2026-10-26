import { useTranslation } from "react-i18next";

import { PortalApiError } from "@/api/client";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ProgressBar } from "@/components/ProgressBar";
import { UpdateCardFrame, UpdateFact } from "@/components/UpdateCardFrame";
import { isVersionAtLeast } from "@/lib/compareVersions";
import { formatUtcTimestamp } from "@/lib/formatTimestamp";
import { usePlatformUpdate } from "@/lib/usePlatformUpdate";
import { useRetryCountdown } from "@/lib/useRetryCountdown";

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
  const { platform, platformError, job, displayFirmware, repairFirmware, repairingFirmware, repairError, startUpdate, starting, startError, checkNow, checking, checkError } =
    usePlatformUpdate();
  const checkErrorShown = useRetryCountdown(checkError);

  if (!platform) {
    return <ApiErrorAlert error={platformError} />;
  }

  const toolFirmware = platform.display_firmware ? {
    result: platform.display_firmware.last_result, reason: platform.display_firmware.last_reason, after: null,
  } : null;
  const firmware = job?.state === "running" ? displayFirmware : toolFirmware ?? displayFirmware;
  const firmwareWarning = firmware && ["busy", "failed", "interrupted", "skipped"].includes(firmware.result);
  const firmwareMessages: Record<string, string> = {
    absent: t("update.displayFirmwareAbsent"),
    current: t("update.displayFirmwareCurrent"),
    updated: t("update.displayFirmwareUpdated"),
    undeclared: t("update.displayFirmwareUndeclared"),
  };
  const firmwareMessage = firmwareWarning
    ? firmware.result === "skipped" ? t("update.displayFirmwarePaused") : t("update.displayFirmwareRetry")
    : firmware ? firmwareMessages[firmware.result] : null;
  const firmwareRunning = job?.state === "running" && job.step === "display_firmware";
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

  const required = !platform.ready;
  const title = required
    ? t("update.platformRequiredTitle")
    : updateAvailable
      ? t("update.platformNewVersionAvailable")
      : upToDate
        ? t("update.platformUpToDate")
        : t("update.platformCheckFailedTitle");

  return (
    <UpdateCardFrame
      warning={required}
      eyebrow={t("update.platformTitle")}
      title={title}
      description={required ? t("update.platformRequiredBody") : t("update.platformDescription")}
      badge={updateAvailable ? <Badge variant="outline">{platform.latest?.tag}</Badge> : null}
      facts={
        <>
          <UpdateFact label={t("update.platformInstalledVersionLabel")}>
            {platform.installed_version ?? "—"}
            {platform.installed_at ? ` · ${formatUtcTimestamp(Date.parse(platform.installed_at) / 1000)}` : ""}
          </UpdateFact>
          <UpdateFact label={t("update.platformLatestVersionLabel")}>
            <span className="flex flex-wrap items-center justify-end gap-x-2">
              {platform.latest ? `${platform.latest.version} (${platform.latest.tag})` : "—"}
              <Button
                variant="ghost"
                size="sm"
                className="-mr-2 h-7 px-2"
                onClick={checkNow}
                disabled={checking || starting || job?.state === "running" || portalUpdateRunning}
              >
                {t("update.platformCheckNowButton")}
              </Button>
            </span>
          </UpdateFact>
          {platform.latest?.summary ? (
            <div className="flex flex-col gap-1">
              <dt className="text-muted-foreground">{t("update.platformSummaryLabel")}</dt>
              <dd>{platform.latest.summary}</dd>
            </div>
          ) : null}
        </>
      }
      status={
        <>
          {platform.verify_diffs.length > 0 ? (
            <Alert variant="destructive">
              <AlertDescription>{t("update.platformVerifyDiffBanner")}</AlertDescription>
            </Alert>
          ) : null}
          {job?.state === "running" ? (
            <div className="flex flex-col gap-2 rounded-lg bg-muted p-3">
              {firmwareRunning ? <p className="text-sm">{t("update.displayFirmwareWriting")}</p> : null}
              <ProgressBar label={t(firmwareRunning ? "update.displayFirmwareWriting" : "common.inProgress")} />
            </div>
          ) : job?.state === "failed" ? (
            <Alert variant="destructive">
              <AlertDescription>{job.error ?? t("errors.install_failed")}</AlertDescription>
            </Alert>
          ) : null}
          {!firmwareRunning && firmware ? (
            <Alert variant={firmwareWarning ? "destructive" : "default"}>
              <AlertDescription>
                {firmwareMessage}
                {firmware.reason ? ` ${firmware.reason}` : null}
                {firmware.after ? ` (${firmware.after})` : null}
              </AlertDescription>
            </Alert>
          ) : null}
          <Button
            variant="outline"
            className="w-fit"
            onClick={repairFirmware}
            disabled={!platform.display_firmware_available || repairingFirmware || starting || job?.state === "running" || portalUpdateRunning}
          >
            {t("update.displayFirmwareRepair")}
          </Button>
          <ApiErrorAlert error={repairError} />

          {legacyLedgerError ? (
            <Alert>
              <AlertDescription>{t("update.platformLegacyLedger")}</AlertDescription>
            </Alert>
          ) : null}
          <ApiErrorAlert error={startError instanceof PortalApiError && startError.code === "ledger_legacy" ? undefined : startError} />
          <ApiErrorAlert
            error={checkError instanceof PortalApiError && checkError.code === "ledger_legacy" ? undefined : checkErrorShown}
          />
        </>
      }
      footer={
        platform.latest?.reflash_required ? (
          <Alert>
            <AlertDescription>{t("update.platformReflashRequired")}</AlertDescription>
          </Alert>
        ) : portalTooOld ? (
          <Alert>
            <AlertDescription>{t("update.platformPortalTooOld")}</AlertDescription>
          </Alert>
        ) : !latestKnown ? (
          <PlatformLatestErrorMessage error={platform.latest_error} />
        ) : upToDate ? null : (
          <Button className="w-fit" onClick={startUpdate} disabled={starting || job?.state === "running"}>
            {t("update.platformUpdateButton")}
          </Button>
        )
      }
    />
  );
}
