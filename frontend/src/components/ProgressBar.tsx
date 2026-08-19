/**
 * An indeterminate progress bar -- a thin `bg-muted` track with an animated
 * `bg-primary` segment sliding across it. Pure CSS (no animation library):
 * a `@keyframes` in src/index.css drives the segment's position. Used by
 * the wifi-waiting screen (routes/wifi.waiting.tsx) and the power screen's
 * rebooting state (via CenteredState.tsx).
 *
 * `label` is required rather than defaulted so no caller can ship a
 * hard-coded English `aria-label` by omission -- every call site passes
 * `t("common.inProgress")` (or another translated string) itself.
 */
export function ProgressBar({ label }: { label: string }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted" role="progressbar" aria-label={label}>
      <div className="h-full w-1/3 animate-indeterminate-progress rounded-full bg-primary" />
    </div>
  );
}
