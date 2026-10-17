export function appText(
  translations: Record<string, Record<string, string>> | undefined,
  language: string,
  key: string,
  fallback: string,
): string {
  const locale = language.toLowerCase();
  for (const candidate of [locale, locale.split("-")[0]]) {
    const value = translations?.[candidate]?.[key];
    if (value?.trim()) return value;
  }
  return fallback;
}
