import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import {
  useCheckPlatformApiV1PlatformCheckPost,
  getGetPlatformApiV1PlatformGetQueryKey,
  useGetPlatformApiV1PlatformGet,
  useGetUpdateJobApiV1PlatformUpdateGet,
  useStartUpdateApiV1PlatformUpdatePost,
} from "@/api/generated/platform/platform";
import type { PlatformStatusResponse } from "@/api/generated/models";

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
  const [polling, setPolling] = useState(false);
  const { data: platform, error: platformError } = useGetPlatformApiV1PlatformGet();
  const { data: jobData } = useGetUpdateJobApiV1PlatformUpdateGet({
    query: {
      enabled: polling,
      refetchInterval: (query) => (query.state.data?.job.state === "running" ? PLATFORM_JOB_POLL_INTERVAL_MS : false),
    },
  });
  const startUpdate = useStartUpdateApiV1PlatformUpdatePost({
    mutation: {
      onSuccess: () => {
        setPolling(true);
        void queryClient.invalidateQueries({ queryKey: getGetPlatformApiV1PlatformGetQueryKey() });
      },
    },
  });
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
  // Stop polling once the job settles; re-fetch `GET /platform` so `ready`/`installed_version`
  // reflect a successful apply immediately instead of waiting for the next natural refetch.
  useEffect(() => {
    if (job !== null && job.state !== "running" && polling) {
      setPolling(false);
      void queryClient.invalidateQueries({ queryKey: getGetPlatformApiV1PlatformGetQueryKey() });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.state]);

  return {
    platform,
    platformError,
    job,
    startUpdate: () => startUpdate.mutate(),
    starting: startUpdate.isPending,
    startError: startUpdate.error,
    checkNow: () => checkNow.mutate(undefined, { onError: () => undefined }),
    checking: checkNow.isPending,
    checkError: checkNow.error,
  };
}
