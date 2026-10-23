import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { useListAppsApiV1AppsGet } from "@/api/generated/apps/apps";
import type { ChecklistState } from "@/api/generated/models";
import { useGetPlatformApiV1PlatformGet } from "@/api/generated/platform/platform";
import { useListGitCredentialsApiV1GitCredentialsGet } from "@/api/generated/secrets/secrets";
import {
  getGetChecklistApiV1SystemChecklistGetQueryKey,
  useGetChecklistApiV1SystemChecklistGet,
  useGetStatusApiV1SystemStatusGet,
  usePutChecklistApiV1SystemChecklistPut,
} from "@/api/generated/system/system";
import { useGetStatusApiV1UpdateStatusGet } from "@/api/generated/update/update";
import { DashboardStatusCard } from "@/components/DashboardStatusCard";
import { NeedsAttentionCard } from "@/components/NeedsAttentionCard";
import { RecentActivityCard } from "@/components/RecentActivityCard";
import { SetupChecklistCard } from "@/components/SetupChecklistCard";
import { Badge } from "@/components/ui/badge";
import { attentionItems } from "@/lib/homeAttention";
import { recentActivity } from "@/lib/recentActivity";
import { checklistItems } from "@/lib/setupChecklist";

/** Home: needs attention, the setup checklist and recent activity, beside the robot status. */
export function HomePanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data: status } = useGetStatusApiV1SystemStatusGet({ query: { refetchInterval: 10_000 } });
  // Cached read only -- never fires POST /update/check; UpdatePanel owns the auto-check.
  const { data: updateStatus } = useGetStatusApiV1UpdateStatusGet();
  const { data: appsData } = useListAppsApiV1AppsGet();
  const { data: platform } = useGetPlatformApiV1PlatformGet();
  const { data: credentials } = useListGitCredentialsApiV1GitCredentialsGet();
  const { data: checklist } = useGetChecklistApiV1SystemChecklistGet();

  const checklistKey = getGetChecklistApiV1SystemChecklistGetQueryKey();
  const hideChecklist = usePutChecklistApiV1SystemChecklistPut({
    mutation: {
      onMutate: () => {
        const previous = queryClient.getQueryData<ChecklistState>(checklistKey);
        queryClient.setQueryData<ChecklistState>(checklistKey, { dismissed: true });
        return { previous };
      },
      onError: (_error, _variables, context) => queryClient.setQueryData(checklistKey, context?.previous),
    },
  });

  const apps = appsData?.apps ?? [];
  const attention = attentionItems({
    apps,
    platformReady: platform?.ready,
    portalUpdateAvailable: updateStatus?.update_available,
  });
  const steps = checklistItems({ apps, gitCredentialCount: credentials?.credentials.length ?? 0 });
  const showChecklist =
    checklist !== undefined && !checklist.dismissed && appsData !== undefined && credentials !== undefined && steps.some((step) => !step.done);
  const activity = recentActivity({ apps, lastWifiAttempt: status?.last_wifi_attempt });

  const updateBadge = updateStatus?.update_available ? (
    <Link to="/settings/update">
      <Badge>{t("dashboard.updateAvailableBadge")}</Badge>
    </Link>
  ) : undefined;

  return (
    <div className="flex flex-col gap-6 lg:flex-row lg:items-start">
      <div className="flex min-w-0 flex-1 flex-col gap-6">
        {attention.length > 0 ? <NeedsAttentionCard items={attention} /> : null}
        {showChecklist ? (
          <SetupChecklistCard
            items={steps}
            hiding={hideChecklist.isPending}
            onHide={() => hideChecklist.mutate({ data: { dismissed: true } })}
          />
        ) : null}
        <RecentActivityCard items={activity} />
      </div>

      <div className="flex w-full flex-col gap-6 lg:w-[360px] lg:shrink-0">
        <DashboardStatusCard portalBadge={updateBadge} />
        {/* Desktop only -- mobile shows Portal/SDK inside DashboardStatusCard's own KV list. */}
        <div className="hidden flex-col gap-3 rounded-xl border border-border bg-card p-4 md:flex">
          <p className="font-semibold">{t("dashboard.softwareTitle")}</p>
          <dl className="flex flex-col gap-2 text-sm">
            <div className="flex items-center justify-between">
              <dt className="text-muted-foreground">{t("dashboard.portalLabel")}</dt>
              <dd className="flex items-center gap-2 font-medium">
                {status?.versions.portal ?? "—"}
                {updateBadge}
              </dd>
            </div>
            <div className="flex items-center justify-between">
              <dt className="text-muted-foreground">{t("dashboard.sdkLabel")}</dt>
              <dd className="font-medium">{status?.versions.sdk ?? "—"}</dd>
            </div>
          </dl>
        </div>
      </div>
    </div>
  );
}
