import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

/**
 * Never actually rendered in steady state: the root route's `beforeLoad`
 * (routes/__root.tsx) always redirects "/" to the required screen. This is
 * the brief loading state while that redirect resolves.
 */
export const Route = createFileRoute("/")({
  component: IndexPlaceholder,
});

function IndexPlaceholder() {
  const { t } = useTranslation();
  return <div className="flex min-h-screen items-center justify-center text-muted-foreground">{t("common.loading")}</div>;
}
