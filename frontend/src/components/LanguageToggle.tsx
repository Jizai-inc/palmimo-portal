import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

const LANGUAGES = ["en", "ja"] as const;

/**
 * Manual override for the browser-language default (see src/i18n/index.ts),
 * styled as a segmented pill: an active segment stands out with
 * `bg-background`, inactive segments sit flush against the `bg-muted`
 * container.
 */
export function LanguageToggle() {
  const { i18n, t } = useTranslation();
  const current = i18n.resolvedLanguage ?? "en";

  return (
    <div className="flex items-center gap-0.5 rounded-md bg-muted p-0.5" aria-label={t("app.languageToggle")}>
      {LANGUAGES.map((lang) => (
        <button
          key={lang}
          type="button"
          onClick={() => void i18n.changeLanguage(lang)}
          aria-pressed={current === lang}
          className={cn(
            "rounded-sm px-2 py-1 text-xs transition-colors",
            current === lang ? "bg-background font-semibold text-foreground" : "text-muted-foreground",
          )}
        >
          {lang.toUpperCase()}
        </button>
      ))}
    </div>
  );
}
