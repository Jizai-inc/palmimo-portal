import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { LearnMoreLink } from "@/components/LearnMoreLink";
import { PageHeader } from "@/components/PageHeader";
import { UpdatePanel } from "@/components/UpdatePanel";

export const Route = createFileRoute("/settings/update")({
  component: UpdateSettings,
});

function UpdateSettings() {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title={t("nav.softwareUpdate")}
        description={
          <>
            {t("update.pageDescription")} <LearnMoreLink />
          </>
        }
      />
      <UpdatePanel />
    </div>
  );
}
