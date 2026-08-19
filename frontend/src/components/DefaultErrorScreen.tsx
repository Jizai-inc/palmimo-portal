import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";

/**
 * The router's last-resort error screen -- rendered only when a route's own
 * `beforeLoad`/`loader`/`component` throws something not turned into a
 * normal screen (main.tsx's `createRouter`'s `defaultErrorComponent`). Every
 * anticipated failure already resolves to a dedicated i18n'd screen further
 * up the stack (routes/status-error.tsx, `resolveAuthGateSafely`), so
 * reaching this means something unexpected slipped through -- still worth
 * an i18n'd message instead of TanStack Router's default error view.
 */
export function DefaultErrorScreen() {
  const { t } = useTranslation();
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 p-4 text-center">
      <h1 className="text-lg font-semibold">{t("app.unexpectedErrorTitle")}</h1>
      <p className="text-sm text-muted-foreground">{t("app.unexpectedErrorBody")}</p>
      <Button onClick={() => window.location.reload()}>{t("common.retry")}</Button>
    </div>
  );
}
