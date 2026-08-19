import i18n from "i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import { initReactI18next } from "react-i18next";

import en from "./en.json";
import ja from "./ja.json";

// Default is the browser's language (LanguageDetector); components/LanguageToggle.tsx
// overrides it via i18next's own localStorage cache.
void i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      en: { translation: en },
      ja: { translation: ja },
    },
    fallbackLng: "en",
    supportedLngs: ["en", "ja"],
    interpolation: {
      escapeValue: false, // React already escapes.
    },
  });

export default i18n;
