import { cn } from "@/lib/utils";

/** Underlined tab styling shared by button tabs (`AddAppPanel`) and link tabs (`/apps`). */
export function tabClassName(active: boolean): string {
  return cn(
    "border-b-2 px-3 py-2 text-sm font-medium",
    active ? "border-primary text-foreground" : "border-transparent text-muted-foreground hover:text-foreground",
  );
}
