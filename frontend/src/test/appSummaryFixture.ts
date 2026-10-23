import type { AppSummary } from "@/api/generated/models";

export function appSummary(overrides: Partial<AppSummary> = {}): AppSummary {
  return {
    id: "alice.robot",
    name: "robot",
    status: "stopped",
    autostart: false,
    broken_reason: null,
    credential_rejected: false,
    installed_at: 1,
    last_job: null,
    latest_commit: null,
    update_available: false,
    source: { type: "git", official: false, url: "https://github.com/alice/robot", subdir: null, manifest: null, ref_kind: "branch", ref: "main", commit: "abc" },
    ...overrides,
  };
}

export function finishedJob(kind: string, state: "done" | "failed", finishedAt: number) {
  return {
    id: `job-${finishedAt}`,
    app_id: "alice.robot",
    kind,
    state,
    step: null,
    error: null,
    error_code: null,
    started_at: finishedAt - 1,
    finished_at: finishedAt,
    dropped_bindings: [],
    dropped_params: [],
    lock_generated: false,
  };
}
