import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";

import { getGetAppApiV1AppsNameGetQueryKey, getListAppsApiV1AppsGetQueryKey, useStartAppEndpointApiV1AppsNameStartPost } from "@/api/generated/apps/apps";
import type { AppJobInfo, AppSummary } from "@/api/generated/models";
import { getGetPlatformApiV1PlatformGetQueryKey, useGetPlatformApiV1PlatformGet } from "@/api/generated/platform/platform";
import { getGetStatusApiV1SystemStatusGetQueryKey } from "@/api/generated/system/system";

export type StartableApp = Pick<AppSummary, "id" | "name" | "display_firmware">;

export function useAppStart(apps: StartableApp[]) {
  const client = useQueryClient();
  const { data: platform, error: platformError } = useGetPlatformApiV1PlatformGet(undefined, {
    query: { enabled: apps.some((app) => app.display_firmware === "1" || app.display_firmware === "2") },
  });
  const [confirmation, setConfirmation] = useState<{ app: StartableApp; unknown: boolean } | null>(null);
  const [confirmationVisible, setConfirmationVisible] = useState(false);
  const [jobVisible, setJobVisible] = useState(false);
  const [job, setJob] = useState<{ id: string; app: StartableApp } | null>(null);
  const inFlight = useRef(false);
  const requestedApp = useRef<StartableApp | null>(null);
  const refresh = (id: string) => {
    for (const queryKey of [
      getListAppsApiV1AppsGetQueryKey(), getGetAppApiV1AppsNameGetQueryKey(id),
      getGetStatusApiV1SystemStatusGetQueryKey(), getGetPlatformApiV1PlatformGetQueryKey(),
    ]) {
      void client.invalidateQueries({ queryKey });
    }
  };
  const mutation = useStartAppEndpointApiV1AppsNameStartPost({ mutation: {
    onSuccess: (data, { name }) => {
      setConfirmationVisible(false);
      setJob({ id: data.job.id, app: requestedApp.current ?? { id: name, name } });
      setJobVisible(true);
      refresh(name);
    },
    onSettled: () => { inFlight.current = false; },
  } });
  const launch = (app: StartableApp) => {
    if (inFlight.current) return;
    inFlight.current = true;
    requestedApp.current = app;
    mutation.mutate({ name: app.id });
  };
  return {
    ...mutation,
    job,
    jobVisible,
    confirmation,
    confirmationVisible,
    request: (app: StartableApp) => {
      if (inFlight.current) return;
      mutation.reset();
      const need = app.display_firmware;
      const desired = platformError ? null : platform?.display_firmware_desired;
      if ((need === "1" || need === "2") && Number(need) !== desired) {
        setConfirmation({ app, unknown: desired == null });
        setConfirmationVisible(true);
      } else {
        launch(app);
      }
    },
    confirm: () => { if (confirmation && confirmationVisible) launch(confirmation.app); },
    cancel: () => setConfirmationVisible(false),
    closeJob: () => setJobVisible(false),
    finish: (result: AppJobInfo) => {
      const appId = result.app_id ?? job?.app.id;
      if (appId) refresh(appId);
      if (result.id === job?.id && (result.warning || result.state === "failed")) setJobVisible(true);
    },
  };
}
