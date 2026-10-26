import { useTranslation } from "react-i18next";

import { DOCS_URL } from "@/lib/navigation";

/** The trailing "Learn more" that replaces a long explanation on a page; opens the docs. */
export function LearnMoreLink({ href = DOCS_URL, label }: { href?: string; label?: string }) {
  const { t } = useTranslation();
  return (
    <a href={href} target="_blank" rel="noreferrer" className="text-foreground underline underline-offset-2 hover:text-foreground/80">
      {label ?? t("common.learnMore")}
    </a>
  );
}
