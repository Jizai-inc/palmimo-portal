import { cn } from "@/lib/utils";

/** Underlined tab styling shared by button tabs (`AddAppPanel`) and link tabs (`/apps`). */
export function tabClassName(active: boolean): string {
  return cn(
    "-mb-px border-b-2 py-2 text-sm",
    active ? "border-foreground font-semibold text-foreground" : "border-transparent text-muted-foreground hover:text-foreground",
  );
}
