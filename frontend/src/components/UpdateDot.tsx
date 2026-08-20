import { cn } from "@/lib/utils";

/**
 * A small red dot marking an update-available spot -- `aria-hidden` and
 * `pointer-events-none` so it never intercepts a click meant for the
 * element it decorates; callers pair it with an accessible announcement.
 * Shared by `AppHeader`'s nav toggle and `AppShell`'s per-item sidebar dot.
 */
export function UpdateDot({ className }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={cn("pointer-events-none absolute block size-2 rounded-full bg-red-500 ring-2 ring-background", className)}
    />
  );
}
