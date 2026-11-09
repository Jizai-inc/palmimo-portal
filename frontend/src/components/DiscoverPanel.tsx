import { AddAppPanel } from "@/components/AddAppPanel";

/** The Apps page's Discover tab: the public app catalog. */
export function DiscoverPanel({ onInstalled }: { onInstalled?: (appId: string | null) => void }) {
  return <AddAppPanel tabs={["catalog"]} onInstalled={onInstalled} />;
}
