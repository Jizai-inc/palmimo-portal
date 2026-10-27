import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { useListAppsApiV1AppsGet } from "@/api/generated/apps/apps";
import type { ChecklistState } from "@/api/generated/models";
import { useListGitCredentialsApiV1GitCredentialsGet } from "@/api/generated/secrets/secrets";
import {
  getGetChecklistApiV1SystemChecklistGetQueryKey,
  useGetChecklistApiV1SystemChecklistGet,
  useGetStatusApiV1SystemStatusGet,
  usePutChecklistApiV1SystemChecklistPut,
} from "@/api/generated/system/system";
import { useGetStatusApiV1UpdateStatusGet } from "@/api/generated/update/update";
import { DashboardStatusCard, DeviceInfoCard } from "@/components/DashboardStatusCard";
import { NeedsAttentionCard } from "@/components/NeedsAttentionCard";
import { RecentActivityCard } from "@/components/RecentActivityCard";
import { SetupChecklistCard } from "@/components/SetupChecklistCard";
import { Badge } from "@/components/ui/badge";
import { attentionItems } from "@/lib/homeAttention";
import { recentActivity } from "@/lib/recentActivity";
import { checklistItems } from "@/lib/setupChecklist";

/** Home: status tiles, then needs attention, the setup checklist and recent activity beside the device facts. */
export function HomePanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data: status } = useGetStatusApiV1SystemStatusGet({ query: { refetchInterval: 10_000 } });
  // Cached read only -- never fires POST /update/check; UpdatePanel owns the auto-check.
  const { data: updateStatus } = useGetStatusApiV1UpdateStatusGet();
  const { data: appsData } = useListAppsApiV1AppsGet();
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
    portalUpdateAvailable: updateStatus?.update_available,
    autostartConflict: appsData?.autostart_conflict,
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
    <div className="flex flex-col gap-4">
      <DashboardStatusCard
        portalBadge={updateBadge}
        appsLink={
          <Link to="/apps" className="text-foreground underline underline-offset-2 hover:text-foreground/80">
            {t("home.tileOpenApps")}
          </Link>
        }
      />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)] lg:items-start">
        <div className="flex min-w-0 flex-col gap-4">
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
        <DeviceInfoCard />
      </div>
    </div>
  );
}
