import { useRef } from "react";
import { useTranslation } from "react-i18next";
import { Badge } from "@/components/ui/badge";

export function AppOfficialBadge({ official, publicAppId }: { official: boolean | null; publicAppId?: string | null }) {
  const { t } = useTranslation();
  const previous = useRef<boolean | null>(null);
  if (official !== null) previous.current = official;
  if (official === null && publicAppId) {
    return <Badge variant="outline">{previous.current ? t("appAdd.catalogOfficialBadge") : t("appAdd.catalogOfficialUnknown")}</Badge>;
  }
  return official ? <Badge>{t("appAdd.catalogOfficialBadge")}</Badge> : null;
}
