import { Wifi, WifiLow, WifiZero } from "lucide-react";
import type { LucideIcon } from "lucide-react";

/**
 * The lucide icon for a Wi-Fi network's signal strength (0-100), used by
 * the Wi-Fi scan screen (routes/wifi.tsx): full bars at 60+, low bars at
 * 30+, and the empty-bars icon below that.
 */
export function signalIcon(signal: number): LucideIcon {
  if (signal >= 60) return Wifi;
  if (signal >= 30) return WifiLow;
  return WifiZero;
}
