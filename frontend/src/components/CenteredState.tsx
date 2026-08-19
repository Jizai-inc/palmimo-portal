import type { LucideIcon } from "lucide-react";
import type * as React from "react";

import { ProgressBar } from "@/components/ProgressBar";

/**
 * A centered icon + title + body block, reused by the power screen's
 * rebooting/shutting-down states (routes/power.tsx via PowerPanel.tsx) and
 * the status-error screens (routes/status-error.tsx). Optionally shows an
 * indeterminate {@link ProgressBar} and/or an action below the body.
 */
export function CenteredState({
  icon: Icon,
  title,
  body,
  showProgress = false,
  progressLabel,
  action,
}: {
  icon: LucideIcon;
  /** Omitted on the status-error screens, whose title already renders as `AuthShell`'s own heading. */
  title?: string;
  body: string;
  /** Shows the indeterminate progress bar below the body -- only the power screen's rebooting state uses this. */
  showProgress?: boolean;
  /** {@link ProgressBar}'s `aria-label`. Required whenever `showProgress` is set -- e.g. `t("common.inProgress")`. */
  progressLabel?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-4 py-8 text-center">
      <div className="flex size-16 items-center justify-center rounded-full bg-muted">
        <Icon className="size-7 text-foreground" />
      </div>
      <div className="flex flex-col gap-1.5">
        {title ? <p className="text-lg font-semibold">{title}</p> : null}
        <p className="text-sm text-muted-foreground">{body}</p>
      </div>
      {showProgress ? (
        <div className="w-full max-w-xs">
          <ProgressBar label={progressLabel ?? ""} />
        </div>
      ) : null}
      {action}
    </div>
  );
}
