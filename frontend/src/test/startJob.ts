import type { AppJobInfo } from "@/api/generated/models";

export const startJob: AppJobInfo = {
  id: "start-1", app_id: "face", kind: "start", state: "running", step: "firmware",
  error: null, error_code: null, started_at: 1, finished_at: null, warning: null,
  dropped_bindings: [], dropped_params: [], lock_generated: false,
};
