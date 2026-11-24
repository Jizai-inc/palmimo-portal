import type { TFunction } from "i18next";

export function displayAssetName(t: TFunction, name: string): string {
  switch (name) {
    case "happy": return t("settings.display.names.happy");
    case "shy": return t("settings.display.names.shy");
    case "excited": return t("settings.display.names.excited");
    case "surprise": return t("settings.display.names.surprise");
    case "curious": return t("settings.display.names.curious");
    case "thinking": return t("settings.display.names.thinking");
    case "angry": return t("settings.display.names.angry");
    case "sad": return t("settings.display.names.sad");
    case "sleepy": return t("settings.display.names.sleepy");
    case "love": return t("settings.display.names.love");
    case "sleeping": return t("settings.display.names.sleeping");
    case "maru": return t("settings.display.names.maru");
    case "batsu": return t("settings.display.names.batsu");
    case "groove": return t("settings.display.names.groove");
    case "system.idle": return t("settings.display.names.systemIdle");
    case "system.sleep": return t("settings.display.names.systemSleep");
    case "system.wake": return t("settings.display.names.systemWake");
    default: return name;
  }
}
