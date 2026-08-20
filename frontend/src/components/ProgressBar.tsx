/**
 * An indeterminate progress bar. Pure CSS (no animation library): a `@keyframes` in
 * src/index.css drives the segment's position. `label` is required, not defaulted, so no
 * caller can ship a hard-coded English `aria-label` by omission.
 */
export function ProgressBar({ label }: { label: string }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted" role="progressbar" aria-label={label}>
      <div className="h-full w-1/3 animate-indeterminate-progress rounded-full bg-primary" />
    </div>
  );
}
