import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import {
  useCheckPlatformApiV1PlatformCheckPost,
  getGetPlatformApiV1PlatformGetQueryKey,
  getGetUpdateJobApiV1PlatformUpdateGetQueryKey,
  useRewriteDisplayFirmwareApiV1PlatformDisplayFirmwarePost,
  useGetPlatformApiV1PlatformGet,
  useGetUpdateJobApiV1PlatformUpdateGet,
  useStartUpdateApiV1PlatformUpdatePost,
} from "@/api/generated/platform/platform";
import type { PlatformStatusResponse, PlatformUpdateAcceptedResponse } from "@/api/generated/models";

/** How often to re-poll `GET /platform/update` while a bundle apply is running (design doc 2.8). */
const PLATFORM_JOB_POLL_INTERVAL_MS = 2_000;

/**
 * Shared state for the device-platform update surface (design doc 2.8/3.7): the apps list's
 * "update the device platform" banner and the update page's platform row both need the same
 * `GET /platform` status, "start update" mutation, and job polling -- factored out once here
 * instead of duplicated.
 */
export function usePlatformUpdate() {
  const queryClient = useQueryClient();
  const { data: platform, error: platformError } = useGetPlatformApiV1PlatformGet();
  const { data: jobData } = useGetUpdateJobApiV1PlatformUpdateGet({
    query: {
      refetchInterval: (query) => (query.state.data?.job.state === "running" ? PLATFORM_JOB_POLL_INTERVAL_MS : false),
    },
  });
  const adoptJob = (data: PlatformUpdateAcceptedResponse) => {
    queryClient.setQueryData(getGetUpdateJobApiV1PlatformUpdateGetQueryKey(), data);
    void queryClient.invalidateQueries({ queryKey: getGetUpdateJobApiV1PlatformUpdateGetQueryKey() });
    void queryClient.invalidateQueries({ queryKey: getGetPlatformApiV1PlatformGetQueryKey() });
  };
  const startUpdate = useStartUpdateApiV1PlatformUpdatePost({ mutation: { onSuccess: adoptJob } });
  const repairFirmware = useRewriteDisplayFirmwareApiV1PlatformDisplayFirmwarePost({ mutation: { onSuccess: adoptJob } });
  // Writes straight into the `GET /platform` query's cache, same as UpdatePanel's `adoptStatus`,
  // so the card reflects a bypassed-cache fetch immediately rather than waiting on a refetch.
  const checkNow = useCheckPlatformApiV1PlatformCheckPost({
    mutation: {
      onSuccess: (data: PlatformStatusResponse) => {
        queryClient.setQueryData(getGetPlatformApiV1PlatformGetQueryKey(), data);
      },
    },
  });

  const job = jobData?.job ?? null;
  const jobState = job?.state;
  useEffect(() => {
    if (jobState !== undefined && jobState !== "running") {
      void queryClient.invalidateQueries({ queryKey: getGetPlatformApiV1PlatformGetQueryKey() });
    }
  }, [jobState, queryClient]);

  return {
    platform,
    platformError,
    job,
    displayFirmware: jobData?.display_firmware,
    repairFirmware: () => repairFirmware.mutate(),
    repairingFirmware: repairFirmware.isPending,
    repairError: repairFirmware.error,
    startUpdate: () => startUpdate.mutate(),
    starting: startUpdate.isPending,
    startError: startUpdate.error,
    checkNow: () => checkNow.mutate(undefined, { onError: () => undefined }),
    checking: checkNow.isPending,
    checkError: checkNow.error,
  };
}
