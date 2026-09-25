/**
 * Renders an app id (`<namespace>.<name>`, design doc 3.9) as one string with the namespace
 * segment dimmed and the name segment bold -- the list/detail/breadcrumb convention for telling
 * two apps with the same manifest `name` apart at a glance.
 */
export function AppIdLabel({ id, className }: { id: string; className?: string }) {
  const dot = id.indexOf(".");
  if (dot === -1) return <span className={className}>{id}</span>;
  return (
    <span className={className}>
      <span className="text-muted-foreground">{id.slice(0, dot + 1)}</span>
      <span className="font-semibold">{id.slice(dot + 1)}</span>
    </span>
  );
}

/** The name segment after an id's namespace, or the whole id if it has no `.` (defensive only --
 * every real id has one, design doc 3.9's `APP_ID_RE`). */
export function appIdNamePart(id: string): string {
  const dot = id.indexOf(".");
  return dot === -1 ? id : id.slice(dot + 1);
}
