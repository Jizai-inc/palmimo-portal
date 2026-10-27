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
    <div className="flex shrink-0 items-center rounded-[7px] bg-accent p-0.5" aria-label={t("app.languageToggle")}>
      {LANGUAGES.map((lang) => (
        <button
          key={lang}
          type="button"
          onClick={() => void i18n.changeLanguage(lang)}
          aria-pressed={current === lang}
          className={cn(
            "rounded-[5px] px-1.5 py-0.5 text-[11px] font-semibold transition-colors",
            current === lang ? "bg-background text-foreground" : "text-muted-foreground hover:text-foreground",
          )}
        >
          {lang.toUpperCase()}
        </button>
      ))}
    </div>
  );
}
